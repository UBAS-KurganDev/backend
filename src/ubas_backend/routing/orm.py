"""Compatibility names for the existing JSON catalog mapping.

The tourism importer deliberately does not replace the catalog schema.
New persistence code should import routing.catalog_models directly.
"""

from ubas_backend.routing.catalog_models import (
    CatalogBase as Base,
)
from ubas_backend.routing.catalog_models import (
    CityRow as CityProfileRow,
)
from ubas_backend.routing.catalog_models import (
    FlightLinkRow,
)

__all__ = ["Base", "CityProfileRow", "FlightLinkRow"]
