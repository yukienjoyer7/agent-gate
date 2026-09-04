from __future__ import annotations

import asyncio

from .recovery import recover

MAX_RECOVERY_ATTEMPTS = 3

# Number of blocked sample points required before
# we consider an element "occluded".
OCCLUSION_THRESHOLD = 3


async def ensure_actionable(page, locator) -> None:
    """
    Ensure a locator is ready for interaction.

    Steps:
    1. Wait until attached.
    2. Scroll into view.
    3. Wait until visible.
    4. Wait until enabled (if supported).
    5. Verify it is not significantly occluded.
    """

    await locator.wait_for(state="attached")
    await locator.scroll_into_view_if_needed()
    await locator.wait_for(state="visible")

    try:
        await locator.wait_for(state="enabled")
    except Exception:
        # Older Playwright versions don't support "enabled".
        pass

    last_error = None

    for attempt in range(MAX_RECOVERY_ATTEMPTS):
        try:
            occluded = await is_occluded(page, locator)

            if not occluded:
                return

            last_error = RuntimeError("Element is occluded.")

        except Exception as exc:
            last_error = exc

        await recover(page)

        try:
            await locator.scroll_into_view_if_needed()
        except Exception:
            pass

        await asyncio.sleep(0.5)

    raise RuntimeError(
        f"Element is still occluded after "
        f"{MAX_RECOVERY_ATTEMPTS} recovery attempts."
    ) from last_error


async def is_occluded(page, locator) -> bool:
    """
    Returns True if most sampled points of the element
    are covered by another element.
    """

    blocked = await locator.evaluate(
        f"""
        (el) => {{
            const rect = el.getBoundingClientRect();

            if (
                rect.width <= 0 ||
                rect.height <= 0
            ) {{
                return {OCCLUSION_THRESHOLD};
            }}

            const inset = Math.min(
                5,
                rect.width / 4,
                rect.height / 4
            );

            const points = [
                [rect.left + rect.width / 2,
                 rect.top + rect.height / 2],

                [rect.left + inset,
                 rect.top + inset],

                [rect.right - inset,
                 rect.top + inset],

                [rect.left + inset,
                 rect.bottom - inset],

                [rect.right - inset,
                 rect.bottom - inset]
            ];

            let blocked = 0;

            for (const [x, y] of points) {{

                const top = document.elementFromPoint(x, y);

                if (!top) {{
                    blocked++;
                    continue;
                }}

                if (
                    top !== el &&
                    !el.contains(top) &&
                    !top.contains(el)
                ) {{
                    blocked++;
                }}
            }}

            return blocked;
        }}
        """
    )

    return blocked >= OCCLUSION_THRESHOLD