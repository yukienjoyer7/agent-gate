from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from playwright.async_api import async_playwright

from app.domains.browser.selector_map.domInspector import build_execution_metadata
from app.domains.browser.selector_map.locatorGenerator import build_locator_candidates
from app.domains.browser.selector_map.locatorRanker import build_selector_map
from app.domains.browser.selector_map.matcher import build_matched_elements
from app.domains.browser.snapshot.snapshotBuilder import (
    build_semantic_elements,
    enrich_semantic_elements,
)


def build_public_snapshot(matched_elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "element_id": element["element_id"],
            "role": element["role"],
            "label": element["label"],
            "risk_hint": element["risk_hint"],
            "dom": element["dom"],
        }
        for element in matched_elements
    ]


async def capture_browser_artifacts(
    *,
    url: str,
    output_dir: str | Path,
    headless: bool = True,
    wait_until: str = "domcontentloaded",
    timeout_ms: int = 15_000,
) -> dict[str, Any]:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = output_path / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless)
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until=wait_until, timeout=timeout_ms)

            screenshot_path = run_dir / "screenshot.png"
            await page.screenshot(path=str(screenshot_path), full_page=True)

            semantic_elements = await build_semantic_elements(page)
            semantic_snapshot = enrich_semantic_elements(semantic_elements)
            execution_metadata = await build_execution_metadata(page, semantic_snapshot)
            matched_elements = build_matched_elements(semantic_snapshot, execution_metadata)
            locator_candidates = build_locator_candidates(matched_elements)
            selector_map = await build_selector_map(page, locator_candidates)

            snapshot_payload = build_public_snapshot(matched_elements)
            (run_dir / "snapshot.json").write_text(
                json.dumps(snapshot_payload, indent=2), encoding="utf-8"
            )
            (run_dir / "selector_map.json").write_text(
                json.dumps(selector_map, indent=2), encoding="utf-8"
            )
            (run_dir / "metadata.json").write_text(
                json.dumps(
                    {
                        "url": url,
                        "title": await page.title(),
                        "final_url": page.url,
                        "created_at": datetime.now().isoformat(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            return {
                "output_dir": str(run_dir),
                "screenshot": str(screenshot_path),
                "snapshot": str(run_dir / "snapshot.json"),
                "selector_map": str(run_dir / "selector_map.json"),
                "metadata": str(run_dir / "metadata.json"),
            }
        finally:
            await browser.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture browser snapshot, selector map, and screenshot")
    parser.add_argument("--url", required=True, help="Target URL to inspect")
    parser.add_argument(
        "--output-dir",
        default="artifacts/browser",
        help="Directory where the captured files will be written",
    )
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run browser in headless mode (default: enabled)",
    )
    parser.add_argument(
        "--wait-until",
        default="domcontentloaded",
        help="Playwright wait_until value (default: domcontentloaded)",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=15_000,
        help="Page load timeout in milliseconds",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    result = await capture_browser_artifacts(
        url=args.url,
        output_dir=args.output_dir,
        headless=args.headless,
        wait_until=args.wait_until,
        timeout_ms=args.timeout_ms,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
