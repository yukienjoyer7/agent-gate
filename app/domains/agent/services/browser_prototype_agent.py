from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from app.config.settings import get_settings
from app.core.schemas import (
    ActionRequest,
    AuditEvent,
    Decision,
    DecisionResponse,
    ExecutionResult,
    ExecutionStatus,
)
from app.domains.audit.repositories.audit_repository_db import AuditRepositoryDB
from app.domains.browser.browser_profile import DEFAULT_EXTRA_HEADERS, user_agent
from app.domains.browser.executor import execute_action
from app.domains.browser.selector_map.domInspector import build_execution_metadata
from app.domains.browser.selector_map.locatorGenerator import build_locator_candidates
from app.domains.browser.selector_map.locatorRanker import build_selector_map
from app.domains.browser.selector_map.matcher import build_matched_elements
from app.domains.browser.snapshot.snapshotBuilder import (
    build_semantic_elements,
    enrich_semantic_elements,
)
from app.domains.guardrail.decision import adecide

SUPPORTED_BROWSER_ACTIONS = {"click", "fill", "submit", "scroll", "screenshot"}

# ActionRequest.action_type -> browser action "type" this module executes.
# Submit maps to Enter-press so search/forms actually execute instead of
# merely focusing the element (clicking an input does nothing).
BROWSER_ACTION_TYPE_MAP = {
    "BROWSER_CLICK": "click",
    "BROWSER_TYPE": "fill",
    "BROWSER_SCROLL": "scroll",
    "BROWSER_SCREENSHOT": "screenshot",
    "BROWSER_SUBMIT": "submit",
    "BROWSER_SELECT": "click",
}


