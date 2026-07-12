import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tracing import TraceWriter


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--latest", action="store_true")
    args = parser.parse_args()

    traces = TraceWriter().list()
    if args.latest:
        print(json.dumps(traces[-1].model_dump(mode="json"), indent=2) if traces else "{}")
        return

    for trace in traces:
        print(json.dumps(trace.model_dump(mode="json")))


if __name__ == "__main__":
    main()
