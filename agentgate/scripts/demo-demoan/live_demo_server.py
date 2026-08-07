"""
Live demo: type a goal in plain English, an LLM (via NVIDIA NIM) turns it
into an AgentGate action proposal, and that proposal runs through the REAL
guardrail pipeline -- the exact same decide() / ExecutionRouter / audit
code path as scripts/audit_db_prototype_demo.py, just driven by an LLM
instead of hand-built proposal dicts, with a browser UI on top so pending
approvals and clarifications can be resolved by clicking instead of
writing more Python.

Setup:
  1. Migrations applied (same DB as audit_db_prototype_demo.py):
       uv run alembic upgrade head
  2. NVIDIA_API_KEY in your environment or in agentgate/.env -- get a free
     key at https://build.nvidia.com (open any model page, "Get API Key").
     Optionally set NVIDIA_NIM_MODEL to override the default model.

Run:
  uv run python scripts/live_demo_server.py
  -> open http://127.0.0.1:8008
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.core.schemas import AuditEvent, new_id
from app.domains.agent.services import run_guarded_action
from app.domains.approval.repositories.pending_approval_repository import PendingApprovalRepository
from app.domains.approval.schemas.pending_approval import ApprovalDecision, PendingApprovalResponse
from app.domains.approval.services import decide_pending_approval
from app.domains.audit.repositories import get_audit_repository
from app.domains.clarification.repositories import PendingUserQuestionRepository
from app.domains.clarification.schemas.pending_user_question import (
    PendingUserQuestionResponse,
    UserResponseRequest,
)
from app.domains.clarification.services import decide_pending_question

# --- tiny .env loader (avoids adding python-dotenv as a project dependency
# just for this demo script) --------------------------------------------
def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _find_project_root(start: Path) -> Path:
    """Walk up from this file until we find the agentgate project root
    (identified by pyproject.toml), instead of assuming a fixed nesting
    depth under scripts/ -- so this still works whether the script lives
    at scripts/live_demo_server.py or scripts/anything/nested/here.py."""
    for candidate in (start, *start.parents):
        if (candidate / "pyproject.toml").exists():
            return candidate
    return start.parent  # fallback: same behavior as before if not found


_ROOT = _find_project_root(Path(__file__).resolve().parent)
for _env_file in (".env", ".env.development"):
    _load_env_file(_ROOT / _env_file)

NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY", "")
NVIDIA_MODEL = os.environ.get("NVIDIA_NIM_MODEL", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning")
NVIDIA_URL = "https://integrate.api.nvidia.com/v1/chat/completions"

SYSTEM_PROMPT = """You are the planning module of an autonomous agent. Given a short goal \
from a user, output EXACTLY ONE JSON object describing the single action you would \
propose to take -- nothing else. No markdown fences, no commentary, no explanation.

Shape:
{
  "action_type": one of "FILE_READ" | "EMAIL_SEND" | "EMAIL_ARCHIVE" | "EMAIL_SEARCH" | \
"GITHUB_REPO_METADATA" | "GITHUB_CREATE_ISSUE" | "GITHUB_DELETE_REPO" | "gmail_archive" | \
"delete_repo" | "list_files" | "CALENDAR_LIST_EVENTS" | "CALENDAR_CREATE_EVENT" | \
"CALENDAR_CANCEL_EVENT" | "TELEGRAM_SEND_MESSAGE" | "STRIPE_CREATE_CHARGE" | "STRIPE_REFUND" | \
"BROWSER_OPEN" | "BROWSER_SNAPSHOT" | "BROWSER_CLICK" | "BROWSER_TYPE" | "BROWSER_SELECT" | \
"BROWSER_SUBMIT" | "BROWSER_SCREENSHOT",
  "domain": one of "filesystem" | "productivity" | "code_protection" | "browser" | "messaging" | "payments",
  "target_system": one of "local_file" | "gmail" | "github" | "browser" | "calendar" | "telegram" | "stripe_sandbox",
  "target": short string identifying what the action touches,
  "risk_hint": one of "unknown" | "file_read" | "source_code" | "external_send" | "payment" | "destructive" | "bulk_action",
  "payload": object with the action's parameters,
  "confidence": number between 0 and 1, your confidence this proposal correctly satisfies the goal
}

