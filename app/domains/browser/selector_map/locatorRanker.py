"""
==========================================================
Locator Ranker
==========================================================

Responsibility
--------------
Verify candidate locators and choose the best one.

Output
------
Selector Map
==========================================================
"""


async def build_selector_map(
    page,
    locator_candidates
):

    selector_map = {}

    for element in locator_candidates:

        ranked = []

        for candidate in element["locator_candidates"]:

            locator = await build_locator(
                page,
                candidate
            )

            if locator is None:
                continue

            try:

                count = await locator.count()

                if count == 1:

                    ranked.append(candidate)

            except Exception:

                continue

        if ranked:

            selector_map[
                element["element_id"]
            ] = {

                "primary":
                    ranked[0],

                "fallbacks":
                    ranked[1:]

            }

    return selector_map

async def build_locator(
    page,
    candidate
):

    strategy = candidate["strategy"]

    if strategy == "role":

        return page.get_by_role(

            candidate["role"],

            name=candidate["name"]

        )

    if strategy == "label":

        return page.get_by_label(

            candidate["value"]

        )

    if strategy == "placeholder":

        return page.get_by_placeholder(

            candidate["value"]

        )

    if strategy == "text":

        return page.get_by_text(

            candidate["value"]

        )

    if strategy == "testid":

        return page.get_by_test_id(

            candidate["value"]

        )

    if strategy == "css":

        return page.locator(

            candidate["value"]

        )

    return None