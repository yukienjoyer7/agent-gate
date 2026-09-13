"""Local composition and durable execution intent boundary."""

from __future__ import annotations

import importlib.util
import json
import os
from contextlib import ExitStack
from importlib import import_module
from typing import Any

from filelock import FileLock, Timeout

from app.config.settings import Settings
from app.core.schemas import (
    ActionRequest,
    Decision,
    DecisionResponse,
    ExecutionResult,
    ExecutionStatus,
)
from app.credentials.local import SECRET_ENV, LocalTokenStore, SecretStore, secret_store
from app.domains.connector.base import BaseConnector
from app.executors.api_executor import APIExecutor
from app.executors.router import ExecutionRouter, skipped
from app.runtime.config import LocalConfig, LocalPaths
from app.runtime.context import RuntimeDependencies, bind_runtime
from app.runtime.safety import Sanitizer
from app.storage.local.database import LocalDatabase, private_directory
from app.storage.local.intents import IntentJournal, UnknownOutcome
from app.storage.local.repositories import (
    LocalAuditStore,
    LocalPaymentStore,
    LocalTraceWriter,
    RunStore,
)


class LazyConnector(BaseConnector):
    def __init__(self, module: str, name: str, **kwargs: Any) -> None:
        self.module, self.name, self.kwargs = module, name, kwargs
        self.connector: BaseConnector | None = None

    async def execute(self, action: str, payload: dict) -> ExecutionResult:
        if self.connector is None:
            try:
                self.connector = getattr(import_module(self.module), self.name)(**self.kwargs)
            except ModuleNotFoundError as exc:
                if exc.name not in {"stripe", "playwright", "playwright.async_api"}:
                    raise
                return ExecutionResult(
                    run_id=payload["run_id"],
                    action_id=payload["action_id"],
                    executor="local",
                    status=ExecutionStatus.FAILED,
                    result_summary="Optional integration is not installed; see 'agentgate doctor'",
                )
        return await self.connector.execute(action, payload)


class LocalRouter:
    def __init__(
        self,
        router: ExecutionRouter,
        database: LocalDatabase,
        payments: LocalPaymentStore,
        settings: Settings,
        sanitizer: Sanitizer,
    ) -> None:
        self.router, self.database, self.payments = router, database, payments
        self.settings, self.sanitizer = settings, sanitizer
        self.intents = IntentJournal(database, sanitizer)

    async def route(
        self, action: ActionRequest, decision: DecisionResponse, **kwargs: Any
    ) -> ExecutionResult:
        if decision.decision != Decision.ALLOW:
            return await self.router.route(action, decision, **kwargs)
        operation = str(action.payload.get("action") or action.action_type)
        browser = action.target_system == "browser" or action.action_type.startswith("BROWSER_")
        if browser:
            # Reactive browser batches have their own per-step intent boundary.
            return await self.router.route(action, decision, **kwargs)
        financial = action.target_system == "stripe" and operation in {
            "create_checkout_session",
            "create_refund",
            "expire_checkout_session",
        }
        key = None
        if financial:
            if importlib.util.find_spec("stripe") is None:
                return skipped(
                    action,
                    ExecutionStatus.FAILED,
                    "Install agentgate[stripe] to enable this integration",
                )
            from app.domains.connector.stripe.stripe import (
                checkout_idempotency_key,
                idempotency_key,
            )

            if not self.settings.STRIPE_SECRET_KEY.startswith(("sk_test_", "rk_test_")):
                return skipped(
                    action, ExecutionStatus.BLOCKED, "Local Stripe actions require a test-mode key"
                )
            if operation == "create_checkout_session":
                catalog = str(action.payload.get("catalog_key") or "")
                key = checkout_idempotency_key(
                    action.run_id,
                    catalog_key=catalog,
                    price_id=self.settings.STRIPE_PRICE_MAP.get(catalog, ""),
                    quantity=action.payload.get("quantity", 1),
                    success_url=self.settings.STRIPE_SUCCESS_URL,
                    cancel_url=self.settings.STRIPE_CANCEL_URL,
                    customer_email=(
                        str(action.payload["customer_email"]).strip()
                        if action.payload.get("customer_email")
                        else None
                    ),
                )
            else:
                key = idempotency_key(
                    action.run_id,
                    action.action_id,
                    "refund" if operation == "create_refund" else "expire",
                )
        try:
            key, previous = self.intents.begin(action, key)
        except UnknownOutcome as exc:
            return skipped(action, ExecutionStatus.BLOCKED, str(exc))
        if previous is not None:
            return previous
        try:
            result = await self.router.route(action, decision, **kwargs)
            # Commit the remote identifier before any secondary reconciliation
            # write so it remains recoverable if payment persistence fails.
            self.intents.finish(key, action, result)
            if action.target_system == "stripe" and result.data.get("id"):
                self.payments.record("refund" if "refund" in operation else "checkout", result.data)
            return result
        except BaseException:
            self.intents.unknown(key)
            raise


