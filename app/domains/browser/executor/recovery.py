from app.domains.browser.snapshot.snapshotBuilder import (
    build_semantic_elements,
    enrich_semantic_elements,
)

from app.domains.browser.selector_map.domInspector import (
    build_execution_metadata,
)

from app.domains.browser.selector_map.matcher import (
    build_matched_elements,
)

from app.domains.browser.selector_map.locatorGenerator import (
    build_locator_candidates,
)

from app.domains.browser.selector_map.locatorRanker import (
    build_selector_map,
)
from app.domains.browser.executor.locatorResolver import resolve_locator

POPUP_KEYWORDS = {

    "close",
    "dismiss",
    "skip",
    "later",
    "cancel",
    "no thanks",
    "not now",

    # Consent / cookie dialogs (e.g. YouTube's "Accept all" / "Reject all")
    # intermittently overlay the page in headless sessions and occlude the
    # target element; recovery must be able to dismiss them. Compound phrases
    # only — bare "accept"/"agree"/"allow" would also match ordinary form
    # buttons ("Accept", "I agree to terms"), which recovery must never click.
    "accept all",
    "reject all",
    "allow all",
    "i agree",
    "agree and continue",
    "decline",
    "got it",

    "tutup",
    "lewati",
    "batal",
    "nanti",
    "setuju",
    "terima",
    "tolak",
    "izinkan",

}

# Single-character close glyphs (e.g. "×" on a dialog) match only when the
# label IS exactly the glyph — substring matching would flag "Exit", "Next",
# "Maximize", "Export" etc. as popup candidates.
_CLOSE_LABELS = {"x", "×", "✕"}


def _is_popup_label(label: str) -> bool:
    text = (label or "").lower()
    if text in _CLOSE_LABELS:
        return True
    return any(keyword in text for keyword in POPUP_KEYWORDS)


def find_popup_candidates(elements):

    candidates = []

    for element in elements:

        label = (
            element.get("label")
            or element.get("aria_label")
            or ""
        ).lower()

        role = (
            element.get("role")
            or ""
        ).lower()

        if role != "button":
            continue

        if _is_popup_label(label):
            candidates.append(element)

    return candidates


def _filter_popup_matched(candidates, matched_elements):
    """Return only the matched (element_id-carrying) forms of the popup
    candidates, keyed by (role, label)."""
    candidate_keys = {
        (candidate.get("role") or "", candidate.get("label") or "")
        for candidate in candidates
    }
    return [
        element
        for element in matched_elements
        if (element.get("role") or "", element.get("label") or "") in candidate_keys
    ]


def _first_popup_label(labels):
    """Return the first label (from an arbitrary element list) that looks like
    a popup/consent dismissal button, else None."""
    for label in labels or []:
        if _is_popup_label(label):
            return label
    return None


async def _dismiss_consent_in_frames(page):
    """Click a popup/consent dismissal button found in a CHILD frame.

    Some sites render consent dialogs inside an iframe (e.g. YouTube's
    accounts/consent frame), which the top-frame ARIA snapshot used by
    ``recover_popup`` cannot see — the element stays occluded and recovery
    fails. Scan every child frame's visible buttons for a popup label and
    click the first match.
    """
    main_frame = page.main_frame
    for frame in page.frames:
        if frame is main_frame:
            continue
        try:
            labels = await frame.evaluate(
                """() => [...document.querySelectorAll('button, [role="button"]')]
                    .map((b) => (b.getAttribute('aria-label') || b.innerText || '').trim())
                    .filter(Boolean)"""
            )
        except Exception:
            continue
        label = _first_popup_label(labels)
        if not label:
            continue
        try:
            await frame.get_by_role("button", name=label).first.click(timeout=3000)
            return True
        except Exception:
            continue
    return False

async def recover_popup(page):

    semantic = await build_semantic_elements(page)

    semantic = enrich_semantic_elements(
        semantic
    )

    candidates = find_popup_candidates(semantic)

    if not candidates:
        return False

    metadata = await build_execution_metadata(
        page,
        semantic
    )

    matched_elements = build_matched_elements(
        semantic,
        metadata
    )

    # Click ONLY the popup-candidate buttons (consent dialogs, banners), never
    # an arbitrary matched element — clicking a random control (e.g. the
    # "Guide" button or a nav link) dismisses nothing and could even navigate
    # away. Match candidates back to their matched (element_id-carrying) forms.
    popup_matched = _filter_popup_matched(
        candidates,
        matched_elements
    )

    if not popup_matched:
        return False

    locator_candidates = build_locator_candidates(
        popup_matched
    )

    selector_map = await build_selector_map(
        page,
        locator_candidates
    )

    for element in popup_matched:

        try:

            locator = await resolve_locator(
                page,
                selector_map,
                element["element_id"]
            )       

            await locator.click()

            return True

        except Exception:

            continue

    return False


async def recover(page):

    # Strategy 1
    await page.keyboard.press("Escape")

    await page.wait_for_timeout(300)

    # Strategy 2: top-frame popup/consent buttons
    if await recover_popup(page):
        await page.wait_for_timeout(300)
        return

    # Strategy 3: consent dialogs rendered inside a child frame (e.g.
    # YouTube's accounts/consent iframe) are invisible to the top-frame
    # ARIA snapshot; scan child frames and click their dismissal button.
    if await _dismiss_consent_in_frames(page):
        await page.wait_for_timeout(300)

    return