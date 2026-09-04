import json
from pathlib import Path

from app.config.settings import get_settings
from app.core.schemas import ActionTrace


class TraceWriter:
    def __init__(self, path: str | None = None) -> None:
        self.path = Path(path or get_settings().TRACE_LOG_PATH)

    def write(self, trace: ActionTrace) -> ActionTrace:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(trace.model_dump(mode="json")) + "\n")
        return trace

    def list(self) -> list[ActionTrace]:
        if not self.path.exists():
            return []
        return [
            ActionTrace.model_validate_json(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line
        ]