Field notes:
- FILE_READ payload should include "path" when the goal specifies which file. If the goal \
does NOT say which file, deliberately omit "path" -- this should trigger a clarification \
question rather than a guess.
- EMAIL_SEND payload should include "to" and "subject" when known; omit whichever the goal \
doesn't specify, same reasoning as above.
- risk_hint "source_code" is for anything touching protected source files or repo internals \
-- this gets hard-blocked, use it when the goal is clearly inappropriate (e.g. "read the \
production secrets file", "dump the settings.py").
- risk_hint "destructive", "bulk_action", "payment", or "external_send" all require human \
approval before executing -- use one of these for anything consequential: deleting things, \
sending messages to other people, spending money, cancelling calendar events, or bulk \
operations. There is no separate risk_hint for messaging/calendar/refund actions -- reuse \
"external_send" for anything sent to another person (Telegram messages included), "payment" \
for anything involving money (including STRIPE_REFUND), and "destructive" for cancellations.
- target_system "calendar", "telegram", and "stripe_sandbox" are NOT YET IMPLEMENTED connectors \
-- proposals routed to them will reach the guardrail and audit log normally, but execution \
will come back FAILED with "unknown connector". Still propose them when they're the correct \
action for the goal; this is expected and useful for exercising the pipeline, not a bug.
- If the goal's natural payload happens to contain something like a person's email address \
or phone number that isn't itself the point of the request, just include it in the payload \
as-is -- the guardrail detects and redacts that on its own, don't sanitize it yourself.
- Respond with ONLY the JSON object, nothing before or after it.
"""

# --- Sequential mode -----------------------------------------------------
# Extends SYSTEM_PROMPT (rather than replacing it) so /api/propose and
# /api/submit keep behaving exactly as before. Sequential mode asks the LLM
# for ONE step at a time, fed the running history of prior steps in this
# run, plus one extra field ("done") the model uses to signal the plan is
# finished. Every step is still a fully separate ActionRequest that goes
# through decide() / ExecutionRouter / audit write on its own -- this is
# purely a client-side loop that (a) shares one run_id across the steps so
# they group under the same run in the audit log/API, per the existing
# run_id grouping already used by AuditRepository.by_run() and
# GET /runs/{run_id}/actions, and (b) feeds each result back to the model
# so it knows what happened before proposing the next action. No schema or
# audit_logs change -- action_id stays unique per row as already enforced
# by migration 0001.
SEQUENCE_SYSTEM_PROMPT = (
    SYSTEM_PROMPT
    + """

Sequential mode:
You are being asked for ONE step at a time in a multi-step plan, not the whole plan at once. \
The user message will include the overall goal and a summary of steps already taken in this \
run (if any), each with what was proposed and how it turned out. Propose only the next single \
action needed to make progress -- do not try to describe the whole remaining plan in one \
proposal. Add exactly one more field to the JSON object:
  "done": boolean -- true if the action you are proposing right now is the LAST action needed \
