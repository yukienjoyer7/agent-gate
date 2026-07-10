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

# Locator Builder
def build_locator(page, selector):

    strategy = selector["strategy"]

    if strategy == "role":

        return page.get_by_role(
            selector["role"],
            name=selector["name"]
        )

    elif strategy == "label":

        return page.get_by_label(
            selector["value"]
        )

    elif strategy == "placeholder":

        return page.get_by_placeholder(
            selector["value"]
        )

    elif strategy == "text":

        return page.get_by_text(
            selector["value"]
        )

    elif strategy == "testid":

        return page.get_by_test_id(
            selector["value"]
        )

    elif strategy == "css":

        return page.locator(
            selector["value"]
        )

    raise ValueError(
        f"Unknown strategy: {strategy}"
    )

# Resolve Locator
async def resolve_locator(
    page,
    selector_map,
    element_id
):

    selector = selector_map[element_id]

    candidates = [
        selector["primary"],
        *selector["fallbacks"]
    ]

    for candidate in candidates:

        locator = build_locator(
            page,
            candidate
        )

        try:

            if await locator.count() == 1:

                return locator

        except Exception:

            continue

    raise RuntimeError(
        f"Unable to locate element {element_id}"
    )

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