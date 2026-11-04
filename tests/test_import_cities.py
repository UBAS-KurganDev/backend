"""Importer logic: score math, snapshot merging, hermetic CLI and HTTP client.

Nothing here touches the network: the OpenTripMap client is driven through
``httpx.MockTransport``, and CLI paths either fail validation early or run in
``--from-snapshot`` mode.
"""

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

import ubas_backend.import_cities as importer
from tests.factories import city_profile
from ubas_backend.routing.storage import SqliteCatalog

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI defaults must not leak from the developer's environment."""

    monkeypatch.delenv(importer.API_KEY_ENV, raising=False)
    monkeypatch.delenv(importer.CATALOG_PATH_ENV, raising=False)


def make_record(
    code: str,
    *,
    score: float = 0.5,
    notable: int = 10,
    landmarks: int = 2,
    population: int | None = 100_000,
) -> importer.CityRecord:
    return importer.CityRecord(
        profile=city_profile(code, tourism_score=score),
        counts=importer.SightCounts(notable=notable, landmarks=landmarks),
        population=population,
    )


def run_cli(argv: list[str]) -> int:
    return importer.run(importer.parse_args(argv))


def mock_client(handler: Handler) -> importer.OpenTripMapClient:
    return importer.OpenTripMapClient(
        httpx.Client(
            base_url=importer.API_BASE_URL, transport=httpx.MockTransport(handler)
        ),
        "test-key",
    )


# --- score normalization -------------------------------------------------


def test_tourism_scores_spans_floor_to_one() -> None:
    """Тестит, что оценки растягиваются на [SCORE_FLOOR, 1.0] по выборке."""

    scores = importer.tourism_scores(
        {
            "AAA": importer.SightCounts(notable=1000, landmarks=0),
            "BBB": importer.SightCounts(notable=0, landmarks=0),
        }
    )
    assert scores["AAA"] == 1.0
    assert scores["BBB"] == pytest.approx(importer.SCORE_FLOOR)


def test_tourism_scores_landmarks_outweigh_notable() -> None:
    """Тестит, что «мировые» достопримечательности весят больше обычных."""

    scores = importer.tourism_scores(
        {
            "AAA": importer.SightCounts(notable=5, landmarks=0),
            "BBB": importer.SightCounts(notable=0, landmarks=2),
        }
    )
    assert scores["BBB"] > scores["AAA"]


def test_tourism_scores_degenerate_set_is_all_ones() -> None:
    """Тестит вырожденный случай: одинаковые счётчики дают всем 1.0."""

    scores = importer.tourism_scores(
        {
            "AAA": importer.SightCounts(notable=7, landmarks=0),
            "BBB": importer.SightCounts(notable=7, landmarks=0),
        }
    )
    assert scores == {"AAA": 1.0, "BBB": 1.0}


# --- record assembly and snapshots ---------------------------------------


def test_assemble_records_builds_profiles_from_fresh_fetch() -> None:
    """Тестит сборку записей из свежих данных API: атрибуты города и оценка."""

    specs = (
        importer.CitySpec("AAA", "Alpha", "FR"),
        importer.CitySpec("BBB", "Beta", "IT"),
    )
    fetched = {
        "AAA": (
            importer.CityLocation(48.85, 2.35, "Europe/Paris", 2_000_000),
            importer.SightCounts(notable=100, landmarks=10),
        ),
        "BBB": (
            importer.CityLocation(41.9, 12.5, "Europe/Rome", None),
            importer.SightCounts(notable=50, landmarks=0),
        ),
    }
    records = importer.assemble_records(specs, fetched, {})
    assert [record.profile.city_code for record in records] == ["AAA", "BBB"]
    assert records[0].profile.timezone == "Europe/Paris"
    assert records[0].profile.visas == ()
    assert records[0].population == 2_000_000
    assert records[0].profile.tourism_score == 1.0
    assert records[1].profile.tourism_score == pytest.approx(importer.SCORE_FLOOR)


def test_assemble_records_merges_previous_snapshot() -> None:
    """Тестит режим --city: прежние города сохраняют атрибуты, но не оценку.

    BBB есть только в старом снапшоте: его имя и счётчики берутся оттуда,
    а оценка пересчитывается по объединённой выборке, где BBB теперь максимум.
    CCC отсутствует везде и выпадает из выдачи.
    """

    specs = (
        importer.CitySpec("AAA", "Alpha", "FR"),
        importer.CitySpec("BBB", "Beta", "IT"),
        importer.CitySpec("CCC", "Gamma", "ES"),
    )
    fetched = {
        "AAA": (
            importer.CityLocation(48.85, 2.35, "Europe/Paris", 2_000_000),
            importer.SightCounts(notable=100, landmarks=10),
        ),
    }
    previous = {"BBB": make_record("BBB", notable=900, landmarks=0)}
    records = importer.assemble_records(specs, fetched, previous)
    codes = [record.profile.city_code for record in records]
    assert codes == ["AAA", "BBB"]

    bbb = records[1]
    assert bbb.profile.name == "City BBB"
    assert bbb.counts == importer.SightCounts(notable=900, landmarks=0)
    assert bbb.profile.tourism_score == 1.0
    # AAA is the sample minimum now, not the lone 1.0 it would be alone.
    assert records[0].profile.tourism_score == pytest.approx(importer.SCORE_FLOOR)


