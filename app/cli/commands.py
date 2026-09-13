"""Local command handlers; business execution stays in the application service."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

from app.cli.console import Console, InteractionRequired
from app.credentials.local import (
    SECRET_ENV,
    CredentialError,
    LocalTokenStore,
    SessionSecrets,
    secret_store,
)
from app.credentials.oauth import connect_google, disconnect_google
from app.domains.guardrail.sensitive import is_sensitive_key
from app.runtime.config import LocalConfig, LocalPaths, load_config, save_config
from app.runtime.local import LocalRuntime
from app.storage.local.database import LocalDatabase
from app.storage.local.repositories import RunStore

PROVIDERS = ("llm", "github", "gmail", "calendar", "stripe")


def configured(args: argparse.Namespace, paths: LocalPaths) -> LocalConfig:
    config = load_config(paths.config)
    updates: dict[str, Any] = {}
    if getattr(args, "workspace", None):
        updates["workspace"] = str(Path(args.workspace).resolve())
    if args.credential_store:
        updates["credential_store"] = args.credential_store
    return LocalConfig.model_validate({**config.model_dump(), **updates})


async def initialize(args: argparse.Namespace, paths: LocalPaths, console: Console) -> int:
    if paths.config.exists():
        console.say("Already initialized. Existing configuration has been preserved")
        return 0
    values: dict[str, Any] = {"credential_store": args.credential_store or "keyring"}
    if args.non_interactive:
        if not args.workspace:
            raise ValueError("Non-interactive init requires '--workspace'")
        values["workspace"] = str(Path(args.workspace).resolve())
    else:
        values["llm_type"] = await console.read(
            "LLM API dialect (openai/anthropic/gemini)", default="openai"
        )
        defaults = LocalConfig()
        values["llm_url"] = await console.read("LLM endpoint URL", default=defaults.llm_url)
        values["llm_model"] = await console.read("LLM model", default=defaults.llm_model)
        values["timezone"] = await console.read("Timezone", default=defaults.timezone)
        values["workspace"] = str(
            Path(
                await console.read("Allowed workspace", default=args.workspace or str(Path.cwd()))
            ).resolve()
        )
        values["credential_store"] = await console.read(
            "Credential store (keyring/env/session)", default=values["credential_store"]
        )
    config = LocalConfig.model_validate(values)
    if not Path(config.workspace).is_dir():
        raise ValueError("Allowed workspace must be an existing directory")
    if not args.non_interactive and config.credential_store == "keyring":
        store = secret_store("keyring", paths.data)
        # Fail before writing config if the selected secure backend is unavailable.
        store.get("llm")
        key = await console.read("LLM API key (blank to connect later)", hidden=True)
        if key:
            store.set("llm", key)
            console.sanitizer.remember(key)
    save_config(paths.config, config)
    with LocalDatabase(paths.database):
        pass
    console.say(f"Initialized. Configuration: {paths.config}")
    console.say(
        "External LLMs receive your prompts and relevant observations. No application server is started"
    )
    return 0


async def diagnose(config: LocalConfig, paths: LocalPaths, console: Console) -> int:
    checks: dict[str, Any] = {
        "config": "ok",
        "workspace": Path(config.workspace).is_dir(),
        "credential_store": config.credential_store,
    }
    store = secret_store(config.credential_store, paths.data)
    try:
        checks["credentials"] = {
            provider: bool(store.get(provider) or store.get(f"oauth.{provider}"))
            for provider in PROVIDERS
        }
        checks["keychain"] = "available" if config.credential_store == "keyring" else "not selected"
    except CredentialError as exc:
        checks["credentials"] = {}
        checks["keychain"] = str(exc)
    try:
        with LocalDatabase(paths.database):
            checks["storage"] = "ok"
    except (OSError, ValueError) as exc:
        checks["storage"] = console.sanitizer.text(str(exc))
    browser = importlib.util.find_spec("playwright") is not None
    checks["browser"] = (
        "disabled"
        if not config.browser_enabled
        else "package installed" if browser else "missing; install agentgate[browser]"
    )
    checks["stripe"] = (
        "package installed"
        if importlib.util.find_spec("stripe")
        else "optional; install agentgate[stripe]"
    )
    checks["stripe_checkout_configured"] = bool(
        config.stripe_success_url and config.stripe_cancel_url and config.stripe_price_map
    )
    browser_ok = not config.browser_enabled
    if config.browser_enabled and browser:
        from playwright.async_api import async_playwright

        async def probe_browser() -> None:
            async with async_playwright() as playwright:
                instance = await playwright.chromium.launch(headless=True)
                await instance.close()

        try:
            await asyncio.wait_for(probe_browser(), timeout=15)
            checks["browser"] = "ready"
            browser_ok = True
        except Exception:
            checks["browser"] = "not ready; run 'agentgate setup browser' and check OS dependencies"
    console.result(checks)
    return (
        0
        if browser_ok
        and checks["workspace"]
        and checks["storage"] == "ok"
        and checks["credentials"].get("llm")
        else 1
    )


async def connect(provider: str, config: LocalConfig, paths: LocalPaths, console: Console) -> int:
    if config.credential_store == "session":
        raise CredentialError(
            "Session credentials cannot survive connect. Use 'run --credential-store session' instead"
        )
    store = secret_store(config.credential_store, paths.data)
    if not store.writable:
        value = store.get(provider)
        if not value:
            raise CredentialError(
                f"Set {SECRET_ENV[provider]} in your environment; no credential file will be written"
            )
        if provider == "stripe" and not value.startswith(("sk_test_", "rk_test_")):
            raise CredentialError("Local Stripe requires a test-mode key")
        console.say(f"{provider}: environment credential is configured")
        return 0
    if provider in ("gmail", "calendar"):
        store.get("google_client_secret")  # Check secure backend before opening the browser.
        client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID") or config.google_client_id
        if not client_id:
            client_id = await console.read("Google Desktop app client ID")
            if not client_id:
                raise CredentialError("A Google Desktop app client ID is required")
            config = config.model_copy(update={"google_client_id": client_id})
            save_config(paths.config, config)
        client_secret = store.get("google_client_secret") or ""
        if not client_secret:
            client_secret = await console.read(
                "Google Desktop client secret (blank if not required)", hidden=True
            )
            if client_secret:
                store.set("google_client_secret", client_secret)
                console.sanitizer.remember(client_secret)
        with LocalDatabase(paths.database) as database:
            await connect_google(
                provider,
                client_id,
                client_secret,
                LocalTokenStore(database, store, console.sanitizer),
                report=console.say,
            )
    else:
        value = await console.read(
            f"{provider} {'test-mode key' if provider == 'stripe' else 'API key/token'}",
            hidden=True,
        )
        if not value:
            raise CredentialError("No credential supplied; nothing changed")
        if provider == "stripe" and not value.startswith(("sk_test_", "rk_test_")):
            raise CredentialError("Local Stripe requires a test-mode key")
        store.set(provider, value)
        console.sanitizer.remember(value)
    console.say(f"{provider}: connected")
    return 0


async def disconnect(
    provider: str, config: LocalConfig, paths: LocalPaths, console: Console
) -> int:
    if config.credential_store == "session":
        console.say("Session credentials disappear when the process exits")
        return 0
    store = secret_store(config.credential_store, paths.data)
    if not store.writable:
        raise CredentialError(
            f"Unset {SECRET_ENV[provider]} yourself. Environment credentials are not modified"
        )
    with LocalDatabase(paths.database) as database:
        if provider in ("gmail", "calendar"):
            await disconnect_google(provider, LocalTokenStore(database, store, console.sanitizer))
            console.say(
                "Google revocation can also invalidate the other Google integration; reconnect it if needed"
            )
        store.delete(provider)
    console.say(
        f"{provider}: disconnected locally"
        + (
            "; revoke a supplied token at its provider if needed"
            if provider in ("github", "stripe")
            else ""
        )
    )
    return 0


async def setup_browser(config: LocalConfig, paths: LocalPaths, console: Console) -> int:
    if importlib.util.find_spec("playwright") is None:
        raise ValueError(
            "Install the browser extra from your source checkout or release wheel first; see docs/cli.md"
        )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "playwright",
        "install",
        "chromium",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()
        raise
    if output:
        console.say(output.decode(errors="replace"))
    if process.returncode:
        raise ValueError("Browser setup failed. Install required OS libraries and run setup again")
    save_config(paths.config, config.model_copy(update={"browser_enabled": True}))
    console.say("Chromium installed and browser capability enabled")
    return 0


async def execute(
    args: argparse.Namespace, config: LocalConfig, paths: LocalPaths, console: Console
) -> int:
    store = secret_store(config.credential_store, paths.data)
    if isinstance(store, SessionSecrets):
        if args.non_interactive or not console.interactive:
            raise InteractionRequired("Session credentials need a terminal")
        for name in SECRET_ENV:
            value = await console.read(f"Session {name} credential (blank to skip)", hidden=True)
            if value:
                store.set(name, value)
                console.sanitizer.remember(value)
    with LocalRuntime(config, paths, secrets=store, sanitizer=console.sanitizer) as runtime:
        from app.runtime.service import AgentService

        if not runtime.settings.LLM_API_KEY:
            raise CredentialError(
                "LLM credential is missing. Run 'agentgate connect llm' or configure LLM_API_KEY with the env store"
            )
        service = AgentService(runtime)
        final_status: str | None = None
        try:
            run = service.submit(args.prompt)
            run.publish({"type": "run_started", "data": {"run_id": run.run_id}})
            async for event in service.events():
                console.event(event)
                if event["type"] not in {"awaiting_approval", "awaiting_input"}:
                    continue
                if args.non_interactive or not console.interactive:
                    final_status = "interaction_required"
                    raise InteractionRequired(
                        "Run requires approval or input; rerun interactively. No pending action was executed"
                    )
                data = event["data"]
                step = data["step"]
                console.action(data)
                if event["type"] == "awaiting_approval":
                    answer = await _read_for_run(console, run, "Approve? (y/N)", default="n")
                    if answer is None:
                        continue
                    service.respond(
                        data["index"],
                        step["action_id"],
                        "approve" if answer.lower() in {"y", "yes"} else "decline",
                    )
                else:
                    fields = {}
                    for field in data.get("fields") or []:
                        key = field["key"]
                        hidden = bool(data.get("sanitize")) or is_sensitive_key(key)
                        field_value = await _read_for_run(
                            console, run, field.get("label", key), hidden=hidden
                        )
                        if field_value is None:
                            break
                        if hidden:
                            console.sanitizer.remember(field_value)
                        fields[key] = field_value
                    else:
                        service.respond(data["index"], step["action_id"], "input", fields)
            return (
                0
                if run.status.value == "done"
                and (not run.execution_log or run.execution_log[-1]["status"] == "done")
                else 1
            )
        except InteractionRequired as exc:
            final_status = "interaction_required"
            if service.run is not None:
                console.event(
                    service.run.publish(
                        {
                            "type": "interaction_required",
                            "data": {
                                "run_id": service.run.run_id,
                                "status": final_status,
                                "message": str(exc),
                            },
                        }
                    )
                )
                exc.reported = True
            raise
        finally:
            await service.cancel()
            if service.run is not None:
                runtime.runs.finish(service.run, final_status)


async def _read_for_run(console: Console, run: Any, label: str, **options: Any) -> str | None:
    """Restore terminal state if the engine times out while the user is typing."""
    incoming = asyncio.create_task(console.read(label, **options))
    try:
        done, _ = await asyncio.wait({incoming, run.task}, return_when=asyncio.FIRST_COMPLETED)
        return None if run.task in done else incoming.result()
    finally:
        if not incoming.done():
            incoming.cancel()
        await asyncio.gather(incoming, return_exceptions=True)


def inspect_run(args: argparse.Namespace, paths: LocalPaths, console: Console) -> int:
    with LocalDatabase(paths.database) as database:
        runs = RunStore(database, console.sanitizer)
        if args.command == "history":
            console.result(runs.history(args.limit))
        else:
            record = runs.get(args.run_id)
            if record is None:
                raise ValueError("Run not found in this profile")
            console.result(record)
    return 0


async def sync_payments(config: LocalConfig, paths: LocalPaths, console: Console) -> int:
    from app.runtime.payments import sync_payments as reconcile

    with LocalRuntime(config, paths, sanitizer=console.sanitizer) as runtime:
        result = await reconcile(runtime)
        console.result(result)
        if result["unresolved"]:
            console.say(
                "Unknown operations without remote IDs require manual provider reconciliation; they are not retried"
            )
        return 1 if result["failures"] or result["unresolved"] else 0
