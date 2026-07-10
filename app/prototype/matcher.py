"""
==========================================================
Matcher
==========================================================

Responsibility
--------------
Merge Semantic Elements and Execution Metadata into one
collection of Matched Elements.

The matcher assigns a stable element_id that is later used
by the Browser Executor.

Output
------
[
    {
        element_id,
        role,
        label,
        risk_hint,
        dom
    }
]
==========================================================
"""


def build_matched_elements(
    semantic_elements: list,
    execution_metadata: list
) -> list:

    matched = []

    current_id = 1

    for semantic, execution in zip(
        semantic_elements,
        execution_metadata
    ):

        matched.append({

            "element_id":
                str(current_id),

            "role":
                semantic["role"],

            "label":
                semantic["label"],

            "risk_hint":
                semantic["risk_hint"],

            "dom":
                execution["dom"]

        })

        current_id += 1

    return matched