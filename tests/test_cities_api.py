"""Cities API: ordering, query filters, validation and error paths.

Requests go through ASGITransport inside the test's event loop; the app
lifespan (which needs Postgres) never runs — the catalog is attached to
``app.state`` directly, mirroring what the lifespan does.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from httpx import ASGITransport, AsyncClient

from tests.conftest import SEEDED_SCORES
from ubas_backend.main import create_app
from ubas_backend.routing.storage import SqliteCatalog


@asynccontextmanager
async def api_client(catalog: SqliteCatalog | None) -> AsyncIterator[AsyncClient]:
    app = create_app()
    app.state.catalog = catalog
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        yield client


async def test_list_cities_orders_by_tourism_score(catalog: SqliteCatalog) -> None:
    """Тестит, что список городов идёт по убыванию туристической оценки."""

    async with api_client(catalog) as client:
        response = await client.get("/api/v1/cities")
    assert response.status_code == 200
    body = response.json()
    assert [city["city_code"] for city in body] == ["PAR", "ROM", "BER"]
    assert body[0]["tourism_score"] == SEEDED_SCORES["PAR"]


async def test_list_cities_supports_limit_and_min_score(
    catalog: SqliteCatalog,
) -> None:
    """Тестит query-параметры limit и min_tourism_score по отдельности и вместе."""

    async with api_client(catalog) as client:
        limited = await client.get("/api/v1/cities?limit=2")
        filtered = await client.get("/api/v1/cities?min_tourism_score=0.5")
        combined = await client.get("/api/v1/cities?min_tourism_score=0.5&limit=1")
    assert [c["city_code"] for c in limited.json()] == ["PAR", "ROM"]
    assert [c["city_code"] for c in filtered.json()] == ["PAR", "ROM"]
    assert [c["city_code"] for c in combined.json()] == ["PAR"]


async def test_list_cities_rejects_out_of_range_query_params(
    catalog: SqliteCatalog,
) -> None:
    """Тестит валидацию: limit вне 1..500 и оценка вне 0..1 дают 422."""

    async with api_client(catalog) as client:
        for query in (
            "limit=0",
            "limit=501",
            "min_tourism_score=1.5",
            "min_tourism_score=-0.1",
        ):
            response = await client.get(f"/api/v1/cities?{query}")
            assert response.status_code == 422, query


async def test_get_city_returns_profile(catalog: SqliteCatalog) -> None:
    """Тестит точечный эндпоинт: известный код отдаёт профиль города."""

    async with api_client(catalog) as client:
        response = await client.get("/api/v1/cities/PAR")
    assert response.status_code == 200
    assert response.json()["city_code"] == "PAR"
    assert response.json()["timezone"] == "Europe/Paris"


async def test_get_city_rejects_unknown_and_malformed_codes(
    catalog: SqliteCatalog,
) -> None:
    """Тестит 404 для отсутствующего города и 422 для не-IATA кода."""

    async with api_client(catalog) as client:
        assert (await client.get("/api/v1/cities/XXX")).status_code == 404
        assert (await client.get("/api/v1/cities/par")).status_code == 422
        assert (await client.get("/api/v1/cities/PARS")).status_code == 422


async def test_cities_endpoints_answer_503_without_catalog() -> None:
    """Тестит, что без настроенного каталога эндпоинты городов отвечают 503."""

    async with api_client(None) as client:
        assert (await client.get("/api/v1/cities")).status_code == 503
        assert (await client.get("/api/v1/cities/PAR")).status_code == 503
