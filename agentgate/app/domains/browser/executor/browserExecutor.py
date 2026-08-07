"""
==========================================================
Browser Executor
==========================================================

Responsibility
--------------
Execute browser actions using the Selector Map.

Supported Actions
-----------------
- click
- fill
- scroll
- screenshot

The executor NEVER searches the page.

It ONLY uses the Selector Map produced by the
Locator Ranker.

==========================================================
"""

from pathlib import Path
from app.domains.browser.executor.actionability import ensure_actionable
from app.domains.browser.executor.locatorResolver import resolve_locator

# Click
async def click(
    page,
    selector_map,
    element_id
):

    locator = await resolve_locator(
        page,
        selector_map,
        element_id
    )

    await ensure_actionable(
        page,
        locator
    )

    await locator.click()

# Fill
async def fill(
    page,
    selector_map,
    element_id,
    value
):

    locator = await resolve_locator(
        page,
        selector_map,
        element_id
    )

    await ensure_actionable(
        page,
        locator
    )

    await locator.fill(value)

# Scroll
async def scroll(
    page,
    selector_map,
    element_id
):

    locator = await resolve_locator(
        page,
        selector_map,
        element_id
    )

    await ensure_actionable(
        page,
        locator
    )

    await locator.scroll_into_view_if_needed()

# Screenshot
async def screenshot(
    page,
    path="screenshot.png",
    full_page=True
):

    Path(path).parent.mkdir(
        parents=True,
        exist_ok=True
    )

    await page.screenshot(
        path=path,
        full_page=full_page
    )

# Dispatcher
async def execute_action(
    page,
    selector_map,
    action
):

    action_type = action["type"]

    if action_type == "click":

        await click(
            page,
            selector_map,
            action["element_id"]
        )

    elif action_type == "fill":

        await fill(
            page,
            selector_map,
            action["element_id"],
            action["value"]
        )

    elif action_type == "scroll":

        await scroll(
            page,
            selector_map,
            action["element_id"]
        )

    elif action_type == "screenshot":

        await screenshot(
            page,
            action.get(
                "path",
                "screenshot.png"
            )
        )

    else:

        raise ValueError(
            f"Unsupported action: {action_type}"
        )