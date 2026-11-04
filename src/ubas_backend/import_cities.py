"""Offline importer of city tourism scores from the OpenTripMap API.

For every reference city the importer resolves the city center through the
OpenTripMap GeoNames endpoint, counts notable sights in a fixed radius and
normalizes the weighted counts into a 0..1 ``tourism_score`` shared by the
whole fetched set. Results are stored as a reviewable JSON snapshot and
upserted into the SQLite catalog as ``CityProfile`` rows without visa
policies: verified entry rules are filled in by a separate manual step.

API access requires a free non-commercial key from https://opentripmap.com
(5000 requests per day, 10 requests per second).
"""

import argparse
import asyncio
import json
import math
import os
import ssl
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx

from ubas_backend.routing.schemas import CityProfile
from ubas_backend.routing.storage import CatalogData, SqliteCatalog

API_BASE_URL = "https://api.opentripmap.com/0.1/en"
USER_AGENT = "ubas-backend/0.1 (study project)"
PAGE_SIZE = 500
MAX_PAGES_PER_CITY = 6
SEARCH_RADIUS_METERS = 10_000
REQUEST_INTERVAL_SECONDS = 0.15
REQUEST_TIMEOUT_SECONDS = 30
REQUEST_ATTEMPTS = 5
RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
MIN_NOTABLE_RATE = 3
LANDMARK_RATE = 4
LANDMARK_WEIGHT = 3
SCORE_FLOOR = 0.05
API_KEY_ENV = "UBAS_OPENTRIPMAP_API_KEY"
CATALOG_PATH_ENV = "UBAS_CATALOG_PATH"
CA_BUNDLE_ENV = "UBAS_CA_BUNDLE"
SNAPSHOT_DEFAULT = Path("data/city_profiles.json")


@dataclass(frozen=True)
class CitySpec:
    """Catalog reference: IATA city code plus a GeoNames lookup name."""

    city_code: str
    name: str
    country_code: str


@dataclass(frozen=True)
class CityLocation:
    latitude: float
    longitude: float
    timezone: str
    population: int | None


@dataclass(frozen=True)
class SightCounts:
    notable: int
    landmarks: int


@dataclass(frozen=True)
class CityRecord:
    """A city ready for the snapshot: profile plus the raw counters."""

    profile: CityProfile
    counts: SightCounts
    population: int | None


