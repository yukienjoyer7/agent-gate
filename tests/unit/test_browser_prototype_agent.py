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


def _search_page_model() -> BrowserPageModel:
    """Page model mirroring YouTube: the search box has DOM name="search_query"."""
    return BrowserPageModel(
        snapshot=[
            {
                "element_id": "el_search",
                "role": "combobox",
                "label": "Search",
                "risk_hint": "unknown",
                "dom": {
                    "tag": "input",
                    "name": "search_query",
                    "id": "search",
                    "placeholder": None,
                    "aria_label": "Search",
                },
            }
        ],
        locator_candidates=[],
        selector_map={"el_search": {"primary": {}, "fallbacks": []}},
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


def test_prepare_action_falls_back_to_label_when_element_id_not_in_selector_map():
    """LLM-emitted DOM attribute (e.g. name="search_query") must not fail when
    it is not a selector_map key — resolve by label instead."""
    prepared = _prepare_action(
        action={
            "type": "fill",
            "label": "Search",
            "role": "combobox",
            "element_id": "search_query",
            "value": "jerome",
        },
        page_model=_search_page_model(),
        action_id="act_demo",
    )

    assert prepared["element_id"] == "el_search"
    assert prepared["value"] == "jerome"


def test_prepare_action_resolves_via_dom_name_attribute():
    """When no label matches, resolve by matching the DOM name/id attributes."""
    prepared = _prepare_action(
        action={"type": "fill", "element_id": "search_query", "value": "jerome"},
        page_model=_search_page_model(),
        action_id="act_demo",
    )

    assert prepared["element_id"] == "el_search"


def test_prepare_action_resolves_via_dom_when_role_differs():
    """Page state can differ between tool call and execution: model says role
    combobox but the runtime snapshot reports searchbox — the role-agnostic DOM
    match tier must still resolve via name="search_query"."""
    page_model = BrowserPageModel(
        snapshot=[
            {
                "element_id": "el_search",
                "role": "searchbox",
                "label": "Search",
                "risk_hint": "unknown",
                "dom": {
                    "tag": "input",
                    "name": "search_query",
                    "id": "search",
                },
            }
        ],
        locator_candidates=[],
        selector_map={"el_search": {"primary": {}, "fallbacks": []}},
    )
    prepared = _prepare_action(
        action={
            "type": "fill",
            "label": "Search",
            "role": "combobox",
            "element_id": "search_query",
            "value": "jerome",
        },
        page_model=page_model,
        action_id="act_demo",
    )

    assert prepared["element_id"] == "el_search"


def test_prepare_action_resolves_localized_label_via_dom():
    """Labels can differ between tool call and execution (e.g. YouTube serves
    'Telusuri' instead of 'Search'): resolution must fall back to DOM name."""
    page_model = BrowserPageModel(
        snapshot=[
            {
                "element_id": "4",
                "role": "combobox",
                "label": "Telusuri",  # Indonesian label, not "Search"
                "risk_hint": "unknown",
                "dom": {"tag": "input", "name": "search_query", "id": None},
            },
            {
                "element_id": "5",
                "role": "button",
                "label": "Search",
                "risk_hint": "unknown",
                "dom": {"tag": "button", "name": None, "id": None},
            },
        ],
        locator_candidates=[],
        selector_map={"4": {"primary": {}, "fallbacks": []}, "5": {"primary": {}, "fallbacks": []}},
    )
    prepared = _prepare_action(
        action={
            "type": "fill",
            "label": "Search",
            "role": "combobox",
            "element_id": "4",
            "value": "jerome",
        },
        page_model=page_model,
        action_id="act_demo",
    )

    assert prepared["element_id"] == "4"


def test_prepare_action_does_not_trust_stale_valid_element_id():
    """An element_id that IS a selector_map key but whose label no longer
    matches the requested action must NOT be trusted (page-state drift) —
    resolution must fall back to the live snapshot."""
    page_model = BrowserPageModel(
        snapshot=[
            {
                "element_id": "4",
                "role": "button",
                "label": "Continue",
                "risk_hint": "unknown",
                "dom": {"tag": "button", "name": None, "id": None},
            },
            {
                "element_id": "5",
                "role": "combobox",
                "label": "Search",
                "risk_hint": "unknown",
                "dom": {"tag": "input", "name": "search_query", "id": "search"},
            },
        ],
        locator_candidates=[],
        selector_map={"4": {"primary": {}, "fallbacks": []}, "5": {"primary": {}, "fallbacks": []}},
    )
    prepared = _prepare_action(
        action={
            "type": "fill",
            "label": "Search",
            "role": "combobox",
            "element_id": "4",  # stale key that now points at Continue
            "value": "jerome",
        },
        page_model=page_model,
        action_id="act_demo",
    )

    assert prepared["element_id"] == "5"  # resolved from snapshot, not the stale "4"


def test_prepare_submit_resolves_and_is_supported():
    """BROWSER_SUBMIT maps to the submit action, which must be prepared the
    same way as click/fill (resolve element, keep value out)."""
    prepared = _prepare_action(
        action={"type": "submit", "label": "Continue"},
        page_model=_page_model(),
        action_id="act_demo",
    )

    assert prepared["type"] == "submit"
    assert prepared["element_id"] == "el_continue"


def test_prepare_fill_prefers_editable_element_over_guessed_button_role():
    """The LLM often guesses role='button' for a search box (e.g. YouTube's
    'Search' input). The resolver must prefer the editable combobox with the
    exact label instead of resolving the guessed-role partial match to the
    voice-search button ('Search with your voice'), which is occluded by the
    input and fails with 'Element is still occluded'."""
    page_model = BrowserPageModel(
        snapshot=[
            {
                "element_id": "4",
                "role": "combobox",
                "label": "Search",
                "risk_hint": "unknown",
                "dom": {"tag": "input", "name": "search_query", "id": "search"},
            },
            {
                "element_id": "5",
                "role": "button",
                "label": "Search with your voice",
                "risk_hint": "unknown",
                "dom": {"tag": "button", "name": None, "id": None},
            },
        ],
        locator_candidates=[],
        selector_map={
            "4": {"primary": {}, "fallbacks": []},
            "5": {"primary": {}, "fallbacks": []},
        },
    )
    prepared = _prepare_action(
        action={
            "type": "fill",
            "label": "Search",
            "role": "button",  # LLM guess; the real element is a combobox
            "value": "jerome",
        },
        page_model=page_model,
        action_id="act_demo",
    )

    assert prepared["element_id"] == "4"


def test_prepare_submit_prefers_editable_element_over_guessed_button_role():
    """Same guard for submit: pressing Enter must target the editable search
    input, not the voice-search button."""
    page_model = BrowserPageModel(
        snapshot=[
            {
                "element_id": "4",
                "role": "combobox",
                "label": "Search",
                "risk_hint": "unknown",
                "dom": {"tag": "input", "name": "search_query", "id": "search"},
            },
            {
                "element_id": "5",
                "role": "button",
                "label": "Search with your voice",
                "risk_hint": "unknown",
                "dom": {"tag": "button", "name": None, "id": None},
            },
        ],
        locator_candidates=[],
        selector_map={
            "4": {"primary": {}, "fallbacks": []},
            "5": {"primary": {}, "fallbacks": []},
        },
    )
    prepared = _prepare_action(
        action={
            "type": "submit",
            "label": "Search",
            "role": "button",  # LLM guess; the real element is a combobox
        },
        page_model=page_model,
        action_id="act_demo",
    )

    assert prepared["element_id"] == "4"


def test_prepare_click_still_prefers_exact_role_match_over_editable():
    """The editable preference must apply ONLY to fill/submit. A click with an
    exact role match (e.g. the search submit button) must still win over an
    editable element with the same label."""
    page_model = BrowserPageModel(
        snapshot=[
            {
                "element_id": "4",
                "role": "combobox",
                "label": "Search",
                "risk_hint": "unknown",
                "dom": {"tag": "input", "name": "search_query", "id": "search"},
            },
            {
                "element_id": "5",
                "role": "button",
                "label": "Search",
                "risk_hint": "unknown",
                "dom": {"tag": "button", "name": None, "id": None},
            },
        ],
        locator_candidates=[],
        selector_map={
            "4": {"primary": {}, "fallbacks": []},
            "5": {"primary": {}, "fallbacks": []},
        },
    )
    prepared = _prepare_action(
        action={"type": "click", "label": "Search", "role": "button"},
        page_model=page_model,
        action_id="act_demo",
    )

    assert prepared["element_id"] == "5"


def test_prepare_action_still_raises_when_unresolvable():
    with pytest.raises(ValueError, match="unable to resolve a unique browser element"):
        _prepare_action(
            action={"type": "fill", "label": "Nonexistent", "value": "x"},
            page_model=_page_model(),
            action_id="act_demo",
        )


def test_sequence_summaries_describe_multi_step_actions():
    actions = [{"type": "screenshot"}, {"type": "click"}, {"type": "screenshot"}]

    assert _payload_summary("https://example.test", actions) == (
        "3 browser actions on https://example.test: screenshot, click, screenshot"
    )
    assert _result_summary(actions, executed_count=3) == "executed 3 browser actions"
