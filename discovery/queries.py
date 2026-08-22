"""Deterministic search-query generation from a structured request.

Pure function of the request: same input always yields the same queries.
No hard-coded cities; geography flows from the request fields.
"""

from __future__ import annotations

from discovery.models import DiscoveryRequest

MAX_QUERIES = 5


def build_queries(request: DiscoveryRequest) -> list[str]:
    """Compose query strings for the provider.

    Variant A: "[station_type] [genre] radio stations [city] [region] [country]"
    Variant B (only when intent parts exist): "... accepting music submissions"
    Variant C (only when geography exists): "radio stations [geography] contact"
    """
    head_parts = [request.station_type, request.genre]
    head = " ".join(p for p in head_parts if p).strip()
    subject = f"{head} radio stations".strip() if head else "radio stations"

    geo_parts = [request.city, request.state_or_region, request.country]
    geo = " ".join(p for p in geo_parts if p).strip()

    queries: list[str] = []

    base = f"{subject} {geo}".strip()
    queries.append(base)

    has_intent = bool(head)
    if has_intent:
        variant_b = f"{subject} accepting music submissions {geo}".strip()
        variant_b = " ".join(variant_b.split())
        queries.append(variant_b)

    if geo:
        variant_c = f"radio stations {geo} contact".strip()
        queries.append(" ".join(variant_c.split()))

    deduped: list[str] = []
    for q in queries:
        collapsed = " ".join(q.split())
        if collapsed and collapsed not in deduped:
            deduped.append(collapsed)
    return deduped[:MAX_QUERIES]
