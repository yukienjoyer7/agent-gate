"""
==========================================================
Snapshot Builder (Semantic Layer)
==========================================================

Responsibility
--------------
Build semantic elements from Playwright's ARIA Snapshot.

Output
------
[
    {
        "role": "...",
        "label": "...",
        "risk_hint": "..."
    }
]
==========================================================
"""

import re
import yaml

from classifyRsik import classify_risk


# ----------------------------------------------------------
# Interactive ARIA Roles
# ----------------------------------------------------------

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


# ----------------------------------------------------------
# Public API
# ----------------------------------------------------------

# Build Semantic Elements
async def build_semantic_elements(page):

    yaml_text = await page.locator("body").aria_snapshot()

    tree = yaml.safe_load(yaml_text)

    semantic_elements = []

    parse_node(tree, semantic_elements)

    return semantic_elements

# Semantic Enrichment
def enrich_semantic_elements(elements):

    snapshot = []

    for element in elements:

        snapshot.append({

            **element,

            "risk_hint":
                classify_risk(
                    element["label"]
                )

        })

    return snapshot


# ----------------------------------------------------------
# Recursive Parser
# ----------------------------------------------------------

def parse_node(node, output):

    if node is None:
        return

    # ----------------------------------------
    # String
    # ----------------------------------------

    if isinstance(node, str):

        match = re.match(
            r'^(\w+)(?:\s+"([^"]+)")?',
            node
        )

        if match:

            role = match.group(1)

            label = match.group(2) or ""

            if role in INTERACTIVE_ROLES:

                output.append({

                    "role": role,

                    "label": label

                })

        return

    # ----------------------------------------
    # Dictionary
    # ----------------------------------------

    if isinstance(node, dict):

        for key, value in node.items():

            parse_node(key, output)

            parse_node(value, output)

    # ----------------------------------------
    # List
    # ----------------------------------------

    elif isinstance(node, list):

        for child in node:

            parse_node(child, output)