# Curated list of major international-destination cities (roughly the yearly
# "most visited cities" rankings). Order only affects fetch order, not scores.
TOP_CITIES: tuple[CitySpec, ...] = (
    CitySpec("LON", "London", "GB"),
    CitySpec("MAN", "Manchester", "GB"),
    CitySpec("EDI", "Edinburgh", "GB"),
    CitySpec("DUB", "Dublin", "IE"),
    CitySpec("PAR", "Paris", "FR"),
    CitySpec("NCE", "Nice", "FR"),
    CitySpec("LYS", "Lyon", "FR"),
    CitySpec("BRU", "Brussels", "BE"),
    CitySpec("AMS", "Amsterdam", "NL"),
    CitySpec("BER", "Berlin", "DE"),
    CitySpec("MUC", "Munich", "DE"),
    CitySpec("FRA", "Frankfurt", "DE"),
    CitySpec("ZRH", "Zurich", "CH"),
    CitySpec("GVA", "Geneva", "CH"),
    CitySpec("VIE", "Vienna", "AT"),
    CitySpec("PRG", "Prague", "CZ"),
    CitySpec("WAW", "Warsaw", "PL"),
    CitySpec("KRK", "Krakow", "PL"),
    CitySpec("BUD", "Budapest", "HU"),
    CitySpec("ZAG", "Zagreb", "HR"),
    CitySpec("SOF", "Sofia", "BG"),
    CitySpec("BUH", "Bucharest", "RO"),
    CitySpec("ATH", "Athens", "GR"),
    CitySpec("ROM", "Rome", "IT"),
    CitySpec("MIL", "Milan", "IT"),
    CitySpec("VCE", "Venice", "IT"),
    CitySpec("FLR", "Florence", "IT"),
    CitySpec("NAP", "Naples", "IT"),
    CitySpec("MAD", "Madrid", "ES"),
    CitySpec("BCN", "Barcelona", "ES"),
    CitySpec("LIS", "Lisbon", "PT"),
    CitySpec("OPO", "Porto", "PT"),
    CitySpec("IST", "Istanbul", "TR"),
    CitySpec("AYT", "Antalya", "TR"),
    CitySpec("MOW", "Moscow", "RU"),
    CitySpec("LED", "Saint Petersburg", "RU"),
    CitySpec("KZN", "Kazan", "RU"),
    CitySpec("SVX", "Yekaterinburg", "RU"),
    CitySpec("OVB", "Novosibirsk", "RU"),
    CitySpec("MSQ", "Minsk", "BY"),
    CitySpec("IEV", "Kyiv", "UA"),
    CitySpec("RIX", "Riga", "LV"),
    CitySpec("VNO", "Vilnius", "LT"),
    CitySpec("TLL", "Tallinn", "EE"),
    CitySpec("NYC", "New York", "US"),
    CitySpec("BOS", "Boston", "US"),
    CitySpec("WAS", "Washington", "US"),
    CitySpec("CHI", "Chicago", "US"),
    CitySpec("MIA", "Miami", "US"),
    CitySpec("ORL", "Orlando", "US"),
    CitySpec("LAS", "Las Vegas", "US"),
    CitySpec("LAX", "Los Angeles", "US"),
    CitySpec("SFO", "San Francisco", "US"),
    CitySpec("YTO", "Toronto", "CA"),
    CitySpec("YMQ", "Montreal", "CA"),
    CitySpec("YVR", "Vancouver", "CA"),
    CitySpec("MEX", "Mexico City", "MX"),
    CitySpec("CUN", "Cancun", "MX"),
    CitySpec("HAV", "Havana", "CU"),
    CitySpec("PUJ", "Punta Cana", "DO"),
    CitySpec("BOG", "Bogota", "CO"),
    CitySpec("LIM", "Lima", "PE"),
    CitySpec("SCL", "Santiago", "CL"),
    CitySpec("BUE", "Buenos Aires", "AR"),
    CitySpec("SAO", "Sao Paulo", "BR"),
    CitySpec("RIO", "Rio de Janeiro", "BR"),
    CitySpec("TYO", "Tokyo", "JP"),
    CitySpec("OSA", "Osaka", "JP"),
    CitySpec("SEL", "Seoul", "KR"),
    CitySpec("PUS", "Busan", "KR"),
    CitySpec("BJS", "Beijing", "CN"),
    CitySpec("SHA", "Shanghai", "CN"),
    CitySpec("CAN", "Guangzhou", "CN"),
    CitySpec("SZX", "Shenzhen", "CN"),
    CitySpec("CTU", "Chengdu", "CN"),
    CitySpec("HKG", "Hong Kong", "HK"),
    CitySpec("MFM", "Macau", "MO"),
    CitySpec("TPE", "Taipei", "TW"),
    CitySpec("SIN", "Singapore", "SG"),
    CitySpec("KUL", "Kuala Lumpur", "MY"),
    CitySpec("BKK", "Bangkok", "TH"),
    CitySpec("HKT", "Phuket", "TH"),
    CitySpec("CNX", "Chiang Mai", "TH"),
    CitySpec("HAN", "Hanoi", "VN"),
    CitySpec("SGN", "Ho Chi Minh City", "VN"),
    CitySpec("JKT", "Jakarta", "ID"),
    CitySpec("DPS", "Denpasar", "ID"),
    CitySpec("MNL", "Manila", "PH"),
    CitySpec("CMB", "Colombo", "LK"),
    CitySpec("KTM", "Kathmandu", "NP"),
    CitySpec("DEL", "Delhi", "IN"),
    CitySpec("BOM", "Mumbai", "IN"),
    CitySpec("BLR", "Bengaluru", "IN"),
    CitySpec("DXB", "Dubai", "AE"),
    CitySpec("AUH", "Abu Dhabi", "AE"),
    CitySpec("DOH", "Doha", "QA"),
    CitySpec("JED", "Jeddah", "SA"),
    CitySpec("TLV", "Tel Aviv", "IL"),
    CitySpec("AMM", "Amman", "JO"),
    CitySpec("BEY", "Beirut", "LB"),
    CitySpec("CAI", "Cairo", "EG"),
    CitySpec("RAK", "Marrakesh", "MA"),
    CitySpec("CMN", "Casablanca", "MA"),
    CitySpec("CPT", "Cape Town", "ZA"),
    CitySpec("JNB", "Johannesburg", "ZA"),
    CitySpec("NBO", "Nairobi", "KE"),
    CitySpec("ZNZ", "Zanzibar", "TZ"),
    CitySpec("MLE", "Male", "MV"),
    CitySpec("TAS", "Tashkent", "UZ"),
    CitySpec("ALA", "Almaty", "KZ"),
    CitySpec("BAK", "Baku", "AZ"),
    CitySpec("TBS", "Tbilisi", "GE"),
    CitySpec("EVN", "Yerevan", "AM"),
)


