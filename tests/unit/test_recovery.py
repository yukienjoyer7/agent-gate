"""Unit tests for recovery popup detection (consent dialogs, banners)."""

from app.domains.browser.executor.recovery import (
    _filter_popup_matched,
    _first_popup_label,
    find_popup_candidates,
)


def _button(label: str) -> dict:
    return {"role": "button", "label": label}


def _non_button(label: str, role: str = "link") -> dict:
    return {"role": role, "label": label}


def test_finds_consent_dialog_buttons():
    """YouTube's consent dialog buttons ('Accept all' / 'Reject all') must be
    recognized as popup candidates so recovery can dismiss the overlay that
    occludes the target element in headless sessions."""
    candidates = find_popup_candidates(
        [
            _button("Reject all"),
            _button("Accept all"),
            _button("Customize"),
            _non_button("Accept all", role="link"),
        ]
    )

    assert len(candidates) == 2
    labels = {candidate["label"] for candidate in candidates}
    assert labels == {"Reject all", "Accept all"}


def test_consent_dialog_buttons_detected_among_header_elements():
    """The consent banner can coexist with page header buttons; only the
    consent buttons should be flagged as popup candidates."""
    candidates = find_popup_candidates(
        [
            _button("Guide"),
            _button("Accept all"),
            _button("Search with your voice"),
            _button("Reject all"),
            _button("Settings"),
        ]
    )

    labels = {candidate["label"] for candidate in candidates}
    assert labels == {"Accept all", "Reject all"}


def test_bare_generic_terms_do_not_match_regular_buttons():
    """Bare 'accept'/'agree'/'allow' must NOT flag ordinary form buttons —
    recovery would otherwise click a real functional control ('Accept',
    'I agree to terms') instead of dismissing an overlay."""
    candidates = find_popup_candidates(
        [
            _button("Accept"),
            _button("I accept the terms"),
            _button("Allow"),
            _button("Save"),
            _button("Submit"),
        ]
    )

    assert candidates == []


def test_close_glyphs_match_only_exact_label():
    """'×' as a close glyph matches when it is the whole label, but substring
    matches like 'Exit'/'Next'/'Maximize' must never be treated as popups."""
    candidates = find_popup_candidates(
        [
            _button("×"),
            _button("✕"),
            _button("x"),
            _button("Exit"),
            _button("Next"),
            _button("Maximize"),
        ]
    )

    assert len(candidates) == 3
    labels = {candidate["label"] for candidate in candidates}
    assert labels == {"×", "✕", "x"}


def test_ignores_non_buttons_and_regular_actions():
    candidates = find_popup_candidates(
        [
            _non_button("Close", role="link"),
            _button("Continue"),
            _button("Save"),
            _button("Submit"),
        ]
    )

    assert candidates == []


def test_still_finds_legacy_dismiss_keywords():
    candidates = find_popup_candidates(
        [
            _button("Close"),
            _button("No thanks"),
            _button("×"),
            _button("Skip"),
        ]
    )

    assert len(candidates) == 4


def test_filter_popup_matched_returns_only_candidates():
    """The filter must return only the matched elements whose (role, label)
    correspond to a popup candidate — never arbitrary elements like nav links
    or the 'Guide' button."""
    candidates = [
        {"role": "button", "label": "Reject all"},
        {"role": "button", "label": "Accept all"},
    ]
    matched_elements = [
        {"element_id": "1", "role": "button", "label": "Guide", "dom": {}},
        {"element_id": "2", "role": "button", "label": "Reject all", "dom": {}},
        {"element_id": "3", "role": "button", "label": "Accept all", "dom": {}},
        {"element_id": "4", "role": "link", "label": "Shorts", "dom": {}},
    ]

    result = _filter_popup_matched(candidates, matched_elements)

    assert [element["element_id"] for element in result] == ["2", "3"]


def test_first_popup_label_returns_consent_button_from_frame_labels():
    """Child-frame button labels (YouTube accounts/consent iframe) should
    yield the first consent dismissal label so iframe recovery can click it."""
    labels = [
        "Guide",
        "Sign in",
        "Accept all",
        "Settings",
    ]

    assert _first_popup_label(labels) == "Accept all"


def test_first_popup_label_none_when_no_popup_labels():
    labels = ["Guide", "Sign in", "Settings", "Shorts"]

    assert _first_popup_label(labels) is None


def test_first_popup_label_handles_empty_input():
    assert _first_popup_label([]) is None
    assert _first_popup_label(None) is None


def test_filter_popup_matched_empty_when_no_candidate_resolved():
    candidates = [{"role": "button", "label": "Reject all"}]
    matched_elements = [
        {"element_id": "1", "role": "button", "label": "Guide", "dom": {}},
        {"element_id": "2", "role": "link", "label": "Shorts", "dom": {}},
    ]

    assert _filter_popup_matched(candidates, matched_elements) == []
