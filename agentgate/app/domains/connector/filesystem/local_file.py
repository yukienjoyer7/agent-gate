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

        settings = get_settings()

        root = Path(settings.LOCAL_FILE_ROOT).resolve()
        requested_path = payload.get("path", "")
        resolved = (root / requested_path).resolve()

        try:
            validate_path(resolved, settings.ALLOWED_FILESYSTEM_PATHS, root)
        except PermissionError as exc:
            return failed(run_id, action_id, "local_file", str(exc))

        if not resolved.is_file():
            return failed(run_id, action_id, "local_file", "file not found")

        text = resolved.read_text(encoding="utf-8")
        return ExecutionResult(
            run_id=run_id,
            action_id=action_id,
            executor="local_file",
            status=ExecutionStatus.SUCCESS,
            result_summary=f"Read {resolved.name} ({len(text)} chars)",
            data={"path": str(resolved.relative_to(root)), "content_preview": text[:500]},
            latency_ms=int((perf_counter() - started) * 1000),
        )


def validate_path(resolved: Path, allowed_paths: list[str], default_root: Path) -> None:
    allowed_roots = [Path(p).resolve() for p in allowed_paths]
    allowed_roots.append(default_root.resolve())

    for allowed_root in allowed_roots:
        if resolved == allowed_root or allowed_root in resolved.parents:
            return

    allowed_display = ", ".join(str(p) for p in allowed_roots)
    raise PermissionError(f"Path '{resolved}' is not within any allowed directory: {allowed_display}")


def failed(run_id: str, action_id: str, executor: str, message: str) -> ExecutionResult:
    return ExecutionResult(
        run_id=run_id,
        action_id=action_id,
        executor=executor,
        status=ExecutionStatus.FAILED,
        result_summary=message,
        error=ConnectorError.validation(message).model_dump(mode="json"),
    )