class OpenTripMapError(RuntimeError):
    """The OpenTripMap API failed or returned an unexpected response."""


class OpenTripMapClient:
    """Small synchronous client honoring the free-tier request limits."""

    def __init__(self, http: httpx.Client, api_key: str) -> None:
        self._http = http
        self._api_key = api_key
        self._last_request_at = 0.0

    def city_location(self, name: str, country_code: str) -> CityLocation | None:
        payload = self._request(
            "/places/geoname", {"name": name, "country": country_code}
        )
        if not isinstance(payload, dict) or payload.get("status") != "OK":
            return None
        latitude = payload.get("lat")
        longitude = payload.get("lon")
        timezone = payload.get("timezone")
        population = payload.get("population")
        if not (
            isinstance(latitude, (int, float))
            and isinstance(longitude, (int, float))
            and isinstance(timezone, str)
            and (population is None or isinstance(population, int))
        ):
            return None
        return CityLocation(
            latitude=float(latitude),
            longitude=float(longitude),
            timezone=timezone,
            population=population,
        )

    def sight_counts(self, latitude: float, longitude: float) -> SightCounts:
        """Count notable and landmark sights within the search radius."""

        notable = 0
        landmarks = 0
        for page in range(MAX_PAGES_PER_CITY):
            payload = self._request(
                "/places/radius",
                {
                    "radius": str(SEARCH_RADIUS_METERS),
                    "lon": f"{longitude}",
                    "lat": f"{latitude}",
                    "kinds": "interesting_places",
                    "rate": str(MIN_NOTABLE_RATE),
                    "limit": str(PAGE_SIZE),
                    "offset": str(page * PAGE_SIZE),
                    "format": "json",
                },
            )
            if not isinstance(payload, list):
                raise OpenTripMapError("radius endpoint returned a non-list")
            for place in payload:
                if not isinstance(place, dict):
                    raise OpenTripMapError("radius endpoint returned a non-object")
                rate = place.get("rate")
                if not isinstance(rate, int):
                    continue
                if rate >= LANDMARK_RATE:
                    landmarks += 1
                else:
                    notable += 1
            if len(payload) < PAGE_SIZE:
                break
        return SightCounts(notable=notable, landmarks=landmarks)

    def _request(self, path: str, params: dict[str, str]) -> object:
        last_error: Exception | None = None
        for attempt in range(REQUEST_ATTEMPTS):
            self._throttle()
            try:
                response = self._http.get(
                    path, params={**params, "apikey": self._api_key}
                )
                response.raise_for_status()
                return cast("object", response.json())
            except httpx.HTTPStatusError as error:
                last_error = error
                if error.response.status_code not in RETRY_STATUS_CODES:
                    raise OpenTripMapError(
                        f"{path} failed with HTTP {error.response.status_code}"
                    ) from error
            except httpx.TransportError as error:
                last_error = error
            except ValueError as error:
                # A 200 response with a non-JSON body (HTML error page of a
                # proxy or a captive portal) is treated as retryable garbage.
                last_error = error
            if attempt + 1 < REQUEST_ATTEMPTS:
                time.sleep(2**attempt)
        raise OpenTripMapError(f"{path} failed: {last_error}") from last_error

    def _throttle(self) -> None:
        wait = REQUEST_INTERVAL_SECONDS - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()


def tourism_scores(counts: dict[str, SightCounts]) -> dict[str, float]:
    """Normalize weighted sight counts into 0..1 across the fetched set.

    Landmarks (world-famous sights) weigh more than ordinary notable ones;
    ``log1p`` softens the order-of-magnitude gap between megacities and
    compact historic centers, then min-max scaling spreads the set over
    ``[SCORE_FLOOR, 1.0]``.
    """

    weighted = {
        city_code: count.notable + LANDMARK_WEIGHT * count.landmarks
        for city_code, count in counts.items()
    }
    logged = {city_code: math.log1p(value) for city_code, value in weighted.items()}
    span = max(logged.values()) - min(logged.values())
    if span == 0:
        return {city_code: 1.0 for city_code in logged}
    lowest = min(logged.values())
    return {
        city_code: SCORE_FLOOR + (1 - SCORE_FLOOR) * (value - lowest) / span
        for city_code, value in logged.items()
    }


