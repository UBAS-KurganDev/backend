"""Shared fixtures: temporary SQLite catalogs seeded with known profiles."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.factories import city_profile
from ubas_backend.routing.storage import CatalogData, SqliteCatalog

# PAR and ROM share the top score on purpose: the ordering contract is
# "score descending, then city code ascending", so the tie is part of the test.
SEEDED_SCORES = {"PAR": 0.9, "ROM": 0.9, "BER": 0.4}


@pytest.fixture
async def catalog(tmp_path: Path) -> AsyncIterator[SqliteCatalog]:
    """An initialized catalog with three cities of known tourism scores."""

    instance = SqliteCatalog(tmp_path / "catalog.sqlite3")
    await instance.initialize()
    await instance.upsert(
        CatalogData(
            cities=tuple(
                city_profile(code, tourism_score=score)
                for code, score in SEEDED_SCORES.items()
            ),
            links=(),
        )
    )
    yield instance
    await instance.dispose()
