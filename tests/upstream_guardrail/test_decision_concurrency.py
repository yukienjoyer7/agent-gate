"""Concurrent detector dispatch (Sprint 2 DS: "make the detectors run concurrently,
this comes first").

Deliberately does not use real Ollama calls, so these run fast and deterministically
in CI. The synthetic detectors below sleep for a controlled duration instead, which
lets the tests assert genuine overlap - not just "it finished quickly," which could
also be explained by a fast machine.

The live speedup depends on the inference server, not just this code: dispatching six
concurrent HTTP calls to Ollama with its default single-slot configuration buys almost
nothing, because Ollama queues them behind one worker rather than serving them in
parallel. Confirmed directly against the HTTP API (four concurrent requests completed
in the same wall time as four sequential ones, staggered in lockstep with the
sequential per-request duration) before writing any of this. With OLLAMA_NUM_PARALLEL
raised to match the detector count, a live run via benchmarks/raw_vs_guarded.py --live
showed guarded P95 drop 25-40% depending on case. See the comment in
DecisionEngine.evaluate() for the measured before/after.
"""

from __future__ import annotations

import time
import unittest

from app.domains.guardrail._vendor.agentgate.decision import DecisionEngine
from app.domains.guardrail._vendor.agentgate.detectors.base import Detector, Finding
from app.domains.guardrail._vendor.agentgate.detectors.llm_client import LLMUnavailable
from app.domains.guardrail._vendor.agentgate.schemas import ActionRequest, Decision, SensitiveEntity
from tests.upstream_guardrail.fake_audit import FakeAuditStore

# Long enough that scheduling jitter cannot make a sequential run look concurrent by
# accident, short enough that the suite stays fast.
_SLOW_MS = 80


class _SlowDetector(Detector):
    """Sleeps, then returns nothing. Records its own [start, end] interval so tests
    can check for genuine overlap between detectors, not just total wall time."""

    def __init__(self, name: str, intervals: list[tuple[float, float]], ms: int = _SLOW_MS):
        self.name = name
        self._intervals = intervals
        self._ms = ms

    def scan(self, req: ActionRequest) -> Finding:
        start = time.perf_counter()
        time.sleep(self._ms / 1000)
        self._intervals.append((start, time.perf_counter()))
        return self._finding()


class _FailingDetector(Detector):
    def __init__(self, name: str, exc: Exception, delay_ms: int = 0):
        self.name = name
        self._exc = exc
        self._delay_ms = delay_ms

    def scan(self, req: ActionRequest) -> Finding:
        if self._delay_ms:
            time.sleep(self._delay_ms / 1000)
        raise self._exc


class _FindingDetector(Detector):
    def __init__(self, name: str, kind: str, contribution: float = 0.4):
        self.name = name
        self._kind = kind
        self._contribution = contribution

    def scan(self, req: ActionRequest) -> Finding:
        return self._finding(
            entities=[SensitiveEntity(self._kind, f"[REDACTED_{self._kind}]", self.name, "HIGH")],
            reasons=[f"{self._kind} detected by {self.name}"],
            risk_contribution=self._contribution,
        )


def _engine(detectors: list[Detector]) -> DecisionEngine:
    return DecisionEngine(detectors=detectors, audit_store=FakeAuditStore())


