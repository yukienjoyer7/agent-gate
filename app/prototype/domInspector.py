"""
==========================================================
DOM Inspector
==========================================================

Responsibility
--------------
Given semantic elements extracted from the ARIA snapshot,
inspect the corresponding DOM nodes and collect execution
metadata.

This module DOES NOT

- build snapshots
- build selector maps
- execute browser actions

Output
------
[
    {
        "role": "...",
        "label": "...",
        "locator_candidates": [...],
        "dom": {...}
    }
]
==========================================================
"""

from playwright.async_api import Page

# Main Functionpip show playwright

async def build_execution_metadata(
    page: Page,
    semantic_elements: list
) -> list:

    execution_metadata = []

    for element in semantic_elements:

        metadata = await inspect_element(
            page,
            element
        )

        if metadata:
            execution_metadata.append(metadata)

    return execution_metadata

# Inspect One Semantic Element

async def inspect_element(
    page: Page,
    semantic: dict
):

    role = semantic["role"]
    label = semantic["label"]

    locator = await resolve_locator(
        page,
        role,
        label
    )

    if locator is None:
        return None

    dom = await locator.evaluate(
        """
(node)=>({

    tag:
        node.tagName.toLowerCase(),

    id:
        node.id || null,

    class:
        node.className || null,

    name:
        node.getAttribute("name"),

    title:
        node.getAttribute("title"),

    placeholder:
        node.getAttribute("placeholder"),

    aria_label:
        node.getAttribute("aria-label"),

    test_id:
        node.dataset.testid || null,

    href:
        node.href || null,

    visible:
        node.checkVisibility(),

    disabled:
        node.disabled || false

})
"""
    )

    return {

        "semantic": semantic,

        "dom": dom

    }

# Resolve Locator
async def resolve_locator(
    page: Page,
    role: str,
    label: str
):
    """
    Resolve one semantic element into a Playwright Locator.
    """

    try:

        locator = page.get_by_role(
            role,
            name=label
        )

        if await locator.count() == 1:
            return locator

    except:
        pass

    return None

