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

            if await locator.count() != 1:
                continue

            await locator.wait_for(
                state="visible",
                timeout=1000
            )

            return locator

        except Exception:

            continue


    raise RuntimeError(
        f"Unable to locate element {element_id}"
    )