def test_snapshot_round_trip(tmp_path: Path) -> None:
    """Тестит, что снапшот пишется и читается без потерь."""

    records = (
        make_record("AAA", score=0.7, notable=11, landmarks=3),
        make_record("BBB", score=0.2, population=None),
    )
    path = tmp_path / "snap.json"
    importer.write_snapshot(path, records)
    assert importer.read_snapshot(path) == records


# --- CLI paths without network -------------------------------------------


def test_run_from_snapshot_upserts_catalog(tmp_path: Path) -> None:
    """Тестит загрузку каталога из снапшота: код 0 и профили в SQLite."""

    snapshot = tmp_path / "snap.json"
    importer.write_snapshot(
        snapshot, (make_record("AAA", score=0.4), make_record("BBB", score=0.9))
    )
    catalog_path = tmp_path / "catalog.sqlite3"
    code = run_cli(["--from-snapshot", str(snapshot), "--catalog", str(catalog_path)])
    assert code == 0

    catalog = SqliteCatalog(catalog_path)
    profiles = asyncio.run(catalog.list_by_tourism())
    assert [profile.city_code for profile in profiles] == ["BBB", "AAA"]


def test_run_from_snapshot_without_catalog_writes_nothing(tmp_path: Path) -> None:
    """Тестит, что без --catalog снапшот-режим завершается без побочных файлов."""

    snapshot = tmp_path / "snap.json"
    importer.write_snapshot(snapshot, (make_record("AAA"),))
    assert run_cli(["--from-snapshot", str(snapshot)]) == 0
    assert list(tmp_path.iterdir()) == [snapshot]


def test_run_from_snapshot_reports_unreadable_file(tmp_path: Path) -> None:
    """Тестит аккуратную ошибку (код 1) вместо трейсбека на битом снапшоте."""

    missing = tmp_path / "missing.json"
    assert run_cli(["--from-snapshot", str(missing)]) == 1

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert run_cli(["--from-snapshot", str(broken)]) == 1


def test_run_rejects_conflicting_flags(tmp_path: Path) -> None:
    """Тестит валидацию аргументов: несовместимые флаги дают код 2."""

    snapshot = tmp_path / "snap.json"
    assert (
        run_cli(["--from-snapshot", str(snapshot), "--city", "PAR", "--api-key", "k"])
        == 2
    )
    assert (
        run_cli(["--from-snapshot", str(snapshot), "--limit", "3", "--api-key", "k"])
        == 2
    )
    assert run_cli(["--api-key", "k", "--city", "PAR", "--limit", "3"]) == 2


def test_run_rejects_non_positive_limit() -> None:
    """Тестит, что --limit < 1 отвергается, а не молча ломает срез списка."""

    assert run_cli(["--api-key", "k", "--limit", "0"]) == 2
    assert run_cli(["--api-key", "k", "--limit", "-2"]) == 2


def test_run_rejects_unknown_city_codes() -> None:
    """Тестит ошибку на кодах, которых нет в справочнике TOP_CITIES."""

    assert run_cli(["--api-key", "k", "--city", "PAR", "--city", "ZZZ"]) == 2


def test_run_requires_api_key(tmp_path: Path) -> None:
    """Тестит, что без ключа импорт из API не стартует (код 2)."""

    assert run_cli(["--snapshot", str(tmp_path / "s.json")]) == 2


# --- OpenTripMap client over MockTransport -------------------------------


def test_client_parses_geoname_response() -> None:
    """Тестит разбор /places/geoname: координаты, зона, население."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["apikey"] == "test-key"
        return httpx.Response(
            200,
            json={
                "status": "OK",
                "lat": 48.85,
                "lon": 2.35,
                "timezone": "Europe/Paris",
                "population": 2_131_855,
            },
        )

    location = mock_client(handler).city_location("Paris", "FR")
    assert location == importer.CityLocation(48.85, 2.35, "Europe/Paris", 2_131_855)


def test_client_returns_none_on_geoname_miss_or_garbage() -> None:
    """Тестит, что мимо GeNames или с битыми полями локация даёт None."""

    def miss(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ZERO_RESULTS"})

    def garbage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "OK",
                "lat": "48.85",  # строка вместо числа
                "lon": 2.35,
                "timezone": "Europe/Paris",
            },
        )

    assert mock_client(miss).city_location("Nowhere", "XX") is None
    assert mock_client(garbage).city_location("Paris", "FR") is None


def test_client_sight_counts_paginates_and_skips_non_integer_rates() -> None:
    """Тестит подсчёт достопримечательностей: пагинация и пропуск мусорного rate."""

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.params["offset"] == "0":
            page = [{"rate": 3}] * 400 + [{"rate": 4}] * 100 + [{"rate": "3"}]
        else:
            page = [{"rate": 4}, {"rate": 3}]
        return httpx.Response(200, json=page)

    counts = mock_client(handler).sight_counts(48.85, 2.35)
    assert counts == importer.SightCounts(notable=401, landmarks=101)
    assert [request.url.params["offset"] for request in requests] == ["0", "500"]


def test_client_retries_non_json_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Тестит фикс: 200 с не-JSON телом ретраится и в итоге даёт ошибку API."""

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text="<html>proxy login page</html>")

    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    with pytest.raises(importer.OpenTripMapError):
        mock_client(handler).city_location("Paris", "FR")
    assert len(requests) == importer.REQUEST_ATTEMPTS