class TestDetectorsRunConcurrently(unittest.TestCase):
    def test_five_slow_detectors_overlap_in_wall_clock_time(self):
        intervals: list[tuple[float, float]] = []
        detectors = [_SlowDetector(f"slow{i}", intervals) for i in range(5)]

        t0 = time.perf_counter()
        _engine(detectors).evaluate(ActionRequest(action_type="API_CALL"))
        wall = time.perf_counter() - t0

        self.assertEqual(len(intervals), 5)
        sequential_would_be = 5 * _SLOW_MS / 1000
        # Concurrent: bounded by the slowest one plus overhead, not the sum of all five.
        self.assertLess(wall, sequential_would_be * 0.6, "detectors did not run concurrently")

    def test_detector_intervals_genuinely_overlap(self):
        # The strongest proof: two detectors' [start, end] windows intersect. This
        # cannot happen if they were run one after another.
        intervals: list[tuple[float, float]] = []
        detectors = [_SlowDetector(f"slow{i}", intervals) for i in range(3)]
        _engine(detectors).evaluate(ActionRequest(action_type="API_CALL"))

        self.assertEqual(len(intervals), 3)
        overlaps = any(
            min(a_end, b_end) > max(a_start, b_start)
            for i, (a_start, a_end) in enumerate(intervals)
            for b_start, b_end in intervals[i + 1 :]
        )
        self.assertTrue(overlaps, f"no overlapping intervals among {intervals}")

    def test_per_detector_timing_is_still_recorded_independently(self):
        # Concurrency must not corrupt per-detector timing: each thread times only
        # its own call, so five detectors still produce five samples of roughly
        # their own sleep duration, not five samples of the total wall time.
        samples: dict[str, float] = {}
        detectors = [_SlowDetector(f"slow{i}", []) for i in range(4)]
        engine = _engine(detectors)
        engine.timing_sink = lambda stage, ms: samples.__setitem__(stage, ms)
        engine.evaluate(ActionRequest(action_type="API_CALL"))

        for i in range(4):
            key = f"detector:slow{i}"
            self.assertIn(key, samples)
            self.assertLess(
                samples[key], _SLOW_MS * 3, f"{key} timing looks like a sum, not one call"
            )

    def test_zero_detectors_does_not_crash(self):
        result = _engine([]).evaluate(ActionRequest(action_type="API_CALL"))
        self.assertEqual(result.decision, Decision.ALLOW)


class TestFailClosedUnderConcurrency(unittest.TestCase):
    """Same fail-closed guarantee as the sequential engine, verified under concurrent
    dispatch where a naive implementation could plausibly lose a failure."""

    def test_llm_unavailable_still_forces_need_approval(self):
        result = _engine(
            [
                _FindingDetector("ok", "SOURCE_CODE"),
                _FailingDetector("down", LLMUnavailable("offline")),
            ]
        ).evaluate(ActionRequest(action_type="API_CALL"))
        self.assertIn(result.decision, {Decision.NEED_APPROVAL, Decision.BLOCK})
        self.assertIn("Ensure Ollama is running", " ".join(result.reasons))

    def test_generic_detector_exception_still_forces_need_approval(self):
        result = _engine(
            [
                _FindingDetector("ok", "SOURCE_CODE"),
                _FailingDetector("broken", RuntimeError("boom")),
            ]
        ).evaluate(ActionRequest(action_type="API_CALL"))
        self.assertIn(result.decision, {Decision.NEED_APPROVAL, Decision.BLOCK})
        self.assertIn("failed", " ".join(result.reasons))

    def test_first_detector_in_registration_order_wins_the_error_message(self):
        # The failing detector that appears FIRST in the list determines the message,
        # deterministic regardless of which thread happens to finish first. Detector
        # B is deliberately faster (no delay) than detector A (small delay), so a
        # completion-order-based implementation would report B's message instead.
        result = _engine(
            [
                _FailingDetector("a_generic", RuntimeError("boom"), delay_ms=20),
                _FailingDetector("b_unavailable", LLMUnavailable("offline"), delay_ms=0),
            ]
        ).evaluate(ActionRequest(action_type="API_CALL"))
        self.assertIn("failed", " ".join(result.reasons))
        self.assertNotIn("Ensure Ollama is running", " ".join(result.reasons))

    def test_findings_from_a_successful_detector_survive_a_sibling_failure(self):
        # Unlike the old sequential engine (which stopped at the first failure and
        # discarded any detector not yet reached), a concurrently-dispatched detector
        # that already completed with a real finding is not thrown away just because
        # a different detector failed. All detectors run regardless of order.
        result = _engine(
            [
                _FailingDetector("broken", RuntimeError("boom")),
                _FindingDetector("secret", "GENERIC_SECRET", contribution=0.85),
            ]
        ).evaluate(ActionRequest(action_type="API_CALL", risk_hint=["external_send"]))
        kinds = {e.kind for e in result.sensitive_entities}
        self.assertIn("GENERIC_SECRET", kinds)

    def test_multiple_failures_still_yield_one_deterministic_reason(self):
        result = _engine(
            [
                _FailingDetector("first", RuntimeError("boom")),
                _FailingDetector("second", RuntimeError("also boom")),
            ]
        ).evaluate(ActionRequest(action_type="API_CALL"))
        reason_text = " ".join(result.reasons)
        self.assertEqual(reason_text.count("failed; action held for review"), 1)


if __name__ == "__main__":
    unittest.main()
