from .recovery import recover
MAX_RECOVERY_ATTEMPTS = 2

async def ensure_actionable(
    page,
    locator,
):

    await locator.scroll_into_view_if_needed()

    await locator.wait_for(
        state="visible"
    )

    for _ in range(MAX_RECOVERY_ATTEMPTS):

        if not await is_occluded(page, locator):
            return

        await recover(page)

    raise RuntimeError(
        "Element is still occluded."
    )
        
async def is_occluded(page, locator):

    return await locator.evaluate(
        """
        (el) => {

            const rect = el.getBoundingClientRect();

            const points = [

                [rect.left + rect.width/2,
                 rect.top + rect.height/2],

                [rect.left + 5,
                 rect.top + 5],

                [rect.right - 5,
                 rect.top + 5],

                [rect.left + 5,
                 rect.bottom - 5],

                [rect.right - 5,
                 rect.bottom - 5]

            ];

            return points.some(([x,y])=>{

                const top=document.elementFromPoint(x,y);

                return !(top===el||el.contains(top));

            });


        }
        """
    )