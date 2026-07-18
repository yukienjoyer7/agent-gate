from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from app.config.settings import get_settings
from app.core.schemas import ActionRequest, AuditEvent, Decision, ExecutionResult, ExecutionStatus
from app.domains.audit.repositories.audit_repository_db import AuditRepositoryDB
from app.domains.browser.executor import execute_action
from app.domains.browser.selector_map.domInspector import build_execution_metadata
from app.domains.browser.selector_map.locatorGenerator import build_locator_candidates
from app.domains.browser.selector_map.locatorRanker import build_selector_map
from app.domains.browser.selector_map.matcher import build_matched_elements
from app.domains.browser.snapshot.snapshotBuilder import (
    build_semantic_elements,
    enrich_semantic_elements,
)
from app.domains.guardrail.decision import decide

SUPPORTED_BROWSER_ACTIONS = {"click", "fill", "scroll", "screenshot"}


@dataclass
class BrowserPageModel:
    snapshot: list[dict[str, Any]]
    locator_candidates: list[dict[str, Any]]
    selector_map: dict[str, Any]


async def run_browser_prototype_agent(
    *,
    url: str,
    action: dict[str, Any] | None = None,
    actions: list[dict[str, Any]] | None = None,
    user_goal: str = "run browser prototype action",
    risk_hint: str = "unknown",
    timeout_ms: int = 15_000,
    wait_until: str = "domcontentloaded",
    settle_ms: int = 0,
) -> AuditEvent:
    """
    Browser prototype agent:
    1. Create an auditable ActionRequest.
    2. Let the existing guardrail decision run.
    3. Use the browser-domain snapshot and selector-map pipeline.
    4. Execute optional browser actions through browserExecutor.py.
    5. Persist the complete lifecycle to Postgres audit_logs.
    """
    total_started = perf_counter()
    browser_actions = _normalize_actions(action=action, actions=actions)
    legacy_action = browser_actions[0] if len(browser_actions) == 1 else {}
    request = ActionRequest(
        source="api",
        domain="browser",
        action_type="BROWSER_PROTOTYPE_ACTION",
        target_system="browser",
        target=url,
        payload_summary=_payload_summary(url, browser_actions),
        payload={
            "url": url,
            "action": legacy_action,
            "actions": browser_actions,
            "wait_until": wait_until,
            "timeout_ms": timeout_ms,
            "user_goal": user_goal,
        },
        risk_hint=risk_hint,
    )

    decision = decide(request)

    if decision.decision != Decision.ALLOW:
        execution = _skipped_execution(request, decision)
        return await _write_audit(request, decision, execution, total_started)

    try:
        execution = await _execute_with_browser(
            request=request,
            url=url,
            actions=browser_actions,
            timeout_ms=timeout_ms,
            wait_until=wait_until,
        )
    except Exception as exc:
        execution = ExecutionResult(
            run_id=request.run_id,
            action_id=request.action_id,
            executor="browser_prototype_agent",
            status=ExecutionStatus.FAILED,
            result_summary="browser prototype action failed",
            error=_error_payload(exc),
        )

    return await _write_audit(request, decision, execution, total_started)


async def _execute_with_browser(
    *,
    request: ActionRequest,
    url: str,
    actions: list[dict[str, Any]],
    timeout_ms: int,
    wait_until: str,
) -> ExecutionResult:
    started = perf_counter()
    settings = get_settings()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=settings.PLAYWRIGHT_HEADLESS,
            args=[
                "--disable-http2",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        try:
            page = await browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;q=0.9,"
                        "image/avif,image/webp,*/*;q=0.8"
                    ),
                    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                    "Upgrade-Insecure-Requests": "1",
                },
            )
            await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            if settle_ms:
                await asyncio.sleep(settle_ms / 1000)

            initial_page_model = await _build_page_model(page)
            page_model = initial_page_model
            action_results: list[dict[str, Any]] = []
            executable_actions: list[dict[str, Any]] = []
            use_indexed_screenshot_paths = len(actions) > 1

            for action_index, browser_action in enumerate(actions, start=1):
                executable_action = _prepare_action(
                    action=browser_action,
                    page_model=page_model,
                    action_id=request.action_id,
                    action_index=action_index if use_indexed_screenshot_paths else None,
                )
                if executable_action is None:
                    continue

                await execute_action(page, page_model.selector_map, executable_action)
                delay_ms = browser_action.get("delay_ms")
                if delay_ms:
                    await asyncio.sleep(delay_ms / 1000)
                executable_actions.append(executable_action)
                action_results.append(
                    {
                        "index": action_index,
                        "type": executable_action["type"],
                        "status": "SUCCESS",
                        "action": executable_action,
                        "final_url": page.url,
                    }
                )

                if action_index < len(actions):
                    await _settle_page(page, timeout_ms=timeout_ms)
                    page_model = await _build_page_model(page)

            final_page_model = page_model
            if actions:
                await _settle_page(page, timeout_ms=timeout_ms)
                final_page_model = await _build_page_model(page)

            executed = bool(action_results)
            legacy_executable_action = (
                executable_actions[0] if len(executable_actions) == 1 else None
            )

            return ExecutionResult(
                run_id=request.run_id,
                action_id=request.action_id,
                executor="browser_prototype_agent",
                status=ExecutionStatus.SUCCESS,
                result_summary=_result_summary(actions, len(action_results)),
                data={
                    "url": url,
                    "final_url": page.url,
                    "snapshot": initial_page_model.snapshot,
                    "selector_map": initial_page_model.selector_map,
                    "locator_candidates": initial_page_model.locator_candidates,
                    "final_snapshot": final_page_model.snapshot,
                    "final_selector_map": final_page_model.selector_map,
                    "action": legacy_executable_action,
                    "actions": executable_actions,
                    "action_results": action_results,
                    "executed": executed,
                },
                latency_ms=int((perf_counter() - started) * 1000),
            )
        finally:
            await browser.close()


