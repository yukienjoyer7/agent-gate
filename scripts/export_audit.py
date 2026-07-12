import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domains.audit.repositories import AuditRepository


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--latest", action="store_true")
    args = parser.parse_args()

    repo = AuditRepository()
    if args.latest:
        event = repo.latest()
        print(json.dumps(event.model_dump(mode="json"), indent=2) if event else "{}")
        return

    for event in repo.list():
        print(json.dumps(event.model_dump(mode="json")))


if __name__ == "__main__":
    main()
