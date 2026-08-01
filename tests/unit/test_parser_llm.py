"""Tests for the OpenRouter-backed LLM parser (app.llm.services.parser)."""

import asyncio

import httpx

from app.config.settings import get_settings
from app.llm.services import parser as llm_parser


def _run(coro):
    return asyncio.run(coro)


class TestNormalizeStep:
    """Verify LLM output is coerced into the canonical step schema."""

    def test_normalizes_browser_step(self) -> None:
        step = llm_parser._normalize_step(
            {
                "action_type": "BROWSER_CLICK",
                "target_system": "browser",
                "target": "playwright.dev",
                "risk_hint": "unknown",
                "payload": {"label": "Login", "role": "button"},
            }
        )
        assert step is not None
        assert step["action_type"] == "BROWSER_CLICK"
        assert step["source"] == "chat"
        assert step["target"] == "https://playwright.dev"  # bare domain gets https://
        assert step["browser_element"]["role"] == "button"

    def test_invalid_action_type_is_rejected(self) -> None:
        step = llm_parser._normalize_step({"action_type": "DO_SOMETHING_ELSE"})
        assert step is None

    def test_action_alias_is_mapped(self) -> None:
        """Free models may return action/aliases; they must map, not be dropped."""
        step = llm_parser._normalize_step(
            {"action": "click", "target_system": "browser", "target": "https://x.dev"}
        )
        assert step is not None
        assert step["action_type"] == "BROWSER_CLICK"

        navigate = llm_parser._normalize_step({"action": "navigate", "url": "https://x.dev"})
        assert navigate is not None
        assert navigate["action_type"] == "BROWSER_OPEN"
        assert navigate["target"] == "https://x.dev"

    def test_unknown_risk_hint_normalized(self) -> None:
        step = llm_parser._normalize_step(
            {"action_type": "API_CALL", "target_system": "gmail", "risk_hint": "not_a_hint"}
        )
        assert step["risk_hint"] == "unknown"

    def test_connector_step(self) -> None:
        step = llm_parser._normalize_step(
            {
                "action_type": "API_CALL",
                "target_system": "gmail",
                "domain": "productivity",
                "target": "john@example.com",
                "risk_hint": "external_send",
                "payload": {"action": "send", "to": "john@example.com"},
            }
        )
        assert step["target_system"] == "gmail"
        assert step["domain"] == "productivity"

    def test_domain_derived_from_target_system(self) -> None:
        """LLM omitting domain must not silently downgrade guardrail risk."""
        gmail = llm_parser._normalize_step(
            {"action_type": "API_CALL", "target_system": "gmail", "payload": {"action": "send"}}
        )
        assert gmail["domain"] == "productivity"
        assert gmail["risk_hint"] == "external_send"

        github = llm_parser._normalize_step(
            {"action_type": "API_CALL", "target_system": "github"}
        )
        assert github["domain"] == "code_protection"

        file_step = llm_parser._normalize_step(
            {"action_type": "FILE_READ", "target_system": "local_file"}
        )
        assert file_step["domain"] == "filesystem"
        assert file_step["risk_hint"] == "file_read"


class TestEnsureOpenStep:
    """Verify BROWSER_OPEN is prepended when needed."""

    def test_prepends_open_for_browser_action_with_url(self) -> None:
        steps = [
            {
                "source": "chat",
                "domain": "browser",
                "action_type": "BROWSER_CLICK",
                "target_system": "browser",
                "target": "https://playwright.dev",
                "risk_hint": "unknown",
                "payload": {"url": "https://playwright.dev", "label": "Login"},
            }
        ]
        result = llm_parser._ensure_open_step(steps)
        assert len(result) == 2
        assert result[0]["action_type"] == "BROWSER_OPEN"
        assert result[0]["payload"] == {"url": "https://playwright.dev"}

    def test_keeps_existing_open_step(self) -> None:
        steps = [
            {"action_type": "BROWSER_OPEN", "target_system": "browser", "target": "https://x.dev"},
            {"action_type": "BROWSER_CLICK", "target_system": "browser", "target": "https://x.dev"},
        ]
        result = llm_parser._ensure_open_step(steps)
        assert len(result) == 2
        assert result[0]["action_type"] == "BROWSER_OPEN"

    def test_prepends_open_when_url_only_in_payload(self) -> None:
        """After normalization, target falls back to payload.url (real flow)."""
        raw = {
            "action_type": "BROWSER_CLICK",
            "target_system": "browser",
            "payload": {"url": "https://example.com", "label": "Go"},
        }
        step = llm_parser._normalize_step(raw)
        assert step["target"] == "https://example.com"
        result = llm_parser._ensure_open_step([step])
        assert len(result) == 2
        assert result[0]["action_type"] == "BROWSER_OPEN"
        assert result[0]["target"] == "https://example.com"