async def _build_page_model(page) -> BrowserPageModel:
    semantic_elements = await build_semantic_elements(page)
    semantic_snapshot = enrich_semantic_elements(semantic_elements)
    execution_metadata = await build_execution_metadata(page, semantic_snapshot)
    matched_elements = build_matched_elements(semantic_snapshot, execution_metadata)
    locator_candidates = build_locator_candidates(matched_elements)
    selector_map = await build_selector_map(page, locator_candidates)

    return BrowserPageModel(
        snapshot=_public_snapshot(matched_elements),
        locator_candidates=locator_candidates,
        selector_map=selector_map,
    )


def _prepare_action(
    *,
    action: dict[str, Any],
    page_model: BrowserPageModel,
    action_id: str,
    action_index: int | None = None,
) -> dict[str, Any] | None:
    if not action:
        return None

    action_type = action.get("type")
    if action_type not in SUPPORTED_BROWSER_ACTIONS:
        raise ValueError(f"unsupported browser action: {action_type}")

    prepared = dict(action)
    if action_type == "screenshot":
        suffix = f"_{action_index:02d}" if action_index is not None else ""
        prepared.setdefault("path", f"data/browser/screenshots/{action_id}{suffix}.png")
        return prepared

    if action_type == "scroll" and not (prepared.get("element_id") or prepared.get("label")):
        return prepared

    prepared["element_id"] = str(
        prepared.get("element_id") or _find_element_id(prepared, page_model)
    )

    if prepared["element_id"] not in page_model.selector_map:
        raise ValueError(f"element_id not found in selector_map: {prepared['element_id']}")

    if action_type == "fill" and "value" not in prepared:
        raise ValueError("fill action requires a value")

    return prepared


def _find_element_id(action: dict[str, Any], page_model: BrowserPageModel) -> str:
    label = action.get("label") or action.get("element_label") or action.get("text")
    role = action.get("role")

    if not label:
        raise ValueError("browser action requires element_id or label")

    exact_matches = [
        element
        for element in page_model.snapshot
        if element["element_id"] in page_model.selector_map
        and element["label"].casefold() == str(label).casefold()
        and (role is None or element["role"] == role)
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]["element_id"]

    partial_matches = [
        element
        for element in page_model.snapshot
        if element["element_id"] in page_model.selector_map
        and str(label).casefold() in element["label"].casefold()
        and (role is None or element["role"] == role)
    ]
    if len(partial_matches) == 1:
        return partial_matches[0]["element_id"]

    raise ValueError(f"unable to resolve a unique browser element for label: {label}")


async def _settle_page(page, *, timeout_ms: int) -> None:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=min(timeout_ms, 5_000))
    except PlaywrightTimeoutError:
        pass


def _normalize_actions(
    *,
    action: dict[str, Any] | None,
    actions: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if action and actions:
        raise ValueError("pass either action or actions, not both")
    if actions is not None:
        return [dict(browser_action) for browser_action in actions if browser_action]
    if action:
        return [dict(action)]
    return []


def _public_snapshot(matched_elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
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


def _skipped_execution(request: ActionRequest, decision) -> ExecutionResult:
    if decision.decision == Decision.BLOCK:
        status = ExecutionStatus.BLOCKED
        summary = "blocked by guardrail"
    elif decision.decision == Decision.NEED_APPROVAL:
        status = ExecutionStatus.PENDING_APPROVAL
        summary = "pending approval"
    else:
        status = ExecutionStatus.SKIPPED
        summary = f"skipped by guardrail decision: {decision.decision}"

    return ExecutionResult(
        run_id=request.run_id,
        action_id=request.action_id,
        executor="browser_prototype_agent",
        status=status,
        result_summary=summary,
        data={"decision": decision.decision},
    )


async def _write_audit(
    request: ActionRequest,
    decision,
    execution: ExecutionResult,
    total_started: float,
) -> AuditEvent:
    latency = {
        "guardrail_ms": decision.latency_ms,
        "executor_ms": execution.latency_ms,
        "total_ms": int((perf_counter() - total_started) * 1000),
    }
    return await AuditRepositoryDB().write(request, decision, execution, latency)


def _payload_summary(url: str, actions: list[dict[str, Any]]) -> str:
    if not actions:
        return f"Build browser snapshot and selector map for {url}"
    if len(actions) == 1:
        return f"{actions[0].get('type', 'browser action')} on {url}"
    action_types = ", ".join(action.get("type", "browser action") for action in actions)
    return f"{len(actions)} browser actions on {url}: {action_types}"


def _result_summary(actions: list[dict[str, Any]], executed_count: int) -> str:
    if not actions:
        return "built browser snapshot and selector map"
    if executed_count == 1:
        return f"executed browser action: {actions[0]['type']}"
    return f"executed {executed_count} browser actions"


def _error_payload(exc: Exception) -> dict[str, str]:
    if isinstance(exc, PlaywrightTimeoutError):
        code = "BROWSER_TIMEOUT"
    elif isinstance(exc, PlaywrightError):
        code = "BROWSER_EXECUTION_ERROR"
    elif isinstance(exc, ValueError):
        code = "INVALID_BROWSER_ACTION"
    else:
        code = exc.__class__.__name__

    return {"code": code, "message": str(exc)[:500]}