to fully satisfy the overall goal (it still gets executed), false if at least one more step \
will be needed after this one.
Do not repeat a step that already succeeded, unless the goal explicitly requires repeating it \
(e.g. sending several distinct messages). If a prior step in the history failed or was \
blocked, you may retry it, propose an alternative, or treat the goal as unachievable -- use \
your judgement, and if unachievable set "done": true on a proposal that best reflects why (a \
FILE_READ of the intended target, etc.) rather than looping forever.
"""
)


class SequenceRequest(BaseModel):
    goal: str
    max_steps: int = 6


async def _propose_step(goal: str, history: list[str]) -> dict:
    """Ask the LLM for the next single action in a sequence, given a running
    history of prior steps. Mirrors propose()'s HTTP/parsing logic but talks
    to SEQUENCE_SYSTEM_PROMPT and includes step history in the user turn."""
    if not NVIDIA_API_KEY:
        raise HTTPException(500, "NVIDIA_API_KEY is not set -- add it to agentgate/.env and restart")

    user_content = goal
    if history:
        user_content = (
            f"Overall goal: {goal}\n\n"
            "Steps already taken in this run (most recent last):\n" + "\n".join(history) + "\n\n"
            "Propose the next single action needed to continue toward the overall goal."
        )

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            NVIDIA_URL,
            headers={"Authorization": f"Bearer {NVIDIA_API_KEY}"},
            json={
                "model": NVIDIA_MODEL,
                "messages": [
                    {"role": "system", "content": SEQUENCE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                "temperature": 0.4,
                "max_tokens": 400,
            },
        )
    if resp.status_code != 200:
        raise HTTPException(502, f"NVIDIA NIM error {resp.status_code}: {resp.text[:300]}")

    raw = resp.json()["choices"][0]["message"]["content"].strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(502, f"model did not return valid JSON: {raw[:300]}") from exc


app = FastAPI()


@app.exception_handler(Exception)
async def _debug_exception_handler(request, exc: Exception):
    # This is a local dev demo tool, not a production service -- surfacing
    # the real traceback as JSON (instead of FastAPI's default opaque
    # plain-text "Internal Server Error") is worth the tradeoff so the
    # frontend can actually show what broke instead of failing on
    # resp.json() with no useful information.
    import traceback

    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=500,
        content={"detail": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()},
    )


class GoalRequest(BaseModel):
    goal: str


class SubmitRequest(BaseModel):
    proposal: dict


class ApprovalDecisionRequest(BaseModel):
    action_id: str
    decision: str  # "APPROVE" | "REJECT"


class AskUserRespondRequest(BaseModel):
    action_id: str
    proceed: bool
    payload_updates: dict | None = None


def _shape(obj: AuditEvent | PendingApprovalResponse | PendingUserQuestionResponse) -> dict:
    """
    Normalize the three possible return shapes from run_guarded_action() /
    decide_pending_approval() / decide_pending_question() into one
    JSON-friendly envelope the frontend can branch on via "kind".
    """
    if isinstance(obj, AuditEvent):
        return {"kind": "audit", **obj.model_dump(mode="json")}
    if isinstance(obj, PendingApprovalResponse):
        return {"kind": "pending_approval", **obj.model_dump(mode="json")}
    if isinstance(obj, PendingUserQuestionResponse):
        return {"kind": "pending_question", **obj.model_dump(mode="json")}
    raise TypeError(f"unexpected result type: {type(obj)!r}")


@app.post("/api/propose")
async def propose(body: GoalRequest) -> dict:
    """Ask the NVIDIA NIM-hosted LLM to turn a goal into a raw proposal dict."""
    if not NVIDIA_API_KEY:
        raise HTTPException(500, "NVIDIA_API_KEY is not set -- add it to agentgate/.env and restart")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            NVIDIA_URL,
            headers={"Authorization": f"Bearer {NVIDIA_API_KEY}"},
            json={
                "model": NVIDIA_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": body.goal},
                ],
                "temperature": 0.4,
                "max_tokens": 400,
            },
        )
    if resp.status_code != 200:
        raise HTTPException(502, f"NVIDIA NIM error {resp.status_code}: {resp.text[:300]}")

    raw = resp.json()["choices"][0]["message"]["content"].strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        proposal = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(502, f"model did not return valid JSON: {raw[:300]}") from exc

    proposal.setdefault("source", "llm_demo")
    return {"proposal": proposal}


@app.post("/api/submit")
async def submit(body: SubmitRequest) -> dict:
    """Run a proposal through the real guardrail pipeline."""
    result = await run_guarded_action(body.proposal, audit=get_audit_repository())
    return _shape(result)


@app.post("/api/approvals/decide")
async def approvals_decide(body: ApprovalDecisionRequest) -> dict:
    decision = ApprovalDecision.APPROVE if body.decision == "APPROVE" else ApprovalDecision.REJECT
    result = await decide_pending_approval(
        body.action_id, decision, repo=PendingApprovalRepository(), audit=get_audit_repository()
    )
    if result is None:
        raise HTTPException(404, "pending approval not found (already resolved or expired)")
    outcome, event = result
    return {"outcome": outcome, **_shape(event)}


@app.post("/api/ask-user/respond")
async def ask_user_respond(body: AskUserRespondRequest) -> dict:
    result = await decide_pending_question(
        body.action_id,
        UserResponseRequest(proceed=body.proceed, payload_updates=body.payload_updates),
        repo=PendingUserQuestionRepository(),
        audit=get_audit_repository(),
    )
    if result is None:
        raise HTTPException(404, "pending question not found (already resolved or expired)")
    outcome, payload = result
    return {"outcome": outcome, **_shape(payload)}


@app.post("/api/sequence/run")
async def sequence_run(body: SequenceRequest) -> dict:
    """
    Run a goal as a sequence of individually-guarded actions sharing one
    run_id. Each step is proposed by the LLM given the outcome of prior
    steps, then submitted through the exact same run_guarded_action()
    pipeline as /api/submit -- one ActionRequest -> DecisionResponse ->
    ExecutionResult -> AuditEvent per step, one row in audit_logs per step
    (action_id stays unique, per migration 0001), grouped by run_id.

    Stops early (status="paused") if a step comes back as a pending
    approval or pending clarification -- the sequence can't safely guess a
    human's decision, so it hands control back to the existing
    /api/approvals/decide or /api/ask-user/respond endpoints. Resuming the
    rest of the sequence after that decision is out of scope here (would
    need the approval-resume orchestration flow) and is not attempted.
    """
    run_id = new_id("run")
    history: list[str] = []
    steps: list[dict] = []

    for step_no in range(1, body.max_steps + 1):
        proposal = await _propose_step(body.goal, history)
        is_final = bool(proposal.pop("done", False))
        proposal.setdefault("source", "llm_demo_sequence")
        proposal["run_id"] = run_id

        result = await run_guarded_action(proposal, audit=get_audit_repository())
        shaped = _shape(result)
        steps.append({"step": step_no, "proposal": proposal, "result": shaped})

        if shaped["kind"] != "audit":
            # NEED_APPROVAL or ASK_USER -- pause; don't guess the outcome.
            return {"run_id": run_id, "status": "paused", "steps": steps}

        history.append(
            f"Step {step_no}: proposed {proposal.get('action_type')} on "
            f"{proposal.get('target_system')} -> execution_status="
            f"{shaped.get('execution_status')}"
        )

        if is_final:
            return {"run_id": run_id, "status": "done", "steps": steps}

    return {"run_id": run_id, "status": "max_steps_reached", "steps": steps}


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (Path(__file__).resolve().parent / "live_demo.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=8008)
    server = uvicorn.Server(config)

    if sys.platform == "win32":
        # uvicorn.run() calls asyncio.run() internally, and in Python 3.14
        # that no longer reliably respects the deprecated global
        # set_event_loop_policy() call above -- it still spins up a
        # ProactorEventLoop, which psycopg's async driver refuses to run
        # under (see the InterfaceError it raises pointing at this exact
        # fix). Explicit loop_factory= is what psycopg's own error message
        # recommends, so we drive uvicorn ourselves instead of calling
        # uvicorn.run().
        import selectors

        asyncio.run(
            server.serve(),
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
        )
    else:
        asyncio.run(server.serve())