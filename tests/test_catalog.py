"""SqliteCatalog: ORM round-trips, tourism listings and topology lookups."""

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from tests.conftest import SEEDED_SCORES
from tests.factories import city_profile, visa_policy
from ubas_backend.routing.schemas import Money
from ubas_backend.routing.storage import CatalogData, FlightLink, SqliteCatalog


async def test_get_returns_the_stored_profile(catalog: SqliteCatalog) -> None:
    """Тестит точечный поиск города: существующий код отдаёт профиль целиком."""

    profile = await catalog.get("PAR")
    assert profile is not None
    assert profile.city_code == "PAR"
    assert profile.tourism_score == SEEDED_SCORES["PAR"]


async def test_get_returns_none_for_unknown_city(catalog: SqliteCatalog) -> None:
    """Тестит, что незнакомый код города даёт None, а не ошибку."""

    assert await catalog.get("XXX") is None


async def test_profile_round_trip_preserves_visas_and_hotel(
    tmp_path: Path,
) -> None:
    """Тестит ORM-маппинг: отель и виза переживают запись и чтение без потерь."""

    original = city_profile(
        "ALA",
        hotel_per_night=Money(amount=Decimal("5000")),
        visas=(visa_policy(fee_per_person=Money(amount=Decimal("40.50"))),),
    )
    instance = SqliteCatalog(tmp_path / "catalog.sqlite3")
    await instance.initialize()
    await instance.upsert(CatalogData(cities=(original,), links=()))
    try:
        restored = await instance.get("ALA")
    finally:
        await instance.dispose()
    assert restored == original


async def test_upsert_replaces_visas_wholesale(tmp_path: Path) -> None:
    """Тестит, что повторный upsert заменяет визы, а не копит их."""

    instance = SqliteCatalog(tmp_path / "catalog.sqlite3")
    await instance.initialize()
    await instance.upsert(
        CatalogData(cities=(city_profile("ALA", visas=(visa_policy(),)),), links=())
    )
    await instance.upsert(CatalogData(cities=(city_profile("ALA"),), links=()))
    try:
        restored = await instance.get("ALA")
    finally:
        await instance.dispose()
    assert restored is not None
    assert restored.visas == ()


async def test_upsert_replaces_existing_profile(tmp_path: Path) -> None:
    """Тестит семантику upsert, на которую опирается повторный импорт городов."""

    instance = SqliteCatalog(tmp_path / "catalog.sqlite3")
    await instance.initialize()
    await instance.upsert(
        CatalogData(cities=(city_profile("PAR", tourism_score=0.1),), links=())
    )
    await instance.upsert(
        CatalogData(cities=(city_profile("PAR", tourism_score=0.8),), links=())
    )
    try:
        profiles = await instance.list_by_tourism()
    finally:
        await instance.dispose()
    assert len(profiles) == 1
    assert profiles[0].tourism_score == 0.8


async def test_list_by_tourism_orders_score_desc_then_code(
    catalog: SqliteCatalog,
) -> None:
    """Тестит сортировку: оценка по убыванию, при равенстве — код города по алфавиту."""

    profiles = await catalog.list_by_tourism()
    assert [profile.city_code for profile in profiles] == ["PAR", "ROM", "BER"]


async def test_list_by_tourism_applies_limit(catalog: SqliteCatalog) -> None:
    """Тестит, что limit отрезает верхушку уже отсортированного списка."""

    profiles = await catalog.list_by_tourism(limit=2)
    assert [profile.city_code for profile in profiles] == ["PAR", "ROM"]


async def test_list_by_tourism_filters_by_min_score(catalog: SqliteCatalog) -> None:
    """Тестит, что min_tourism_score отсекает города с оценкой ниже порога."""

    profiles = await catalog.list_by_tourism(min_tourism_score=0.5)
    assert [profile.city_code for profile in profiles] == ["PAR", "ROM"]


async def test_list_by_tourism_combines_filter_and_limit(
    catalog: SqliteCatalog,
) -> None:
    """Тестит совместную работу фильтра и лимита в одном запросе."""

    profiles = await catalog.list_by_tourism(limit=1, min_tourism_score=0.0)
    assert [profile.city_code for profile in profiles] == ["PAR"]


async def test_list_by_tourism_empty_catalog(tmp_path: Path) -> None:
    """Тестит, что пустой каталог отдаёт пустой кортеж без ошибок."""

    instance = SqliteCatalog(tmp_path / "empty.sqlite3")
    await instance.initialize()
    try:
        assert await instance.list_by_tourism() == ()
        assert await instance.list_by_tourism(limit=5, min_tourism_score=0.1) == ()
    finally:
        await instance.dispose()


async def test_get_many_orders_and_skips_unknown(catalog: SqliteCatalog) -> None:
    """Тестит пакетную загрузку: сортировка по коду, отсутствующие пропускаются."""

    profiles = await catalog.get_many(("BER", "XXX", "PAR"))
    assert [profile.city_code for profile in profiles] == ["BER", "PAR"]


async def test_connecting_cities_intersects_links(tmp_path: Path) -> None:
    """Тестит пересечение топологии: город B должен иметь рейсы из A и в C."""

    links = (
        FlightLink(origin="MOW", destination="BKK"),
        FlightLink(origin="MOW", destination="SIN"),
        FlightLink(origin="BKK", destination="TYO"),
        FlightLink(origin="SIN", destination="TYO"),
        FlightLink(origin="PAR", destination="TYO"),  # не из MOW — не подходит
    )
    instance = SqliteCatalog(tmp_path / "catalog.sqlite3")
    await instance.initialize()
    await instance.upsert(CatalogData(cities=(), links=links))
    await instance.upsert(CatalogData(cities=(), links=links))  # дубликаты игнорируются
    try:
        assert await instance.connecting_cities("MOW", "TYO") == ("BKK", "SIN")
        assert await instance.connecting_cities("MOW", "PAR") == ()
    finally:
        await instance.dispose()


async def test_initialize_rejects_retired_schema(tmp_path: Path) -> None:
    """Тестит, что каталог старой JSON-схемы отвергается с понятной ошибкой."""

    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE city_profiles (city_code TEXT PRIMARY KEY,"
            " profile_json TEXT NOT NULL); PRAGMA user_version = 1;"
        )
    instance = SqliteCatalog(path)
    try:
        with pytest.raises(RuntimeError, match="retired JSON-blob schema"):
            await instance.initialize()
    finally:
        await instance.dispose()
