from playwright.async_api import Error, Locator


def build_locator(page, selector) -> Locator:
    strategy = selector["strategy"]

    if strategy == "role":
        return page.get_by_role(
            selector["role"],
            name=selector["name"],
        )

    elif strategy == "label":
        return page.get_by_label(
            selector["value"],
        )

    elif strategy == "placeholder":
        return page.get_by_placeholder(
            selector["value"],
        )

    elif strategy == "text":
        return page.get_by_text(
            selector["value"],
        )

    elif strategy == "testid":
        return page.get_by_test_id(
            selector["value"],
        )

    elif strategy == "css":
        return page.locator(
            selector["value"],
        )

    raise ValueError(f"Unknown strategy: {strategy}")


async def resolve_locator(
    page,
    selector_map,
    element_id,
) -> Locator:
    """
    Resolve a selector into a usable Playwright locator.

    Resolution order:
        1. Primary selector
        2. Fallback selectors

    Returns the first visible and enabled locator.
    """

    if element_id not in selector_map:
        raise KeyError(f"Unknown element_id: {element_id}")

    selector = selector_map[element_id]

    candidates = [
        selector["primary"],
        *selector.get("fallbacks", []),
    ]

    errors = []

    for candidate in candidates:
        try:
            locator = build_locator(page, candidate)

            count = await locator.count()

            if count == 0:
                errors.append(f"{candidate}: no matches")
                continue

            # Multiple matches are acceptable.
            # Use the first visible one.
            locator = locator.first

            await locator.wait_for(
                state="visible",
                timeout=2000,
            )

            try:
                if not await locator.is_enabled():
                    errors.append(f"{candidate}: disabled")
                    continue
            except Error:
                # Some element types don't support is_enabled().
                pass

            return locator

        except Exception as exc:
            errors.append(f"{candidate}: {exc}")

    raise RuntimeError(
        f"Unable to locate usable element '{element_id}'.\n"
        f"Tried:\n- " + "\n- ".join(errors)
    )