from __future__ import annotations

import argparse
import sys
from typing import Any

import httpx

from app.config.settings import get_settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Register the AgentGate Telegram webhook.")
    parser.add_argument(
        "--url",
        required=True,
        help="Public HTTPS webhook URL, e.g. https://example.com/api/v1/telegram/webhook",
    )
    args = parser.parse_args()

    settings = get_settings()
    if not settings.TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN is not configured", file=sys.stderr)
        return 1
    if not settings.TELEGRAM_WEBHOOK_SECRET:
        print("TELEGRAM_WEBHOOK_SECRET is not configured", file=sys.stderr)
        return 1

    api_base = settings.TELEGRAM_API_BASE.rstrip("/")
    url = f"{api_base}/bot{settings.TELEGRAM_BOT_TOKEN}/setWebhook"
    body = {"url": args.url, "secret_token": settings.TELEGRAM_WEBHOOK_SECRET}

    try:
        response = httpx.post(url, json=body, timeout=10)
    except httpx.TimeoutException:
        print("Telegram setWebhook request timed out", file=sys.stderr)
        return 1
    except httpx.HTTPError:
        print("Telegram setWebhook request failed", file=sys.stderr)
        return 1

    data = _safe_json(response)
    if response.status_code < 200 or response.status_code >= 300:
        print(f"Telegram returned HTTP {response.status_code}", file=sys.stderr)
        return 1
    if not isinstance(data, dict) or data.get("ok") is not True:
        description = ""
        if isinstance(data, dict):
            description = str(data.get("description") or "")
        print(
            f"Telegram setWebhook failed: {description[:300] or 'unknown error'}", file=sys.stderr
        )
        return 1

    print(f"Telegram webhook configured for {args.url}")
    return 0


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
