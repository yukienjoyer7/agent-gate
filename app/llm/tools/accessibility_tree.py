"""
Accessibility Tree Tool (for LLM function calling)
===================================================

Gives tool-calling models a way to inspect the *real* interactive elements
of a web page before they emit click/type/scroll steps. This eliminates the
failure mode where the LLM guesses an element label that does not match the
page's actual accessible label/name, which previously made the browser
executor fail to resolve the element.

The tool reuses the same pipeline as the runtime browser agent:

- ``build_semantic_elements`` (ARIA snapshot / DOM fallback)
- ``build_execution_metadata`` (DOM attributes such as ``name``, ``placeholder``,
  ``id``, ``aria-label``, ``data-testid``)

so the labels exposed to the model are exactly the ones the executor will
resolve against.
"""

from __future__ import annotations

import asyncio
from typing import Any

from playwright.async_api import async_playwright

from app.config.settings import get_settings
from app.domains.browser.selector_map.domInspector import build_execution_metadata
from app.domains.browser.snapshot.snapshotBuilder import (
    build_semantic_elements,
    enrich_semantic_elements,
)

TOOL_NAME = "get_accessibility_tree"

TOOL_DEFINITION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": (
            "Open the given URL in a headless browser and return the page's "
            "accessibility tree: interactive elements with their exact ARIA role, "
            "accessible label, and DOM attributes (name, placeholder, aria-label, "
            "id, data-testid, text). Use this BEFORE emitting BROWSER_CLICK, "
            "BROWSER_TYPE, BROWSER_SCROLL or BROWSER_SELECT steps so the label/role "
            "you put in the plan payload match the real page exactly."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Full URL of the page to inspect (e.g. https://example.com).",
                },
                "wait_until": {
                    "type": "string",
                    "enum": ["domcontentloaded", "load", "networkidle"],
                    "default": "domcontentloaded",
                    "description": "Playwright navigation wait condition.",
                },
                "timeout_ms": {
                    "type": "integer",
                    "default": 15000,
                    "description": "Navigation timeout in milliseconds.",
                },
            },
            "required": ["url"],
        },
    },
}

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


async def get_accessibility_tree(
    *,
    url: str,
    wait_until: str = "domcontentloaded",
    timeout_ms: int = 15_000,
) -> dict[str, Any]:
    """Fetch the accessibility tree of ``url`` as a JSON-safe dict.

    Returns a stable, serializable structure so the LLM can pick exact
    labels/roles. Never raises: errors are converted into an ``error`` dict
    that the model can react to.
    """
    try:
        return await _fetch_tree(url, wait_until=wait_until, timeout_ms=timeout_ms)
    except Exception as exc:  # noqa: BLE001 - errors are fed back to the model
        return {"error": f"{exc.__class__.__name__}: {str(exc)[:500]}"}


async def _fetch_tree(
    url: str,
    *,
    wait_until: str,
    timeout_ms: int,
) -> dict[str, Any]:
    settings = get_settings()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=settings.PLAYWRIGHT_HEADLESS,
            args=[
                "--disable-http2",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        try:
            page = await browser.new_page(
                user_agent=_UA,
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;q=0.9,"
                        "image/avif,image/webp,*/*;q=0.8"
                    ),
                    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                    "Upgrade-Insecure-Requests": "1",
                },
            )
            await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            # Align with the executor (which settles BROWSER_SETTLE_MS before
            # building its snapshot): SPA pages render interactive headers
            # slightly after domcontentloaded, so settle before inspecting the
            # tree or the model may conclude an element is missing (e.g.
            # YouTube's search box).
            await asyncio.sleep(settings.BROWSER_SETTLE_MS / 1000)

            semantic_elements = await build_semantic_elements(page)
            semantic_snapshot = enrich_semantic_elements(semantic_elements)
            metadata = await build_execution_metadata(page, semantic_snapshot)

            # Cap the tree so a dense page cannot blow up the model context
            # window when the result is fed back into the conversation.
            max_elements = settings.PLAYWRIGHT_MAX_ELEMENTS
            elements: list[dict[str, Any]] = []
            for index, item in enumerate(metadata[:max_elements], start=1):
                semantic = item["semantic"]
                dom = item["dom"]
                elements.append(
                    {
                        "element_id": str(index),
                        "role": semantic["role"],
                        "label": (semantic["label"] or "")[:120],
                        "name": dom.get("name"),
                        "placeholder": dom.get("placeholder"),
                        "aria_label": dom.get("aria_label"),
                        "id": dom.get("id"),
                        "test_id": dom.get("test_id"),
                        "text": (dom.get("text") or "")[:80],
                    }
                )

            return {
                "url": url,
                "final_url": page.url,
                "count": len(elements),
                "truncated": len(metadata) > max_elements,
                "elements": elements,
            }
        finally:
            await browser.close()
