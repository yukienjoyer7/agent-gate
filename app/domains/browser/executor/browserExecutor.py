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

import asyncio
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

# Submit
async def submit(
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

    # Press Enter on the element (search box / input / focused button) to
    # trigger the form/search submission instead of a plain click, which only
    # focuses inputs (e.g. YouTube's search box).
    await locator.press("Enter")

# Scroll
async def scroll(
    page,
    selector_map,
    element_id=None,
    *,
    x=0,
    y=0,
    duration_ms=0
):

    if element_id:
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
        return

    if duration_ms and (x or y):
        steps = max(1, duration_ms // 50)
        step_x = x / steps
        step_y = y / steps
        for _ in range(steps):
            await page.mouse.wheel(step_x, step_y)
            await asyncio.sleep(0.05)
        return

    if action.get("top"):
        await page.evaluate("window.scrollTo(0, 0)")
        return

    await page.evaluate(
        "(args) => window.scrollBy(args.x, args.y)",
        {"x": x, "y": y},
    )

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

    elif action_type == "submit":

        await submit(
            page,
            selector_map,
            action["element_id"]
        )

    elif action_type == "scroll":

        await scroll(
            page,
            selector_map,
            action.get("element_id"),
            x=action.get("x", 0),
            y=action.get("y", 0),
            duration_ms=action.get("duration_ms", 0),
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