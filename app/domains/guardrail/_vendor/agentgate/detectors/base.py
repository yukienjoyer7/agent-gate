"""Detector base class and shared helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..schemas import ActionRequest, SensitiveEntity


@dataclass
class Finding:
    """What a detector returns: the entities it found plus a contribution to risk.

    risk_contribution is in [0, 1]; the decision engine combines contributions
    across detectors.
    """

    detector: str
    entities: list[SensitiveEntity] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    risk_contribution: float = 0.0
    tags: set[str] = field(default_factory=set)  # e.g. {"source_code", "external_send"}

    @property
    def triggered(self) -> bool:
        return bool(self.entities) or self.risk_contribution > 0


def truncate(text: str, n: int = 48) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


class Detector:
    """Base detector. Subclasses implement ``scan``."""

    name: str = "detector"

    def scan(self, req: ActionRequest) -> Finding:  # pragma: no cover - interface
        raise NotImplementedError

    # convenience for subclasses
    def _finding(self, **kwargs: Any) -> Finding:
        kwargs.setdefault("detector", self.name)
        return Finding(**kwargs)
