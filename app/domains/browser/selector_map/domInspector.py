"""
DOM Inspector

Resolve semantic browser elements to DOM nodes and collect execution metadata.
This module does not build selector maps or execute actions.
"""

from typing import Any

from playwright.async_api import Page


async def build_execution_metadata(
    page: Page,
    semantic_elements: list[dict[str, str]],
) -> list[dict[str, Any]]:
    execution_metadata = []

    for element in semantic_elements:
        metadata = await inspect_element(page, element)
        if metadata:
            execution_metadata.append(metadata)

    return execution_metadata


async def inspect_element(
    page: Page,
    semantic: dict[str, str],
) -> dict[str, Any] | None:
    role = semantic["role"]
    label = semantic["label"]
    locator = await resolve_locator(page, role, label)

    if locator is None:
        return None

    dom = await locator.evaluate(
        """
(node)=>({
    tag: node.tagName.toLowerCase(),
    id: node.id || null,
    class: typeof node.className === "string" ? node.className : null,
    text: (node.innerText || node.textContent || "").trim() || null,
    name: node.getAttribute("name"),
    title: node.getAttribute("title"),
    placeholder: node.getAttribute("placeholder"),
    aria_label: node.getAttribute("aria-label"),
    test_id: node.dataset.testid || null,
    href: node.getAttribute("href"),
    visible: typeof node.checkVisibility === "function" ? node.checkVisibility() : true,
    disabled: Boolean(node.disabled)
})
"""
    )

    return {"semantic": semantic, "dom": dom}


async def resolve_locator(page: Page, role: str, label: str):
    if not label:
        return None

    try:
        locator = page.get_by_role(role, name=label)
        if await locator.count() == 1:
            return locator
    except Exception:
        pass

    return None
