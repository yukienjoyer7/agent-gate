"""
==========================================================
Locator Generator
==========================================================

Responsibility
--------------
Generate every possible Playwright locator strategy
for each matched element.

This module DOES NOT

- execute Playwright
- rank locators
- click anything

Output
------
[
    {
        element_id,
        locator_candidates
    }
]
==========================================================
"""


def build_locator_candidates(matched_elements):

    generated = []

    for element in matched_elements:

        dom = element["dom"]

        candidates = []

        # --------------------------------------------
        # Role
        # --------------------------------------------

        if (
            element["role"] and
            element["label"]
        ):

            candidates.append({

                "strategy": "role",

                "role":
                    element["role"],

                "name":
                    element["label"]

            })

        # --------------------------------------------
        # ARIA Label
        # --------------------------------------------

        if dom.get("aria_label"):

            candidates.append({

                "strategy": "label",

                "value":
                    dom["aria_label"]

            })

        # --------------------------------------------
        # Placeholder
        # --------------------------------------------

        if dom.get("placeholder"):

            candidates.append({

                "strategy": "placeholder",

                "value":
                    dom["placeholder"]

            })

        # --------------------------------------------
        # Text
        # --------------------------------------------

        if dom.get("text"):

            candidates.append({

                "strategy": "text",

                "value":
                    dom["text"]

            })

        # --------------------------------------------
        # Test ID
        # --------------------------------------------

        if dom.get("test_id"):

            candidates.append({

                "strategy": "testid",

                "value":
                    dom["test_id"]

            })

        # --------------------------------------------
        # HTML ID
        # --------------------------------------------

        if dom.get("id"):

            candidates.append({

                "strategy": "css",

                "value":
                    f'#{dom["id"]}'

            })

        generated.append({

            "element_id":
                element["element_id"],

            "role":
                element["role"],

            "label":
                element["label"],

            "risk_hint":
                element["risk_hint"],

            "locator_candidates":
                candidates

        })

    return generated