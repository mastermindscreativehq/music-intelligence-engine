"""Phase 3: music opportunity intelligence layer.

- ``scoring``: deterministic, explainable 0..100 scoring model (pure).
- ``service``: orchestration over the repository (rank/filter, outreach
  awareness). Computes opportunities on the read path; it never writes
  storage, never sends, and never invents evidence.
"""

from opportunity.scoring import (
    ALREADY_CONTACTED_PENALTY,
    HIGH_THRESHOLD,
    MEDIUM_THRESHOLD,
    TIER_HIGH,
    TIER_LOW,
    TIER_MEDIUM,
    WEIGHTS,
    score_opportunity,
    tier_for,
)

__all__ = [
    "ALREADY_CONTACTED_PENALTY",
    "HIGH_THRESHOLD",
    "MEDIUM_THRESHOLD",
    "TIER_HIGH",
    "TIER_LOW",
    "TIER_MEDIUM",
    "WEIGHTS",
    "score_opportunity",
    "tier_for",
]
