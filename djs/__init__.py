"""DJ intelligence package (Phase 4)."""

from djs.dedupe import (  # noqa: F401
    deduplicate_djs,
    dj_fingerprints,
    merge_dj_records,
)
from djs.service import (  # noqa: F401
    DJ_CHANNEL_ROUTE_CLASS,
    DJ_CHANNEL_ROUTE_LABEL,
    DJ_CHANNEL_TYPES,
    create_dj,
    delete_dj,
    dj_detail,
    dj_identity_key,
    dj_outreach,
    get_dj,
    ingest_dj_discovery,
    list_djs,
)

__all__ = [
    "DJ_CHANNEL_TYPES", "DJ_CHANNEL_ROUTE_CLASS", "DJ_CHANNEL_ROUTE_LABEL",
    "create_dj", "delete_dj", "dj_detail", "dj_identity_key", "dj_outreach",
    "get_dj", "list_djs", "ingest_dj_discovery",
    "deduplicate_djs", "dj_fingerprints", "merge_dj_records",
]