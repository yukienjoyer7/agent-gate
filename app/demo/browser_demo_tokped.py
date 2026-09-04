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

URL = "https://www.shopee.co.id"
OUTPUT_DIR = "artifacts/browser_demo_tokped"


async def main() -> None:
    artifact_result = await capture_browser_artifacts(
        url=URL,
        output_dir=OUTPUT_DIR,
        wait_until="load",
        timeout_ms=60_000,
    )
    print("Captured browser artifacts:")
    print(artifact_result)

    try:
        result = await run_browser_prototype_agent(
            url=URL,
            timeout_ms=60_000,
            wait_until="load",
            settle_ms=20000,
            actions=[
                {
                    "type": "screenshot",
                    "path": f"{OUTPUT_DIR}/open_initial.png",
                    "delay_ms": 2000,
                },
                {
                    "type": "scroll",
                    "y": 2000,
                    "duration_ms": 5000,
                },
                {
                    "type": "scroll",
                    "top": True,
                },
                {
                    "type": "screenshot",
                    "path": f"{OUTPUT_DIR}/initial_snapshot.png",
                    "delay_ms": 500,
                },
                {
                    "type": "click",
                    "label": "Masuk",
                    "delay_ms": 2000,
                },
                {
                    "type": "screenshot",
                    "path": f"{OUTPUT_DIR}/after_click_1.png",
                    "delay_ms": 500,
                },
            ],
        )
    except Exception as exc:  # pragma: no cover - demo resilience
        print("Browser prototype agent failed:")
        print(exc)
        return

    print("Browser prototype agent result:")
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
