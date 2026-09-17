"""Decision engine.

Runs every detector over an ActionRequest, aggregates the findings, asks the policy
engine for domain rules, blends that with the risk score, and produces a single
DecisionResponse. This is the place where "what did we detect" becomes "what should
happen".
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from . import risk
from .audit import STAGE_ACTION, build_audit_store
from .detectors import Detector, Finding, get_default_detectors
from .detectors.llm_client import LLMUnavailable
from .policy import PolicyContext, PolicyEngine
from .sanitizer import sanitize
from .schemas import ActionRequest, Decision, DecisionResponse, RiskLevel

_RANK = {
    Decision.ALLOW: 0,
    Decision.SANITIZE: 1,
    Decision.ASK_USER: 2,
    Decision.NEED_APPROVAL: 3,
    Decision.BLOCK: 4,
}

# Below this, a NEED_APPROVAL is redirected to ASK_USER instead - the planner itself
# wasn't confident, so intent should be clarified before a human reviews a risk score
# computed from a guess.
_LOW_CONFIDENCE_THRESHOLD = 0.75
_CONFIDENCE_GATED_TYPES = {
    "API_CALL", "BROWSER_SUBMIT", "BROWSER_CLICK", "BROWSER_TYPE",
    "FILE_WRITE", "FILE_DELETE",
}

_NEXT_STEP = {
    Decision.ALLOW: "execute",
    Decision.SANITIZE: "execute_sanitized",
    Decision.ASK_USER: "ask_user",
    Decision.NEED_APPROVAL: "approval",
    Decision.BLOCK: "stop",
}


def _stronger(a: Decision, b: Decision) -> Decision:
    return a if _RANK[a] >= _RANK[b] else b


def _risk_decision(level: RiskLevel) -> Decision:
    if level == RiskLevel.CRITICAL:
        return Decision.BLOCK
    if level == RiskLevel.HIGH:
        return Decision.NEED_APPROVAL
    return Decision.ALLOW


class DecisionEngine:
    def __init__(
        self,
        detectors: list[Detector] | None = None,
        policy_engine: PolicyEngine | None = None,
        audit_store: Any | None = None,
        timing_sink: Callable[[str, float], None] | None = None,
    ):
        self.detectors = detectors if detectors is not None else get_default_detectors()
        self.policy_engine = policy_engine if policy_engine is not None else PolicyEngine()
        # Auditing is mandatory (PRD F14). An unset or unreachable DSN raises here, at
        # construction, rather than letting unaudited decisions through at evaluate().
        self.audit_store = audit_store if audit_store is not None else build_audit_store()
        self.timing_sink = timing_sink

    def evaluate(self, req: ActionRequest, stage: str = STAGE_ACTION) -> DecisionResponse:
        # 1. Detection. The six detectors are independent HTTP calls to the local
        # model, so they are dispatched concurrently rather than one after another.
        #
        # This alone is not sufficient: Ollama serves one generation request at a
        # time per model instance unless OLLAMA_NUM_PARALLEL is raised above its
        # effective default of 1, in which case concurrent dispatch just queues
        # behind a single worker and buys nothing. Confirmed directly against the
        # Ollama HTTP API before touching this code: four concurrent requests with
        # the default config completed in visibly staggered, ~0.18s-spaced
        # intervals summing to the same wall time as four sequential requests - the
        # signature of server-side serialization, not real parallelism.
        #
        # With OLLAMA_NUM_PARALLEL=6 (matching the detector count) on the same
        # machine, live-detector P95 measured via benchmarks/raw_vs_guarded.py
        # --live dropped 25-40% depending on case (e.g. 9682ms -> 5803ms), limited
        # by the slowest single detector rather than the sum of all six - real but
        # sub-linear, since six requests still share one GPU's compute. Concurrent
        # dispatch is necessary here; OLLAMA_NUM_PARALLEL is what makes it matter.
        #
        # Each detector still times its own call independently via _record_timing,
        # so per-detector P50/P95 is unaffected by running in parallel; only the
        # overall wall-clock time improves once the server can actually overlap them.
        entities = []
        reasons: list[str] = []
        contributions: list[float] = []
        tags: set[str] = set()
        entity_kinds: set[str] = set()
        detector_error: str | None = None

        if self.detectors:
            outcomes: list[tuple[Finding | None, str | None]] = [(None, None)] * len(
                self.detectors
            )
            workers = max(1, min(len(self.detectors), 32))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                future_to_index = {
                    pool.submit(self._scan_one, det, req): index
                    for index, det in enumerate(self.detectors)
                }
                for future in as_completed(future_to_index):
                    outcomes[future_to_index[future]] = future.result()

            # Detector failures still fail closed, same as the sequential engine this
            # replaced: the first detector to fail *in registration order* sets
            # detector_error (deterministic regardless of which thread happens to
            # finish first), using the message that says whether the runtime itself
            # is unavailable or one detector broke. Unlike the sequential version, a
            # failure no longer discards findings from detectors that already
            # completed - they ran concurrently, so their results exist whether or
            # not a sibling detector failed, and throwing away a real finding on an
            # unrelated detector's error would weaken the guardrail, not strengthen it.
            for det, (finding, error) in zip(self.detectors, outcomes):
                if error is not None and detector_error is None:
                    detector_error = error
                if finding is None or not finding.triggered:
                    continue
                entities.extend(finding.entities)
                reasons.extend(finding.reasons)
                contributions.append(finding.risk_contribution)
                tags |= finding.tags
                entity_kinds |= {e.kind for e in finding.entities}

        # 2. Policy
        started = time.perf_counter()
        try:
            ctx = PolicyContext(tags=tags, entity_kinds=entity_kinds)
            policy = self.policy_engine.evaluate(req, ctx)
        finally:
            self._record_timing("policy", started)

        # 3. Risk score and policy floor. CRITICAL is reserved for categorically
        #    critical signals or a CRITICAL policy floor. Accumulated lower-severity
        #    findings cap at HIGH so legitimate-but-risky actions route to approval.
        started = time.perf_counter()
        try:
            base_score = risk.combine(contributions)
            has_critical_entity = any(e.severity == "CRITICAL" for e in entities)
            if not has_critical_entity:
                base_score = min(base_score, 0.84)
            score = risk.apply_floor(base_score, policy.risk_floor)
            if detector_error:
                score = risk.apply_floor(score, RiskLevel.HIGH)
            level = risk.score_to_level(score)
        finally:
            self._record_timing("risk_scoring", started)

        started = time.perf_counter()
        try:
            # 5. Final decision = strongest of (policy decision, risk-band decision)
            decision = _stronger(policy.decision, _risk_decision(level))
            if detector_error:
                decision = _stronger(decision, Decision.NEED_APPROVAL)

            # 5b. Low-confidence override: a NEED_APPROVAL routed off a guess the planner
            # itself wasn't sure about should clarify intent first, not go straight to a
            # human reviewer judging a risk score computed from that guess. Only downgrades
            # NEED_APPROVAL - a confirmed BLOCK/SANITIZE finding (e.g. a live secret) is not
            # softened by low confidence, that would weaken a real safety signal instead of
            # resolving an ambiguous one.
            if (
                decision == Decision.NEED_APPROVAL
                and detector_error is None
                and req.confidence < _LOW_CONFIDENCE_THRESHOLD
                and req.action_type in _CONFIDENCE_GATED_TYPES
            ):
                decision = Decision.ASK_USER
        finally:
            self._record_timing("decision_resolution", started)

        # 6. Sanitized preview whenever we have something to redact
        started = time.perf_counter()
        try:
            sanitized_payload = None
            if entities and req.raw_payload:
                redacted = sanitize(req.raw_payload)
                if redacted != req.raw_payload:
                    sanitized_payload = redacted

            # If policy asked to SANITIZE but we couldn't redact anything, fall back to approval.
            if decision == Decision.SANITIZE and sanitized_payload is None:
                decision = Decision.NEED_APPROVAL
        finally:
            self._record_timing("sanitization", started)

        if detector_error:
            reasons.append(detector_error)
        all_reasons = reasons + [r for r in policy.reasons if r not in reasons]
        if not all_reasons:
            all_reasons = ["No policy violations or sensitive content detected"]

        response = DecisionResponse(
            decision=decision,
            risk_level=level,
            risk_score=score,
            reasons=all_reasons,
            triggered_policies=policy.triggered,
            sensitive_entities=entities,
            sanitized_payload=sanitized_payload,
            next_step=_NEXT_STEP[decision],
            evaluation_error=detector_error,
        )
        # Stamps response.audit_id in place. A failed write raises AuditUnavailable:
        # an unauditable decision must not be returned as if it had been recorded.
        started = time.perf_counter()
        try:
            self.audit_store.record(req, response, stage)
        finally:
            self._record_timing("audit_write", started)
        return response

    def _scan_one(self, det: Detector, req: ActionRequest) -> tuple[Finding | None, str | None]:
        """Run one detector, timed independently of the others running alongside it."""
        started = time.perf_counter()
        try:
            return det.scan(req), None
        except LLMUnavailable:
            return None, (
                "LLM detector is unavailable. Ensure Ollama is running and the "
                "configured model is installed."
            )
        except Exception:
            return None, f"LLM detector {det.name!r} failed; action held for review."
        finally:
            self._record_timing(f"detector:{det.name}", started)

    def _record_timing(self, stage: str, started: float) -> None:
        if self.timing_sink is None:
            return
        try:
            self.timing_sink(stage, (time.perf_counter() - started) * 1000)
        except Exception:
            # Instrumentation must never change a safety decision.
            pass
