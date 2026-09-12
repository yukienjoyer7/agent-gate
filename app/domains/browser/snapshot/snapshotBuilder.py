"""
Snapshot Builder (Semantic Layer)

Build semantic interactive elements from Playwright's ARIA snapshot. If the
installed Playwright version does not expose aria_snapshot(), fall back to a
DOM-based extractor.
"""

import re
from typing import Any

import yaml

from app.domains.browser.snapshot.classifyRsik import classify_risk

INTERACTIVE_ROLES = {
    "button",
    "link",
    "textbox",
    "searchbox",
    "checkbox",
    "radio",
    "switch",
    "combobox",
    "tab",
    "menuitem",
    "spinbutton",
    "option",
}

# Native selects can contribute hundreds of ``option`` nodes.  Keep the
# actionable controls at the front of both planner and runtime snapshots.
ACTIONABLE_ROLE_PRIORITY = {
    "textbox": 0,
    "searchbox": 0,
    "spinbutton": 0,
    "button": 1,
    "checkbox": 2,
    "radio": 3,
    "switch": 3,
    "combobox": 4,
    "link": 5,
    "textarea": 6,
    "tab": 7,
    "menuitem": 7,
    "option": 99,
}


async def build_semantic_elements(page) -> list[dict[str, str]]:
    body = page.locator("body")
    aria_snapshot = getattr(body, "aria_snapshot", None)

    if aria_snapshot is not None:
        try:
            yaml_text = await aria_snapshot()
            tree = yaml.safe_load(yaml_text)
            semantic_elements: list[dict[str, str]] = []
            parse_node(tree, semantic_elements)
            if semantic_elements:
                return semantic_elements
        except Exception:
            pass

    return await _fallback_interactive_elements(page)


def enrich_semantic_elements(elements: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {
            **element,
            "risk_hint": classify_risk(element["label"]),
        }
        for element in elements
    ]


def prioritize_interactive_elements(
    elements: list[dict[str, Any]], max_elements: int | None = None
) -> list[dict[str, Any]]:
    """Prioritize actionable controls and avoid option floods.

    Element order is stable within each role priority. Options are retained
    only when a page contains no other actionable element; the combobox/select
    itself remains visible with compact selection metadata.
    """
    indexed = list(enumerate(elements))

    def role_of(item: dict[str, Any]) -> str:
        semantic = item.get("semantic")
        return str((semantic or {}).get("role") or item.get("role") or "").lower()

    actionable = [(index, item) for index, item in indexed if role_of(item) != "option"]
    candidates = actionable or indexed
    candidates.sort(key=lambda pair: (ACTIONABLE_ROLE_PRIORITY.get(role_of(pair[1]), 50), pair[0]))
    if max_elements is None:
        return [item for _, item in candidates]
    return [item for _, item in candidates[:max_elements]]


def parse_node(node: Any, output: list[dict[str, str]]) -> None:
    if node is None:
        return

    if isinstance(node, str):
        match = re.match(r'^(\w+)(?:\s+"([^"]+)")?', node)
        if match:
            role = match.group(1)
            label = match.group(2) or ""
            if role in INTERACTIVE_ROLES:
                output.append({"role": role, "label": label})
        return

    if isinstance(node, dict):
        for key, value in node.items():
            parse_node(key, output)
            parse_node(value, output)
        return

    if isinstance(node, list):
        for child in node:
            parse_node(child, output)


async def _fallback_interactive_elements(page) -> list[dict[str, str]]:
    elements = await page.locator(
        "button,a,input,textarea,select,[role],[aria-label],[data-testid]"
    ).evaluate_all(
        """
(nodes) => nodes.map((node) => {
    const tag = node.tagName.toLowerCase();
    const inputType = (node.getAttribute("type") || "").toLowerCase();
    const role = node.getAttribute("role") || inferRole(tag, inputType);
    const label = (
        node.getAttribute("aria-label") ||
        node.innerText ||
        node.value ||
        node.getAttribute("placeholder") ||
        node.getAttribute("name") ||
        node.getAttribute("title") ||
        ""
    ).trim();

    return { role, label };
}).filter((element) => element.role && element.label);

function inferRole(tag, inputType) {
    if (tag === "a") return "link";
    if (tag === "button") return "button";
    if (tag === "textarea") return "textbox";
    if (tag === "select") return "combobox";
    if (tag !== "input") return null;
    if (inputType === "checkbox") return "checkbox";
    if (inputType === "radio") return "radio";
    if (inputType === "search") return "searchbox";
    if (inputType === "number") return "spinbutton";
    return "textbox";
}
"""
    )
    return [
        {"role": element["role"], "label": element["label"]}
        for element in elements
        if element["role"] in INTERACTIVE_ROLES
    ]
