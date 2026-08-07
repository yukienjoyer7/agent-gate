import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domains.agent.services import run_guarded_action


SCENARIOS = {
    "local_file_read": {
        "source": "cli",
        "domain": "filesystem",
        "action_type": "FILE_READ",
        "target_system": "local_file",
        "target": "sample.txt",
        "risk_hint": "file_read",
        "payload": {"action": "read", "path": "sample.txt"},
    },
    "browser_snapshot": {
        "source": "cli",
        "domain": "browser",
        "action_type": "BROWSER_SNAPSHOT",
        "target_system": "browser",
        "target": "https://example.test/demo",
        "risk_hint": "unknown",
        "payload": {"url": "https://example.test/demo", "title": "Demo page"},
    },
    "payload_sanitize": {
        "source": "cli",
        "domain": "filesystem",
        "action_type": "FILE_READ",
        "target_system": "local_file",
        "target": "sample.txt",
        "risk_hint": "file_read",
        # "note" carries an email address -- decide() detects it, redacts
        # it, and re-decides before this ever reaches the executor. See
        # app/domains/guardrail/detectors/sensitive_entities.py.
        "payload": {
            "action": "read",
            "path": "sample.txt",
            "note": "flag any issues to reviewer@example.com",
        },
    },
    "ask_user_low_confidence": {
        "source": "cli",
        "domain": "filesystem",
        "action_type": "FILE_READ",
        "target_system": "local_file",
        "target": "sample.txt",
        "risk_hint": "file_read",
        # confidence below settings.ASK_USER_CONFIDENCE_THRESHOLD (default
        # 0.5) routes to Decision.ASK_USER instead of ALLOW -- goes into
        # the pending_user_questions queue via
        # POST /ask-user/{action_id}/respond, not executed here. See
        # app/domains/clarification/services/clarification_service.py.
        "confidence": 0.3,
        "payload": {"action": "read", "path": "sample.txt"},
    },
}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario", choices=SCENARIOS)
    args = parser.parse_args()

    event = await run_guarded_action(SCENARIOS[args.scenario])
    print(json.dumps(event.model_dump(mode="json"), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