class TestParsePromptPlan:
    """Verify public API returns the documented shape (LLM mocked)."""

    def test_plan_shape(self, monkeypatch) -> None:
        """_llm_plan is mocked at its post-normalization contract (2 steps)."""
        async def fake_llm_plan(prompt: str) -> dict:
            return {
                "plan": [
                    {
                        "source": "chat",
                        "domain": "browser",
                        "action_type": "BROWSER_OPEN",
                        "target_system": "browser",
                        "target": "https://playwright.dev",
                        "risk_hint": "unknown",
                        "payload": {"url": "https://playwright.dev"},
                    },
                    {
                        "source": "chat",
                        "domain": "browser",
                        "action_type": "BROWSER_CLICK",
                        "target_system": "browser",
                        "target": "https://playwright.dev",
                        "risk_hint": "unknown",
                        "payload": {"label": "Login", "role": "button"},
                    },
                ],
                "summary": "Click Login",
            }

        monkeypatch.setattr(llm_parser, "_llm_plan", fake_llm_plan)
        result = _run(llm_parser.parse_prompt_plan("Click the login button on playwright.dev"))

        assert "plan" in result
        assert result["llm_provider"] == "openrouter/free"
        assert result["raw_prompt"] == "Click the login button on playwright.dev"
        assert "human_readable" in result
        assert len(result["plan"]) == 2
        assert result["plan"][0]["action_type"] == "BROWSER_OPEN"

    def test_parse_prompt_returns_primary_step(self, monkeypatch) -> None:
        async def fake_llm_plan(prompt: str) -> dict:
            return {
                "plan": [
                    {
                        "action_type": "BROWSER_OPEN",
                        "target_system": "browser",
                        "target": "https://youtube.com",
                    },
                    {
                        "action_type": "BROWSER_CLICK",
                        "target_system": "browser",
                        "target": "https://youtube.com",
                    },
                ],
                "summary": "",
            }

        monkeypatch.setattr(llm_parser, "_llm_plan", fake_llm_plan)
        result = _run(llm_parser.parse_prompt("Click something on youtube.com"))
        assert result["parsed"]["action_type"] == "BROWSER_CLICK"  # last non-OPEN step


class TestLlmPlanPipeline:
    """Verify _llm_plan wires normalize → risk → ensure_open in the right order."""

    def test_raw_model_output_is_normalized_into_plan(self, monkeypatch) -> None:
        """Model-style output ({action: click, url: ...}) gets BROWSER_OPEN prepended."""
        async def fake_post(self, url, json=None, headers=None):
            class _Resp:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {
                        "choices": [
                            {
                                "message": {
                                    "content": '{"plan": [{"action": "click", "url": "playwright.dev"}], "summary": "x"}'
                                }
                            }
                        ]
                    }

            return _Resp()

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
        monkeypatch.setenv("OPENROUTER_MODEL", "openrouter/free")
        get_settings.cache_clear()
        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        try:
            # Destructive keyword also proves _apply_prompt_risk runs in-order.
            result = _run(llm_parser.parse_prompt_plan("Delete the account button on playwright.dev"))
        finally:
            get_settings.cache_clear()

        assert result["llm_provider"] == "openrouter/free"
        assert len(result["plan"]) == 2
        # _ensure_open_step prepends navigation since the model only gave a click.
        assert result["plan"][0]["action_type"] == "BROWSER_OPEN"
        assert result["plan"][0]["target"] == "https://playwright.dev"
        assert result["plan"][1]["action_type"] == "BROWSER_CLICK"  # alias mapped
        # safety net ran after normalization: both steps carry the BLOCK hint
        assert result["plan"][0]["risk_hint"] == "destructive"
        assert result["plan"][1]["risk_hint"] == "destructive"


class TestFallback:
    """Verify fallback to the rule-based parser on failure."""

    def test_falls_back_without_api_key(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "")
        get_settings.cache_clear()
        try:
            result = _run(llm_parser.parse_prompt_plan("Open youtube.com"))
        finally:
            get_settings.cache_clear()
        # Rule-based parser produces the same plan even without the LLM.
        assert result["llm_provider"] == "dummy"
        assert result["plan"][0]["action_type"] == "BROWSER_OPEN"
        assert result["plan"][0]["target"] == "https://youtube.com"

    def test_falls_back_on_llm_error(self, monkeypatch) -> None:
        async def boom(prompt: str) -> dict:
            raise RuntimeError("LLM exploded")

        monkeypatch.setattr(llm_parser, "_llm_plan", boom)
        result = _run(llm_parser.parse_prompt_plan("Send email to john@example.com saying hello"))
        assert result["llm_provider"] == "dummy"
        assert result["plan"][0]["target_system"] == "gmail"

    def test_extract_json_handles_markdown_fence(self) -> None:
        content = '```json\n{"plan": [], "summary": "x"}\n```'
        assert llm_parser._extract_json(content) == {"plan": [], "summary": "x"}


class TestPromptRiskSafetyNet:
    """Verify deterministic risk keywords override LLM output (guardrail safety)."""

    def test_destructive_keyword_overrides_risk_hint(self) -> None:
        steps = [{"action_type": "BROWSER_CLICK", "target_system": "browser", "risk_hint": "unknown"}]
        result = llm_parser._apply_prompt_risk(steps, "Delete the repository on github.com")
        assert result[0]["risk_hint"] == "destructive"

    def test_unauthorized_keyword_overrides_risk_hint(self) -> None:
        steps = [{"action_type": "BROWSER_CLICK", "target_system": "browser", "risk_hint": "unknown"}]
        result = llm_parser._apply_prompt_risk(steps, "Bypass the login page on admin.example.com")
        assert result[0]["risk_hint"] == "unauthorized"

    def test_safe_prompt_unchanged(self) -> None:
        steps = [{"action_type": "BROWSER_CLICK", "target_system": "browser", "risk_hint": "unknown"}]
        result = llm_parser._apply_prompt_risk(steps, "Click the login button on playwright.dev")
        assert result[0]["risk_hint"] == "unknown"
