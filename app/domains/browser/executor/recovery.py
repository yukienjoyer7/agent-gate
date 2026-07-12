from app.domains.browser.snapshot.snapshotBuilder import (
    build_semantic_elements,
    enrich_semantic_elements,
)

from app.domains.browser.selector_map.domInspector  import (
    inspect_element,
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

    "tutup",
    "lewati",
    "batal",
    "nanti",

    "×",
    "✕",
    "x",

}

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

        if any(
            keyword in label
            for keyword in POPUP_KEYWORDS
        ):
            candidates.append(element)

    return candidates

async def recover_popup(page):

    semantic = await build_semantic_elements(page)

    semantic = await enrich_semantic_elements(
        page,
        semantic
    )

    candidates = find_popup_candidates(semantic)

    if not candidates:
        return False

    metadata = await inspect_element(page)

    matched = build_matched_elements(
        candidates,
        metadata
    )

    matched = build_locator_candidates(
        matched
    )

    selector_map = build_selector_map(
        matched
    )

    for element in matched:

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

    # Strategy 2
    closed = await recover_popup(page)

    if closed:
        await page.wait_for_timeout(300)

    return