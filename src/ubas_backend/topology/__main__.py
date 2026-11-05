"""Collect a topology snapshot, or import a reviewed snapshot into SQLite."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

import httpx
from pydantic import TypeAdapter

from ubas_backend.routing.schemas import Code
from ubas_backend.routing.storage import SqliteCatalog
from ubas_backend.topology.collector import collect
from ubas_backend.topology.geography import Geography
from ubas_backend.topology.models import AIRPORTS_URL, CITIES_URL, Snapshot
from ubas_backend.topology.snapshots import combine_snapshots
from ubas_backend.topology.source import FlyWhereTo, read_body


def save_snapshot(snapshot: Snapshot, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as temporary:
        temp_path = Path(temporary.name)
        try:
            temporary.write(snapshot.model_dump_json(indent=2).encode("utf-8"))
            temporary.flush()
            os.fsync(temporary.fileno())
        except BaseException:
            temporary.close()
            temp_path.unlink()
            raise
    try:
        # Publish atomically, without overwriting another run's snapshot.
        os.link(temp_path, path)
    finally:
        temp_path.unlink()


async def download(
    city_codes: tuple[str, ...], *, max_airports: int, interval: float
) -> Snapshot:
    async with (
        asyncio.timeout(600),
        httpx.AsyncClient(
            timeout=30,
            headers={"User-Agent": "UBAS-topology/0.1"},
            follow_redirects=False,
        ) as client,
    ):
        raw = []
        for url in (AIRPORTS_URL, CITIES_URL):
            async with client.stream("GET", url) as response:
                raw.append(json.loads(await read_body(response, 12_000_000)))
        geography = Geography(*raw)
        source = FlyWhereTo(client, interval_seconds=interval)
        try:
            await source.initialize()
            return await collect(
                geography,
                source,
                city_codes,
                max_airports=max_airports,
                progress=lambda message: print(message, file=sys.stderr, flush=True),
            )
        finally:
            await source.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--cities", nargs="+", help="City IATA codes, e.g. MOW SVX ALA")
    modes.add_argument("--cities-file", type=Path, help="JSON array of city IATA codes")
    modes.add_argument(
        "--merge-snapshots",
        nargs="+",
        type=Path,
        help="Combine disjoint collections without network access",
    )
    modes.add_argument(
        "--from-snapshot", type=Path, help="Import without network access"
    )
    parser.add_argument("--snapshot", type=Path, help="New JSON snapshot file")
    parser.add_argument(
        "--catalog", type=Path, help="SQLite catalog to create or update"
    )
    parser.add_argument("--max-airports", type=int, default=100)
    parser.add_argument("--interval", type=float, default=1)
    args = parser.parse_args()
    input_or_output = args.from_snapshot or args.snapshot
    if (
        args.merge_snapshots
        and args.catalog
        and any(
            path.resolve() == args.catalog.resolve() for path in args.merge_snapshots
        )
    ):
        parser.error("Snapshot and catalog must be different files")
    if (
        args.catalog
        and input_or_output
        and (args.catalog.resolve() == input_or_output.resolve())
    ):
        parser.error("Snapshot and catalog must be different files")
    if (
        args.max_airports < 1
        or args.max_airports > 1000
        or not 0.25 <= args.interval <= 60
    ):
        parser.error("max-airports must be 1..1000; interval must be 0.25..60 seconds")
    if args.from_snapshot:
        if args.snapshot or not args.catalog:
            parser.error("--from-snapshot requires --catalog and excludes --snapshot")
        snapshot = Snapshot.model_validate_json(args.from_snapshot.read_bytes())
    elif args.merge_snapshots:
        if args.snapshot is None or args.snapshot.exists():
            parser.error("--merge-snapshots requires a new --snapshot path")
        snapshot = combine_snapshots(
            tuple(
                Snapshot.model_validate_json(path.read_bytes())
                for path in args.merge_snapshots
            )
        )
        save_snapshot(snapshot, args.snapshot)
    else:
        if args.snapshot is None or args.snapshot.exists():
            parser.error("Collection requires a new --snapshot path")
        codes = (
            TypeAdapter(tuple[Code, ...]).validate_json(args.cities_file.read_bytes())
            if args.cities_file
            else tuple(code.upper() for code in args.cities)
        )
        snapshot = asyncio.run(
            download(
                codes,
                max_airports=args.max_airports,
                interval=args.interval,
            )
        )
        save_snapshot(snapshot, args.snapshot)
    if args.catalog:
        args.catalog.parent.mkdir(parents=True, exist_ok=True)
        catalog = SqliteCatalog(args.catalog)
        try:
            catalog.initialize()
            catalog.replace_topology(snapshot.catalog_data(), snapshot.covered_cities)
        finally:
            catalog.close()
    print(
        json.dumps(
            {
                "covered_cities": snapshot.covered_cities,
                "airports_fetched": len(snapshot.observations),
                "city_links": len(snapshot.catalog_data().links),
                "geography_cities": len(snapshot.cities),
                "catalog": str(args.catalog) if args.catalog else None,
            }
        )
    )


if __name__ == "__main__":
    main()
