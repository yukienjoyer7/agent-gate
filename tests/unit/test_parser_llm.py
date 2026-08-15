"""Tests for the LLM-backed parser (app.llm.services.parser)."""

import asyncio
import json

import httpx
import pytest

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

    def test_search_alias_normalizes_to_search_box_type(self) -> None:
        step = llm_parser._normalize_step(
            {
                "action": "search",
                "target_system": "browser",
                "target": "youtube.com",
                "query": "jerome",
            }
        )

        assert step is not None
        assert step["action_type"] == "BROWSER_TYPE"
        assert step["target"] == "https://youtube.com"
        assert step["payload"]["element_id"] == "search_query"
        assert step["payload"]["label"] == "Search"
        assert step["payload"]["role"] == "combobox"
        assert step["payload"]["value"] == "jerome"

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

        github = llm_parser._normalize_step({"action_type": "API_CALL", "target_system": "github"})
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


class TestToolCallLoop:
    """Verify function calling: tools are sent, results fed back, plan returned."""

    def test_tool_call_is_executed_and_result_fed_back(self, monkeypatch) -> None:
        """A tool_calls response runs the tool and continues to the final plan."""
        calls: list[dict] = []

        async def fake_post(self, url, json=None, headers=None):
            calls.append(json)
            if len(calls) == 1:
                return _Resp(
                    {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "call_1",
                                            "type": "function",
                                            "function": {
                                                "name": "get_accessibility_tree",
                                                "arguments": '{"url": "https://playwright.dev"}',
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    }
                )
            return _Resp(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": (
                                    '{"plan": [{"action_type": "BROWSER_CLICK", '
                                    '"target_system": "browser", '
                                    '"target": "https://playwright.dev", '
                                    '"payload": {"label": "Get started", "role": "link"}}], '
                                    '"summary": "Click Get started"}'
                                ),
                            }
                        }
                    ]
                }
            )

        async def fake_execute_tool(name: str, arguments: dict) -> dict:
            assert name == "get_accessibility_tree"
            assert arguments == {"url": "https://playwright.dev"}
            return {"url": "https://playwright.dev", "count": 1, "elements": []}

        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        get_settings.cache_clear()
        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        monkeypatch.setattr(llm_parser, "execute_tool", fake_execute_tool)
        try:
            result = _run(llm_parser.parse_prompt_plan("Click Get started on playwright.dev"))
        finally:
            get_settings.cache_clear()

        assert len(calls) == 2
        # First request carries the tool definition; second carries tool result.
        assert any(
            "get_accessibility_tree" in json.dumps(tool) for tool in (calls[0].get("tools") or [])
        )
        tool_messages = [msg for msg in calls[1]["messages"] if msg.get("role") == "tool"]
        assert len(tool_messages) == 1
        assert tool_messages[0]["tool_call_id"] == "call_1"
        assert "url" in tool_messages[0]["content"]
        # _ensure_open_step prepends navigation, so the click is the last step.
        assert result["plan"][0]["action_type"] == "BROWSER_OPEN"
        assert result["plan"][-1]["action_type"] == "BROWSER_CLICK"
        assert result["plan"][-1]["payload"]["label"] == "Get started"

    def test_tool_calls_loop_capped_by_max_iterations(self, monkeypatch) -> None:
        """Endless tool calls hit the iteration cap and raise instead of hanging."""

        async def fake_post(self, url, json=None, headers=None):
            return _Resp(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_x",
                                        "type": "function",
                                        "function": {
                                            "name": "get_accessibility_tree",
                                            "arguments": '{"url": "https://x.dev"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            )

        async def fake_execute_tool(name: str, arguments: dict) -> dict:
            return {"error": "boom"}

        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.setenv("LLM_MAX_TOOL_ITERATIONS", "2")
        get_settings.cache_clear()
        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        monkeypatch.setattr(llm_parser, "execute_tool", fake_execute_tool)
        try:
            with pytest.raises(ValueError, match="max tool iterations"):
                _run(llm_parser.parse_prompt_plan("Click something on x.dev"))
        finally:
            get_settings.cache_clear()

    def test_response_format_omitted_when_tools_enabled(self, monkeypatch) -> None:
        """JSON mode biases models against tool calls; only send it tool-less."""
        calls: list[dict] = []

        async def fake_post(self, url, json=None, headers=None):
            calls.append(json)
            return _Resp(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": (
                                    '{"plan": [{"action_type": "BROWSER_OPEN", '
                                    '"target_system": "browser", '
                                    '"target": "https://x.dev"}], "summary": "x"}'
                                ),
                            }
                        }
                    ]
                }
            )

        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        get_settings.cache_clear()
        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        try:
            _run(llm_parser.parse_prompt_plan("Open x.dev"))
        finally:
            get_settings.cache_clear()

        assert "tools" in calls[0]
        assert "response_format" not in calls[0]

    def test_response_format_sent_when_tools_disabled(self, monkeypatch) -> None:
        """Without function calling we still ask for strict JSON output."""
        calls: list[dict] = []

        async def fake_post(self, url, json=None, headers=None):
            calls.append(json)
            return _Resp(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": (
                                    '{"plan": [{"action_type": "BROWSER_OPEN", '
                                    '"target_system": "browser", '
                                    '"target": "https://x.dev"}], "summary": "x"}'
                                ),
                            }
                        }
                    ]
                }
            )

        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.setenv("LLM_TOOLS_ENABLED", "false")
        get_settings.cache_clear()
        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        try:
            result = _run(llm_parser.parse_prompt_plan("Open x.dev"))
        finally:
            get_settings.cache_clear()

        assert "tools" not in calls[0]
        assert calls[0]["response_format"] == {"type": "json_object"}
        assert result["llm_provider"] == "openrouter/free"

    def test_retries_without_tools_when_rejected_400(self, monkeypatch) -> None:
        """Models without tool support: first request 400 → retry without tools."""
        calls: list[dict] = []

        async def fake_post(self, url, json=None, headers=None):
            calls.append(json)
            if len(calls) == 1:
                resp = httpx.Response(
                    400,
                    json={"error": {"message": "model does not support tools"}},
                    request=httpx.Request("POST", url),
                )
                return resp
            return _Resp(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": (
                                    '{"plan": [{"action_type": "BROWSER_OPEN", '
                                    '"target_system": "browser", '
                                    '"target": "https://playwright.dev"}], "summary": "x"}'
                                ),
                            }
                        }
                    ]
                }
            )

        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        get_settings.cache_clear()
        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        try:
            result = _run(llm_parser.parse_prompt_plan("Open playwright.dev"))
        finally:
            get_settings.cache_clear()

        assert len(calls) == 2
        assert "tools" in calls[0]
        assert "tools" not in calls[1]
        assert result["plan"][0]["action_type"] == "BROWSER_OPEN"