class LocalRuntime:
    def __init__(
        self,
        config: LocalConfig,
        paths: LocalPaths,
        *,
        secrets: SecretStore | None = None,
        sanitizer: Sanitizer | None = None,
    ) -> None:
        self.config, self.paths = config, paths
        self.sanitizer = sanitizer or Sanitizer()
        self.secrets = secrets or secret_store(config.credential_store, paths.data)
        self.database = LocalDatabase(paths.database)
        self._stack = ExitStack()

    def __enter__(self) -> LocalRuntime:
        private_directory(self.paths.data)
        try:
            self._stack.enter_context(
                FileLock(str(self.paths.data / "run.lock"), timeout=0, mode=0o600)
            )
        except Timeout as exc:
            raise ValueError("Another AgentGate run is active for this profile") from exc
        try:
            self._stack.enter_context(self.database)
            self.runs = RunStore(self.database, self.sanitizer)
            self.runs.interrupt_abandoned()
            self.payments = LocalPaymentStore(self.database)
            credentials = {name: self.secrets.get(name) or "" for name in SECRET_ENV}
            for value in credentials.values():
                self.sanitizer.remember(value)
            workspace = self.config.workspace
            if not workspace or not os.path.isdir(workspace):
                raise ValueError(
                    "Configure an existing allowed workspace with 'init' or '--workspace'"
                )
            cfg_values = self.config.model_dump()
            for field, variable in {
                "llm_type": "LLM_TYPE",
                "llm_url": "LLM_URL",
                "llm_model": "LLM_MODEL",
                "google_client_id": "GOOGLE_OAUTH_CLIENT_ID",
                "stripe_success_url": "STRIPE_SUCCESS_URL",
                "stripe_cancel_url": "STRIPE_CANCEL_URL",
            }.items():
                if variable in os.environ:
                    cfg_values[field] = os.environ[variable]
            if "STRIPE_PRICE_MAP" in os.environ:
                cfg_values["stripe_price_map"] = json.loads(os.environ["STRIPE_PRICE_MAP"])
            cfg = LocalConfig.model_validate(cfg_values)
            settings_values: dict[str, Any] = dict(
                _env_file=None,
                LLM_TYPE=cfg.llm_type,
                LLM_URL=cfg.llm_url,
                LLM_MODEL=cfg.llm_model,
                LLM_API_KEY=credentials["llm"],
                GITHUB_TOKEN=credentials["github"],
                GMAIL_ACCESS_TOKEN=credentials["gmail"],
                GOOGLE_CALENDAR_ACCESS_TOKEN=credentials["calendar"],
                STRIPE_SECRET_KEY=credentials["stripe"],
                GOOGLE_OAUTH_CLIENT_ID=cfg.google_client_id,
                GOOGLE_OAUTH_CLIENT_SECRET=credentials["google_client_secret"],
                CALENDAR_DEFAULT_TIMEZONE=cfg.timezone,
                LOCAL_FILE_ROOT=workspace,
                DATA_DIR=str(self.paths.data / "artifacts"),
                ALLOWED_FILESYSTEM_PATHS=[workspace],
                STRIPE_SUCCESS_URL=cfg.stripe_success_url,
                STRIPE_CANCEL_URL=cfg.stripe_cancel_url,
                STRIPE_PRICE_MAP=cfg.stripe_price_map,
                LLM_TOOLS_ENABLED=cfg.browser_enabled,
                ATOMIC_BROWSER_AUDIT=True,
                ALLOWED_TARGET_SYSTEMS=["local_file", "github", "gmail", "calendar", "stripe"]
                + (["browser"] if cfg.browser_enabled else []),
            )
            # Pass every field explicitly so BaseSettings cannot load unrelated
            # server variables or weaken policy via environment overrides.
            defaults = Settings.model_construct()
            tunable = {
                name: os.environ[name]
                for name in (
                    "LLM_TIMEOUT",
                    "LLM_MAX_TOKENS",
                    "GUARDRAIL_LLM_ENABLED",
                    "GUARDRAIL_MODEL",
                    "AGENT_MAX_STEPS",
                    "AGENT_MAX_REPLAN",
                    "AGENT_WAIT_RESPONSE_TIMEOUT_SEC",
                    "AGENT_RUN_TIMEOUT_SEC",
                    "PLAYWRIGHT_HEADLESS",
                    "BROWSER_TIMEOUT_MS",
                    "BROWSER_SETTLE_MS",
                )
                if name in os.environ
            }
            self.settings = Settings(**{**defaults.model_dump(), **tunable, **settings_values})
            self.tokens = LocalTokenStore(self.database, self.secrets, self.sanitizer)
            connectors: dict[str, BaseConnector] = {
                name: LazyConnector(f"app.domains.connector.{module}", cls)
                for name, module, cls in [
                    ("local_file", "filesystem", "LocalFileConnector"),
                    ("github", "github", "GitHubConnector"),
                    ("gmail", "gmail", "GmailConnector"),
                    ("calendar", "calendar", "CalendarConnector"),
                ]
            }
            connectors["stripe"] = LazyConnector(
                "app.domains.connector.stripe", "StripeConnector", payment_store=self.payments
            )
            self.connectors = connectors
            self.router = LocalRouter(
                ExecutionRouter(APIExecutor(connectors)),
                self.database,
                self.payments,
                self.settings,
                self.sanitizer,
            )
            self._stack.enter_context(
                bind_runtime(
                    RuntimeDependencies(
                        self.settings,
                        LocalAuditStore(self.database, self.sanitizer),
                        self.router,
                        self.tokens,
                        LocalTraceWriter(self.database, self.sanitizer),
                        self.router.intents,
                        self.sanitizer.clean,
                    )
                )
            )
        except BaseException:
            self._stack.close()
            raise
        return self

    def __exit__(self, *args: Any) -> None:
        self._stack.close()
