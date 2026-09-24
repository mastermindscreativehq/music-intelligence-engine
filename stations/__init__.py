"""Station intelligence package.

Adapter-agnostic service helpers over the shared intelligence repository,
mirroring ``djs``. Station discovery intake lives here; the read-path
projection stays in ``backend.contracts``.
"""

from stations.service import (  # noqa: F401
    DEFAULT_SOURCE,
    ingest_station_discovery,
)

__all__ = ["DEFAULT_SOURCE", "ingest_station_discovery"]
