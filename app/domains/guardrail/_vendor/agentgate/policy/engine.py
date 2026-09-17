"""Policy engine.

Loads domain *policy packs* (JSON rules) and matches them against an ActionRequest
plus the aggregated detector context. Rules are declarative so non-DS teammates can
read and extend them. The engine returns every triggered rule, the strongest decision
suggested, and a risk floor.

Rule schema (all match-* keys optional; a rule matches only if every present key
matches):

  {
    "id": "booking.external_payment_send",
    "description": "...",
    "domains":          ["booking_style"],        # req.domain in list
    "action_types":     ["BROWSER_SUBMIT"],        # req.action_type in list
    "risk_hints_any":   ["payment_related"],       # any hint present
    "tags_any":         ["payment_related"],       # any detector tag present
    "entity_kinds_any": ["CREDIT_CARD"],           # any detected entity kind present
    "target_systems_any": ["Gmail"],
    "min_confidence": null,                          # trigger when confidence <= value
    "decision": "NEED_APPROVAL",
    "risk_floor": "HIGH",
    "reason": "External customer payment message requires human approval"
  }
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..schemas import ActionRequest, Decision, RiskLevel

PACKS_DIR = Path(__file__).parent / "packs"

_DECISION_RANK = {
    Decision.ALLOW: 0,
    Decision.SANITIZE: 1,
    Decision.ASK_USER: 2,
    Decision.NEED_APPROVAL: 3,
    Decision.BLOCK: 4,
}
_RISK_RANK = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2, RiskLevel.CRITICAL: 3}
_LIST_CONDITIONS = {
    "domains",
    "action_types",
    "risk_hints_any",
    "tags_any",
    "entity_kinds_any",
    "target_systems_any",
}
_RULE_KEYS = {
    "id",
    "description",
    "reason",
    "decision",
    "risk_floor",
    "min_confidence",
    "requires_no_rollback",
    "_pack",
    *_LIST_CONDITIONS,
}


@dataclass
class PolicyResult:
    triggered: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    decision: Decision = Decision.ALLOW
    risk_floor: RiskLevel = RiskLevel.LOW

    def merge_rule(self, rule_id: str, reason: str, decision: Decision, risk_floor: RiskLevel) -> None:
        self.triggered.append(rule_id)
        if reason:
            self.reasons.append(reason)
        if _DECISION_RANK[decision] > _DECISION_RANK[self.decision]:
            self.decision = decision
        if _RISK_RANK[risk_floor] > _RISK_RANK[self.risk_floor]:
            self.risk_floor = risk_floor


@dataclass
class PolicyContext:
    """Aggregated detector signals the policy engine matches against."""

    tags: set[str] = field(default_factory=set)
    entity_kinds: set[str] = field(default_factory=set)


class PolicyEngine:
    def __init__(self, rules: list[dict[str, Any]] | None = None):
        self.rules: list[dict[str, Any]] = rules if rules is not None else load_packs()
        if rules is None and not self.rules:
            raise RuntimeError("AgentGate policy packs are missing or empty")
        _validate_rules(self.rules)

    def evaluate(self, req: ActionRequest, ctx: PolicyContext) -> PolicyResult:
        result = PolicyResult()
        for rule in self.rules:
            if self._matches(rule, req, ctx):
                result.merge_rule(
                    rule_id=rule["id"],
                    reason=rule.get("reason", rule.get("description", "")),
                    decision=Decision(rule.get("decision", "ALLOW")),
                    risk_floor=RiskLevel(rule.get("risk_floor", "LOW")),
                )
        return result

    @staticmethod
    def _matches(rule: dict[str, Any], req: ActionRequest, ctx: PolicyContext) -> bool:
        if "domains" in rule and req.domain not in rule["domains"]:
            return False
        if "action_types" in rule and req.action_type not in rule["action_types"]:
            return False
        if "risk_hints_any" in rule:
            # Detector-inferred tags count as risk hints too, so the guardrail does not
            # depend on the (untrusted) planner self-reporting its own risk.
            effective_hints = set(req.risk_hint) | ctx.tags
            if not (set(rule["risk_hints_any"]) & effective_hints):
                return False
        if "tags_any" in rule and not (set(rule["tags_any"]) & ctx.tags):
            return False
        if "entity_kinds_any" in rule and not (set(rule["entity_kinds_any"]) & ctx.entity_kinds):
            return False
        if "target_systems_any" in rule and req.target_system not in rule["target_systems_any"]:
            return False
        if rule.get("min_confidence") is not None and req.confidence > rule["min_confidence"]:
            return False
        if rule.get("requires_no_rollback") and req.rollback_available:
            return False
        return True


def load_packs(packs_dir: Path | None = None) -> list[dict[str, Any]]:
    """Load and concatenate every rule from every *.json pack in the packs dir."""
    packs_dir = packs_dir or PACKS_DIR
    rules: list[dict[str, Any]] = []
    for path in sorted(packs_dir.glob("*.json")):
        data = json.loads(path.read_text())
        for rule in data.get("rules", []):
            rule.setdefault("_pack", data.get("name", path.stem))
            rules.append(rule)
    return rules


def _validate_rules(rules: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("Policy rules must be objects")
        unknown = set(rule) - _RULE_KEYS
        if unknown:
            raise ValueError(f"Policy rule contains unknown keys: {sorted(unknown)}")
        rule_id = rule.get("id")
        if not isinstance(rule_id, str) or not rule_id:
            raise ValueError("Policy rules require a non-empty id")
        if rule_id in seen:
            raise ValueError(f"Duplicate policy rule id: {rule_id}")
        seen.add(rule_id)
        if "decision" not in rule or "risk_floor" not in rule:
            raise ValueError(f"Policy rule {rule_id!r} requires decision and risk_floor")
        Decision(rule["decision"])
        RiskLevel(rule["risk_floor"])
        for key in _LIST_CONDITIONS:
            value = rule.get(key)
            if value is not None and (
                not isinstance(value, list)
                or any(not isinstance(item, str) for item in value)
            ):
                raise ValueError(f"Policy rule {rule_id!r} field {key!r} must be a string list")
        confidence = rule.get("min_confidence")
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= confidence <= 1
        ):
            raise ValueError(f"Policy rule {rule_id!r} has invalid min_confidence")
        if "requires_no_rollback" in rule and not isinstance(
            rule["requires_no_rollback"], bool
        ):
            raise ValueError(
                f"Policy rule {rule_id!r} field 'requires_no_rollback' must be boolean"
            )
        for key in ("description", "reason", "_pack"):
            if key in rule and not isinstance(rule[key], str):
                raise ValueError(f"Policy rule {rule_id!r} field {key!r} must be text")
