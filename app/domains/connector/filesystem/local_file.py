from pathlib import Path
from time import perf_counter

from app.config.settings import get_settings
from app.core.errors import ConnectorError
from app.core.schemas import ExecutionResult, ExecutionStatus
from app.domains.connector.base import BaseConnector


class LocalFileConnector(BaseConnector):
    async def execute(self, action: str, payload: dict) -> ExecutionResult:
        started = perf_counter()
        run_id = payload["run_id"]
        action_id = payload["action_id"]

        if action != "read":
            return failed(run_id, action_id, "local_file", "unsupported local file action")

        root = Path(get_settings().LOCAL_FILE_ROOT).resolve()
        path = (root / payload.get("path", "")).resolve()
        if root not in [path, *path.parents]:
            return failed(run_id, action_id, "local_file", "path is outside LOCAL_FILE_ROOT")
        if not path.is_file():
            return failed(run_id, action_id, "local_file", "file not found")

        text = path.read_text(encoding="utf-8")
        return ExecutionResult(
            run_id=run_id,
            action_id=action_id,
            executor="local_file",
            status=ExecutionStatus.SUCCESS,
            result_summary=f"Read {path.name} ({len(text)} chars)",
            data={"path": str(path.relative_to(root)), "content_preview": text[:500]},
            latency_ms=int((perf_counter() - started) * 1000),
        )


def failed(run_id: str, action_id: str, executor: str, message: str) -> ExecutionResult:
    return ExecutionResult(
        run_id=run_id,
        action_id=action_id,
        executor=executor,
        status=ExecutionStatus.FAILED,
        result_summary=message,
        error=ConnectorError.validation(message).model_dump(mode="json"),
    )