def plan_step_to_browser_action(step: dict[str, Any]) -> dict[str, Any] | None:
    """Convert one plan-step dict (``action_type`` + ``payload``) into the
    ``{"type": ..., ...}`` action this module's executor consumes.

    Returns ``None`` for steps with no browser-action equivalent (e.g.
    ``BROWSER_OPEN``/``BROWSER_SNAPSHOT``, which just navigate + snapshot).
    """
    browser_type = BROWSER_ACTION_TYPE_MAP.get(step.get("action_type"))
    if not browser_type:
        return None

    payload = step.get("payload", {})
    payload = payload if isinstance(payload, dict) else {}
    browser_action: dict[str, Any] = {"type": browser_type}
    for key in ("label", "element_id", "role"):
        if payload.get(key):
            browser_action[key] = payload[key]

    value = payload.get("value") or payload.get("query") or payload.get("text")
    if value:
        browser_action["value"] = value

    for key in ("delay_ms", "duration_ms", "x", "y", "path", "full_page"):
        if key in payload:
            browser_action[key] = payload[key]
        elif key in step:
            browser_action[key] = step[key]

    return browser_action


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
    timeout_ms: int | None = None,
    wait_until: str | None = None,
    settle_ms: int = 0,
    run_id: str | None = None,
    action_id: str | None = None,
    skip_guardrail: bool = False,
) -> AuditEvent:
    """
    Browser prototype agent:
    1. Create an auditable ActionRequest (run/action ids inherited from the
       caller when provided, so audit events group under the same run).
    2. Let the existing guardrail decision run (or skip it when the caller,
       e.g. the reactive agent loop, already evaluated the steps).
    3. Use the browser-domain snapshot and selector-map pipeline.
    4. Execute optional browser actions through browserExecutor.py.
    5. Persist the complete lifecycle to Postgres audit_logs.
    """
    total_started = perf_counter()
    settings = get_settings()
    if timeout_ms is None:
        timeout_ms = settings.BROWSER_TIMEOUT_MS
    if wait_until is None:
        wait_until = settings.BROWSER_WAIT_UNTIL
    browser_actions = _normalize_actions(action=action, actions=actions)
    legacy_action = browser_actions[0] if len(browser_actions) == 1 else {}
    request_kwargs: dict[str, Any] = {}
    if run_id:
        request_kwargs["run_id"] = run_id
    if action_id:
        request_kwargs["action_id"] = action_id
    request = ActionRequest(
        **request_kwargs,
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

    if skip_guardrail:
        decision = DecisionResponse(
            run_id=request.run_id,
            action_id=request.action_id,
            decision=Decision.ALLOW,
            reasons=["guardrail skipped (steps pre-checked by the agent loop)"],
        )
    else:
        decision = await adecide(request)

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
            settle_ms=settle_ms,
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
    settle_ms: int = 0,
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
                user_agent=user_agent(),
                extra_http_headers=DEFAULT_EXTRA_HEADERS,
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

    # Prefer the LLM-provided element_id only when it is a real selector_map
    # key AND still matches the requested label (page state can shift between
    # the tool call and execution, so an index that coincidentally exists may
    # now point at a different element). Otherwise fall back to resolving the
    # element against the live snapshot (label/role/DOM attrs).
    provided_id = prepared.get("element_id")
    if (
        provided_id
        and str(provided_id) in page_model.selector_map
        and _key_matches_action(str(provided_id), prepared, page_model)
    ):
        prepared["element_id"] = str(provided_id)
    else:
        prepared["element_id"] = str(_find_element_id(prepared, page_model))

    if prepared["element_id"] not in page_model.selector_map:
        raise ValueError(f"element_id not found in selector_map: {prepared['element_id']}")

    if action_type == "fill" and "value" not in prepared:
        raise ValueError("fill action requires a value")

    return prepared


def _key_matches_action(
    element_id: str,
    action: dict[str, Any],
    page_model: BrowserPageModel,
) -> bool:
    """True when the selector_map key still matches the action's requested label.

    Guards against trusting a coincidentally-valid element_id (index from the
    tool output) that page-state drift has re-pointed at a different element.
    """
    label = action.get("label") or action.get("element_label") or action.get("text")
    if not label:
        return True  # nothing to cross-check
    for element in page_model.snapshot:
        if element["element_id"] == element_id:
            element_label = element.get("label") or ""
            return (
                str(label).casefold() == element_label.casefold()
                or str(label).casefold() in element_label.casefold()
            )
    return True  # not in snapshot; trust the map key


def _find_element_id(action: dict[str, Any], page_model: BrowserPageModel) -> str:
    """Resolve the target element_id against the live snapshot.

    Tries, in order:
    1. exact label (+ optional role),
    2. partial label (+ optional role),
    3. DOM attribute match on the LLM-provided element_id / label
       (name, id, placeholder, aria-label, data-testid, title) — covers the
       common case where the model emits the real DOM ``name`` (e.g.
       ``search_query``) instead of the ARIA label.
    """
    label = action.get("label") or action.get("element_label") or action.get("text")
    role = action.get("role")
    requested_id = str(action.get("element_id") or "")
    action_type = action.get("type") or ""

    if not label and not requested_id:
        raise ValueError("browser action requires element_id or label")

    candidates = [
        element
        for element in page_model.snapshot
        if element["element_id"] in page_model.selector_map
    ]

    def role_ok(element: dict[str, Any]) -> bool:
        return role is None or element["role"] == role

    # Fill/submit must land on an element we can type into. Free models often
    # guess a generic role (e.g. "button") for text inputs, so prefer a unique
    # editable-role element with the exact label first — otherwise the
    # guessed-role partial match below can resolve to an unrelated control
    # (e.g. YouTube's voice-search button "Search with your voice") and the
    # executor then fails with "Element is still occluded".
    editable_roles = {"combobox", "searchbox", "textbox"}
    prefer_editable = action_type in ("fill", "submit")

    if label:
        if prefer_editable:
            editable_exact = [
                element
                for element in candidates
                if element["role"] in editable_roles
                and element["label"].casefold() == str(label).casefold()
            ]
            if len(editable_exact) == 1:
                return editable_exact[0]["element_id"]

        exact_matches = [
            element
            for element in candidates
            if role_ok(element) and element["label"].casefold() == str(label).casefold()
        ]
        if len(exact_matches) == 1:
            return exact_matches[0]["element_id"]

        if prefer_editable:
            editable_partial = [
                element
                for element in candidates
                if element["role"] in editable_roles
                and str(label).casefold() in element["label"].casefold()
            ]
            if len(editable_partial) == 1:
                return editable_partial[0]["element_id"]

        partial_matches = [
            element
            for element in candidates
            if role_ok(element) and str(label).casefold() in element["label"].casefold()
        ]
        if len(partial_matches) == 1:
            return partial_matches[0]["element_id"]

    # DOM attribute match: the LLM may emit the element's real name/id/etc.
    # Try the requested element_id first, then the label. Labels can differ
    # between the tool call and execution (e.g. localized labels like
    # YouTube's "Telusuri" vs "Search") while DOM name/id attributes stay
    # stable, so a label-based DOM hit is a valid fallback.
    dom_fields = ("name", "id", "placeholder", "aria_label", "test_id", "title")

    def dom_contains(element: dict[str, Any], needle: str) -> bool:
        haystack = " ".join(
            str((element.get("dom") or {}).get(field) or "") for field in dom_fields
        )
        return needle.casefold() in haystack.casefold()

    # Numeric requested_ids are selector_map indices (e.g. "4" from the tool
    # output), never DOM attributes — trying them as DOM substrings could
    # match an unrelated element that happens to contain the digit. The label
    # needle stays unconditional (a numeric label like "2024" is legitimate).
    requested_needle = requested_id if requested_id and not requested_id.isdigit() else ""
    needles = list(dict.fromkeys(n for n in (requested_needle, str(label or "")) if n))
    for needle in needles:
        # Prefer a unique role-consistent DOM match; the page state can differ
        # between the tool call and execution (consent dialogs, localized
        # labels), so fall back to a role-agnostic DOM match — a unique
        # name/id is a strong enough signal on its own.
        role_dom_matches = [
            element for element in candidates if role_ok(element) and dom_contains(element, needle)
        ]
        if len(role_dom_matches) == 1:
            return role_dom_matches[0]["element_id"]

        dom_matches = [element for element in candidates if dom_contains(element, needle)]
        if len(dom_matches) == 1:
            return dom_matches[0]["element_id"]

    target = label or requested_id
    raise ValueError(f"unable to resolve a unique browser element for label: {target}")


async def _settle_page(page, *, timeout_ms: int) -> None:
    for state, limit_ms in (("domcontentloaded", 5_000), ("load", 5_000), ("networkidle", 1_500)):
        try:
            await page.wait_for_load_state(state, timeout=min(timeout_ms, limit_ms))
        except PlaywrightTimeoutError:
            pass
    try:
        await page.wait_for_timeout(250)
    except PlaywrightError:
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