class _Resp:
    """Minimal stand-in for an httpx.Response used by tests."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._data


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

        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.setenv("LLM_MODEL", "openrouter/free")
        get_settings.cache_clear()
        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        try:
            # Destructive keyword also proves _apply_prompt_risk runs in-order.
            result = _run(
                llm_parser.parse_prompt_plan("Delete the account button on playwright.dev")
            )
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

    def test_search_prompt_repairs_incomplete_llm_plan(self, monkeypatch) -> None:
        async def fake_post(self, url, json=None, headers=None):
            class _Resp:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {
                        "choices": [
                            {
                                "message": {
                                    "content": (
                                        '{"plan": ['
                                        '{"action_type": "BROWSER_OPEN", '
                                        '"target_system": "browser", '
                                        '"target": "https://youtube.com"}, '
                                        '{"action_type": "BROWSER_SCREENSHOT", '
                                        '"target_system": "browser", '
                                        '"target": "https://youtube.com"}], '
                                        '"summary": "Open YouTube and screenshot"}'
                                    )
                                }
                            }
                        ]
                    }

            return _Resp()

        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.setenv("LLM_MODEL", "openrouter/free")
        get_settings.cache_clear()
        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        try:
            result = _run(
                llm_parser.parse_prompt_plan("open youtube.com, search jerome, and screenshot")
            )
        finally:
            get_settings.cache_clear()

        assert [step["action_type"] for step in result["plan"]] == [
            "BROWSER_OPEN",
            "BROWSER_TYPE",
            "BROWSER_SUBMIT",
            "BROWSER_SCREENSHOT",
        ]
        assert result["plan"][1]["payload"]["value"] == "jerome"
        assert result["plan"][2]["payload"]["delay_ms"] == 2000


class TestNoFallback:
    """The rule-based fallback parser was removed: LLM errors propagate."""

    def test_raises_without_api_key(self, monkeypatch) -> None:
        monkeypatch.setenv("LLM_API_KEY", "")
        get_settings.cache_clear()
        try:
            with pytest.raises(RuntimeError, match="LLM_API_KEY"):
                _run(llm_parser.parse_prompt_plan("Open youtube.com"))
        finally:
            get_settings.cache_clear()

    def test_raises_on_llm_error(self, monkeypatch) -> None:
        async def boom(prompt: str) -> dict:
            raise RuntimeError("LLM exploded")

        monkeypatch.setattr(llm_parser, "_llm_plan", boom)
        with pytest.raises(RuntimeError, match="LLM exploded"):
            _run(llm_parser.parse_prompt_plan("Send email to john@example.com saying hello"))

    def test_extract_json_handles_markdown_fence(self) -> None:
        content = '```json\n{"plan": [], "summary": "x"}\n```'
        assert llm_parser._extract_json(content) == {"plan": [], "summary": "x"}

    def test_extract_json_handles_bare_step_array(self) -> None:
        """Free models sometimes return [{...}] instead of {"plan": [...]}."""
        content = (
            '[{"action_type": "BROWSER_OPEN", "target_system": "browser", '
            '"target": "https://youtube.com"}, '
            '{"action_type": "BROWSER_TYPE", "target_system": "browser", '
            '"target": "https://youtube.com"}]'
        )
        result = llm_parser._extract_json(content)
        assert "plan" in result
        assert len(result["plan"]) == 2
        assert result["plan"][0]["action_type"] == "BROWSER_OPEN"


class TestPromptRiskSafetyNet:
    """Verify deterministic risk keywords override LLM output (guardrail safety)."""

    def test_destructive_keyword_overrides_risk_hint(self) -> None:
        steps = [
            {"action_type": "BROWSER_CLICK", "target_system": "browser", "risk_hint": "unknown"}
        ]
        result = llm_parser._apply_prompt_risk(steps, "Delete the repository on github.com")
        assert result[0]["risk_hint"] == "destructive"

    def test_unauthorized_keyword_overrides_risk_hint(self) -> None:
        steps = [
            {"action_type": "BROWSER_CLICK", "target_system": "browser", "risk_hint": "unknown"}
        ]
        result = llm_parser._apply_prompt_risk(steps, "Bypass the login page on admin.example.com")
        assert result[0]["risk_hint"] == "unauthorized"

    def test_safe_prompt_unchanged(self) -> None:
        steps = [
            {"action_type": "BROWSER_CLICK", "target_system": "browser", "risk_hint": "unknown"}
        ]
        result = llm_parser._apply_prompt_risk(steps, "Click the login button on playwright.dev")
        assert result[0]["risk_hint"] == "unknown"
