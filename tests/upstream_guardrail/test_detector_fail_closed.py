from __future__ import annotations

import unittest
from unittest.mock import patch

from app.domains.guardrail._vendor.agentgate.decision import DecisionEngine
from app.domains.guardrail._vendor.agentgate.detectors.llm_client import LLMUnavailable
from app.domains.guardrail._vendor.agentgate.detectors.llm_detectors import (
    LLMActionIntentDetector,
    LLMPaymentPhishingDetector,
    LLMPIIDetector,
    LLMSecretDetector,
    LLMSourceCodeDetector,
)
from app.domains.guardrail._vendor.agentgate.detectors.llm_prompt_injection import (
    LLMPromptInjectionDetector,
)
from app.domains.guardrail._vendor.agentgate.schemas import ActionRequest, Decision
from tests.upstream_guardrail.fake_audit import audit_patch


def setUpModule():
    global _AUDIT
    _AUDIT = audit_patch()
    _AUDIT.start()


def tearDownModule():
    _AUDIT.stop()


class TestDetectorFailClosed(unittest.TestCase):
    def setUp(self):
        self.req = ActionRequest(action_type="API_CALL", payload_summary="test data")

    def _assert_fail_closed(self, detector_class, bad_response):
        with patch(
            "app.domains.guardrail._vendor.agentgate.detectors.llm_client.chat_json",
            return_value=bad_response,
        ):
            result = DecisionEngine(detectors=[detector_class()]).evaluate(self.req)
        self.assertEqual(
            result.decision,
            Decision.NEED_APPROVAL,
            f"{detector_class.name} did not fail closed on {bad_response}",
        )

    def test_pii_fail_closed(self):
        # Malformed: missing has_pii
        self._assert_fail_closed(LLMPIIDetector, {})
        # Out-of-range: invalid severity
        self._assert_fail_closed(
            LLMPIIDetector,
            {"has_pii": True, "items": [{"type": "EMAIL", "value": "x", "severity": "SUPER_BAD"}]},
        )
        # Self-contradictory: has_pii false but has items
        self._assert_fail_closed(
            LLMPIIDetector,
            {"has_pii": False, "items": [{"type": "EMAIL", "value": "x", "severity": "HIGH"}]},
        )

    def test_secret_fail_closed(self):
        self._assert_fail_closed(LLMSecretDetector, {})
        self._assert_fail_closed(
            LLMSecretDetector,
            {
                "has_secrets": True,
                "items": [{"type": "INVALID_TYPE", "value": "x", "severity": "HIGH"}],
            },
        )
        self._assert_fail_closed(
            LLMSecretDetector,
            {
                "has_secrets": False,
                "items": [{"type": "AWS_ACCESS_KEY", "value": "x", "severity": "HIGH"}],
            },
        )
        self._assert_fail_closed(LLMSecretDetector, {"has_secrets": True, "items": []})

    def test_source_code_fail_closed(self):
        self._assert_fail_closed(LLMSourceCodeDetector, {})
        # Missing confidence
        self._assert_fail_closed(
            LLMSourceCodeDetector, {"has_code": True, "has_codename": False, "language": "python"}
        )
        # Self-contradictory: no code/codename but has language
        self._assert_fail_closed(
            LLMSourceCodeDetector,
            {"has_code": False, "has_codename": False, "language": "python", "confidence": 0.9},
        )

    def test_payment_phishing_fail_closed(self):
        self._assert_fail_closed(LLMPaymentPhishingDetector, {})
        # Out-of-range confidence
        self._assert_fail_closed(
            LLMPaymentPhishingDetector,
            {
                "has_payment": True,
                "has_credential_request": False,
                "has_urgency": False,
                "confidence": 1.5,
            },
        )

    def test_action_intent_fail_closed(self):
        self._assert_fail_closed(LLMActionIntentDetector, {})
        # Self-contradictory: not bulk but estimated_count >= 20
        self._assert_fail_closed(
            LLMActionIntentDetector,
            {
                "is_bulk": False,
                "estimated_count": 50,
                "is_destructive": False,
                "is_external_send": False,
                "confidence": 0.9,
            },
        )

    def test_prompt_injection_fail_closed(self):
        req = ActionRequest(action_type="API_CALL", content_context="test data")
        # PromptInjection schema allows "injection" or "benign". This is invalid.
        with patch(
            "app.domains.guardrail._vendor.agentgate.detectors.llm_client.chat_json",
            return_value={"label": "invalid_label", "confidence": 0.9},
        ):
            result = DecisionEngine(detectors=[LLMPromptInjectionDetector()]).evaluate(req)
        self.assertEqual(result.decision, Decision.NEED_APPROVAL)


class TestDetectorInputs(unittest.TestCase):
    def setUp(self):
        self.req_scan = ActionRequest(action_type="API_CALL", payload_summary="payload_xyz123")
        self.req_content = ActionRequest(action_type="API_CALL", content_context="content_xyz123")

    def _assert_input_checked(self, detector_class, req, expected_substring):
        def fake_chat_json(system_prompt, user_content, **kwargs):
            if expected_substring not in user_content:
                raise LLMUnavailable(
                    f"Expected {expected_substring} in prompt, got: {user_content}"
                )

            if detector_class == LLMPIIDetector:
                return {"has_pii": False}
            elif detector_class == LLMSecretDetector:
                return {"has_secrets": False}
            elif detector_class == LLMSourceCodeDetector:
                return {"has_code": False, "has_codename": False, "language": "", "confidence": 1.0}
            elif detector_class == LLMPaymentPhishingDetector:
                return {
                    "has_payment": False,
                    "has_credential_request": False,
                    "has_urgency": False,
                    "confidence": 1.0,
                }
            elif detector_class == LLMActionIntentDetector:
                return {
                    "is_bulk": False,
                    "estimated_count": 0,
                    "is_destructive": False,
                    "is_external_send": False,
                    "confidence": 1.0,
                }
            elif detector_class == LLMPromptInjectionDetector:
                return {"label": "benign", "confidence": 1.0}
            return {}

        with patch(
            "app.domains.guardrail._vendor.agentgate.detectors.llm_client.chat_json",
            side_effect=fake_chat_json,
        ):
            result = DecisionEngine(detectors=[detector_class()]).evaluate(req)

        # If it didn't find the expected substring, the fake_chat_json would raise LLMUnavailable,
        # which evaluates to NEED_APPROVAL (fail-closed). So we assert it passed successfully (ALLOW).
        self.assertEqual(result.decision, Decision.ALLOW)

    def test_inputs(self):
        self._assert_input_checked(LLMPIIDetector, self.req_scan, "payload_xyz123")
        self._assert_input_checked(LLMSecretDetector, self.req_scan, "payload_xyz123")
        self._assert_input_checked(LLMSourceCodeDetector, self.req_scan, "payload_xyz123")
        self._assert_input_checked(LLMPaymentPhishingDetector, self.req_scan, "payload_xyz123")
        self._assert_input_checked(LLMActionIntentDetector, self.req_scan, "payload_xyz123")

        # Prompt injection reads content_text which takes content_context
        self._assert_input_checked(LLMPromptInjectionDetector, self.req_content, "content_xyz123")