def fetch_cities(
    client: OpenTripMapClient, specs: tuple[CitySpec, ...]
) -> tuple[dict[str, tuple[CityLocation, SightCounts]], tuple[str, ...]]:
    """Fetch locations and sight counts for every spec, skipping failures."""

    fetched: dict[str, tuple[CityLocation, SightCounts]] = {}
    skipped: list[str] = []
    for spec in specs:
        try:
            location = client.city_location(spec.name, spec.country_code)
            if location is None:
                raise OpenTripMapError("geoname lookup miss")
            count = client.sight_counts(location.latitude, location.longitude)
        except OpenTripMapError as error:
            skipped.append(spec.city_code)
            print(f"skip {spec.city_code} {spec.name}: {error}", file=sys.stderr)
            continue
        fetched[spec.city_code] = (location, count)
        print(
            f"{spec.city_code} {spec.name}: "
            f"{count.notable} notable, {count.landmarks} landmarks"
        )
    return fetched, tuple(skipped)


def assemble_records(
    specs: tuple[CitySpec, ...],
    fetched: dict[str, tuple[CityLocation, SightCounts]],
    previous: dict[str, CityRecord],
) -> tuple[CityRecord, ...]:
    """Build snapshot records for every city with known counts.

    Freshly fetched cities take their attributes from the API response;
    cities present only in the previous snapshot keep theirs with an
    updated score. Cities absent from both are dropped.
    """

    counts: dict[str, SightCounts] = {
        city_code: count for city_code, (_, count) in fetched.items()
    }
    counts.update({city_code: record.counts for city_code, record in previous.items()})
    scores = tourism_scores(counts)
    records: list[CityRecord] = []
    for spec in specs:
        count = counts.get(spec.city_code)
        if count is None:
            continue
        if spec.city_code in fetched:
            location = fetched[spec.city_code][0]
            profile = CityProfile(
                city_code=spec.city_code,
                name=spec.name,
                country_code=spec.country_code,
                timezone=location.timezone,
                tourism_score=scores[spec.city_code],
                visas=(),
            )
            population = location.population
        else:
            prior = previous[spec.city_code]
            profile = prior.profile.model_copy(
                update={"tourism_score": scores[spec.city_code]}
            )
            population = prior.population
        records.append(CityRecord(profile=profile, counts=count, population=population))
    return tuple(records)


