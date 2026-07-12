import pytest

from app.domains.agent.services.browser_prototype_agent import (
    BrowserPageModel,
    _normalize_actions,
    _payload_summary,
    _prepare_action,
    _result_summary,
)


def _page_model() -> BrowserPageModel:
    return BrowserPageModel(
        snapshot=[
            {
                "element_id": "el_continue",
                "role": "button",
                "label": "Continue",
                "risk_hint": "unknown",
                "dom": {"tag": "button"},
            }
        ],
        locator_candidates=[],
        selector_map={"el_continue": {"primary": {}, "fallbacks": []}},
    )


def test_normalize_actions_preserves_single_action_compatibility():
    assert _normalize_actions(action={"type": "screenshot"}, actions=None) == [
        {"type": "screenshot"}
    ]


def test_normalize_actions_rejects_mixed_action_shapes():
    with pytest.raises(ValueError, match="either action or actions"):
        _normalize_actions(
            action={"type": "click"},
            actions=[{"type": "screenshot"}],
        )


def test_prepare_screenshot_adds_indexed_default_path_for_sequences():
    prepared = _prepare_action(
        action={"type": "screenshot"},
        page_model=_page_model(),
        action_id="act_demo",
        action_index=2,
    )

    assert prepared == {
        "type": "screenshot",
        "path": "data/browser/screenshots/act_demo_02.png",
    }


def test_prepare_click_resolves_label_against_current_page_model():
    prepared = _prepare_action(
        action={"type": "click", "label": "Continue"},
        page_model=_page_model(),
        action_id="act_demo",
    )

    assert prepared["element_id"] == "el_continue"


def test_sequence_summaries_describe_multi_step_actions():
    actions = [{"type": "screenshot"}, {"type": "click"}, {"type": "screenshot"}]

    assert _payload_summary("https://example.test", actions) == (
        "3 browser actions on https://example.test: screenshot, click, screenshot"
    )
    assert _result_summary(actions, executed_count=3) == "executed 3 browser actions"
