"""
Matcher

Merge resolved execution metadata into matched elements with stable element IDs.
The IDs are later used by the browser executor.
"""

from typing import Any


def build_matched_elements(
    semantic_elements: list[dict[str, str]],
    execution_metadata: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    del semantic_elements

    matched = []
    for index, execution in enumerate(execution_metadata, start=1):
        semantic = execution["semantic"]
        matched.append(
            {
                "element_id": str(index),
                "role": semantic["role"],
                "label": semantic["label"],
                "risk_hint": semantic["risk_hint"],
                "dom": execution["dom"],
            }
        )

    return matched
