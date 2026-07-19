import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.demo.save_browser_artifacts import capture_browser_artifacts  # noqa: E402
from app.domains.agent.services.browser_prototype_agent import (  # noqa: E402
    run_browser_prototype_agent,
)

URL = "https://playwright.dev"
OUTPUT_DIR = "artifacts/browser_demo"


async def main() -> None:
    artifact_result = await capture_browser_artifacts(
        url=URL,
        output_dir=OUTPUT_DIR,
    )
    print("Captured browser artifacts:")
    print(artifact_result)

    try:
        result = await run_browser_prototype_agent(
            url=URL,
            actions=[
                {
                    "type": "screenshot",
                    "path": f"{OUTPUT_DIR}/initial_page.png",
                },
                {
                    "type": "screenshot",
                    "path": f"{OUTPUT_DIR}/after_click_1.png",
                },
                {
                    "type": "click",
                    "label": "Switch between dark and light mode (currently system mode)",
                },
                {
                    "type": "click",
                    "label": "Switch between dark and light mode (currently light mode)",
                },
                
                {
                    "type": "screenshot",
                    "path": f"{OUTPUT_DIR}/after_click_2.png",
                },
            ],
            # timeout_ms=60_000,
            wait_until="load",
            # settle_ms=20000,
        )
    except Exception as exc:  # pragma: no cover - demo resilience
        print("Browser prototype agent failed:")
        print(exc)
        return

    print("Browser prototype agent result:")
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
