"""Risk scoring.

Combines detector risk contributions into a single [0, 1] score, then maps that
score (raised to any policy-imposed floor) into a RiskLevel band.

Combination uses a "noisy-OR": independent signals each chip away at the
probability of safety, so multiple weak signals accumulate but never exceed 1.0.
"""

from __future__ import annotations

from .schemas import RiskLevel

# Score thresholds for each band.
_BANDS = [
    (0.85, RiskLevel.CRITICAL),
    (0.6, RiskLevel.HIGH),
    (0.3, RiskLevel.MEDIUM),
    (0.0, RiskLevel.LOW),
]

_LEVEL_MIN_SCORE = {
    RiskLevel.LOW: 0.0,
    RiskLevel.MEDIUM: 0.3,
    RiskLevel.HIGH: 0.6,
    RiskLevel.CRITICAL: 0.85,
}


def combine(contributions: list[float]) -> float:
    """Noisy-OR combination of independent risk contributions."""
    safe = 1.0
    for c in contributions:
        c = max(0.0, min(1.0, c))
        safe *= (1.0 - c)
    return round(1.0 - safe, 4)


def score_to_level(score: float) -> RiskLevel:
    for threshold, level in _BANDS:
        if score >= threshold:
            return level
    return RiskLevel.LOW


def apply_floor(score: float, floor: RiskLevel) -> float:
    """Raise a score so it lands at least in the policy-required band."""
    return max(score, _LEVEL_MIN_SCORE[floor])