def write_snapshot(path: Path, records: tuple[CityRecord, ...]) -> None:
    document = {
        "source": "OpenTripMap",
        "generated_at": datetime.now(UTC).isoformat(),
        "scoring": {
            "radius_meters": SEARCH_RADIUS_METERS,
            "kinds": "interesting_places",
            "min_rate": MIN_NOTABLE_RATE,
            "landmark_rate": LANDMARK_RATE,
            "landmark_weight": LANDMARK_WEIGHT,
            "score_floor": SCORE_FLOOR,
        },
        "cities": [
            {
                "profile": json.loads(record.profile.model_dump_json()),
                "notable": record.counts.notable,
                "landmarks": record.counts.landmarks,
                "population": record.population,
            }
            for record in records
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"snapshot written: {path} ({len(records)} cities)")


def read_snapshot(path: Path) -> tuple[CityRecord, ...]:
    document = json.loads(path.read_text(encoding="utf-8"))
    entries = cast("list[dict[str, object]]", document["cities"])
    records: list[CityRecord] = []
    for entry in entries:
        profile_data = entry.get("profile")
        if not isinstance(profile_data, dict):
            raise OpenTripMapError("snapshot record without a profile object")
        records.append(
            CityRecord(
                profile=CityProfile.model_validate(profile_data),
                counts=SightCounts(
                    notable=_snapshot_int(entry, "notable"),
                    landmarks=_snapshot_int(entry, "landmarks"),
                ),
                population=_snapshot_population(entry),
            )
        )
    return tuple(records)


def _snapshot_int(entry: dict[str, object], field: str) -> int:
    value = entry.get(field)
    if not isinstance(value, int):
        raise OpenTripMapError(f"snapshot field {field!r} is not an integer")
    return value


def _snapshot_population(entry: dict[str, object]) -> int | None:
    value = entry.get("population")
    if value is None or isinstance(value, int):
        return value
    raise OpenTripMapError("snapshot field 'population' is not an integer")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import city tourism scores from OpenTripMap into the catalog"
    )
    parser.add_argument(
        "--api-key",
        default="",
        help=f"OpenTripMap API key; defaults to ${API_KEY_ENV}",
    )
    parser.add_argument(
        "--catalog",
        default=os.environ.get(CATALOG_PATH_ENV, ""),
        help=f"SQLite catalog path; defaults to ${CATALOG_PATH_ENV}",
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=SNAPSHOT_DEFAULT,
        help=f"JSON snapshot path (default: {SNAPSHOT_DEFAULT})",
    )
    parser.add_argument(
        "--from-snapshot",
        type=Path,
        default=None,
        help="skip the API and import city profiles from this snapshot",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="fetch only the first N cities (smoke runs)",
    )
    parser.add_argument(
        "--city",
        action="append",
        default=None,
        metavar="CODE",
        help="fetch only the given IATA city code (repeatable); merges into "
        "an existing snapshot and renormalizes all scores",
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    if args.limit is not None and args.limit < 1:
        print("--limit must be at least 1", file=sys.stderr)
        return 2
    if args.from_snapshot is not None:
        if args.city or args.limit is not None:
            print(
                "--from-snapshot cannot be combined with --city or --limit",
                file=sys.stderr,
            )
            return 2
        try:
            records = read_snapshot(args.from_snapshot)
        except (OSError, ValueError, KeyError, OpenTripMapError) as error:
            print(
                f"cannot read snapshot {args.from_snapshot}: {error}", file=sys.stderr
            )
            return 1
        print(f"loaded {len(records)} city profiles from {args.from_snapshot}")
    else:
        api_key = args.api_key or os.environ.get(API_KEY_ENV, "")
        if not api_key:
            print(
                f"missing API key: pass --api-key or set ${API_KEY_ENV}",
                file=sys.stderr,
            )
            return 2
        if args.city and args.limit is not None:
            print("--city and --limit are mutually exclusive", file=sys.stderr)
            return 2
        specs = TOP_CITIES
        previous: dict[str, CityRecord] = {}
        if args.city:
            wanted = {code.upper() for code in args.city}
            unknown = sorted(wanted - {spec.city_code for spec in TOP_CITIES})
            if unknown:
                print(f"unknown city codes: {', '.join(unknown)}", file=sys.stderr)
                return 2
            specs = tuple(spec for spec in TOP_CITIES if spec.city_code in wanted)
            if args.snapshot.is_file():
                previous = {
                    record.profile.city_code: record
                    for record in read_snapshot(args.snapshot)
                }
                print(f"merging into {args.snapshot} ({len(previous)} cities)")
            else:
                print(
                    f"snapshot {args.snapshot} not found; scores will "
                    "cover only the fetched cities",
                    file=sys.stderr,
                )
        elif args.limit is not None:
            specs = specs[: args.limit]
        ca_bundle = os.environ.get(CA_BUNDLE_ENV, "") or os.environ.get(
            "SSL_CERT_FILE", ""
        )
        verify: ssl.SSLContext | bool = True
        if ca_bundle:
            verify = ssl.create_default_context(cafile=ca_bundle)
            # Corporate TLS inspectors often sign chains without the
            # Authority Key Identifier extension, which Python 3.13+
            # rejects in strict verification mode.
            verify.verify_flags &= ~ssl.VERIFY_X509_STRICT
        with httpx.Client(
            base_url=API_BASE_URL,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT},
            verify=verify,
        ) as http_client:
            fetched, skipped = fetch_cities(
                OpenTripMapClient(http_client, api_key), specs
            )
        records = assemble_records(TOP_CITIES, fetched, previous)
        if len(records) < 2:
            print(
                "fewer than two cities available; nothing to normalize",
                file=sys.stderr,
            )
            return 1
        write_snapshot(args.snapshot, records)
        if skipped:
            print(f"skipped cities: {', '.join(skipped)}", file=sys.stderr)
    if not args.catalog:
        print(
            f"catalog path empty; set --catalog or ${CATALOG_PATH_ENV} "
            "to write the catalog",
            file=sys.stderr,
        )
        return 0
    catalog = SqliteCatalog(Path(args.catalog))

    async def load_catalog() -> None:
        await catalog.initialize()
        await catalog.upsert(
            CatalogData(cities=tuple(record.profile for record in records), links=())
        )

    asyncio.run(load_catalog())
    print(f"catalog {args.catalog}: upserted {len(records)} city profiles")
    return 0


def main() -> None:
    raise SystemExit(run(parse_args()))


if __name__ == "__main__":
    main()
