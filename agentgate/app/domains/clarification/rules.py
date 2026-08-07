"""
Completeness policy for the ASK_USER flow.

Per the ASK_USER-narrowing ADR: ASK_USER exists only because a proposal is
structurally incomplete -- required information is missing from its
payload, so the guardrail cannot evaluate it at all. This is clarification
policy (what counts as "enough to evaluate"), not decision logic (what to
do once evaluable) and not connector implementation (how a field is used
once execution happens) -- so it lives here, in the clarification domain,
rather than in guardrail/decision/simple.py or on the connectors
themselves. decide() consumes check_completeness() as one input; it does
not own the required-field table.

Only action_types listed in REQUIRED_FIELDS have a registered completeness
requirement. For those, completeness is the *only* signal decide() uses to
trigger ASK_USER -- confidence is not consulted, because a static,
never-derived float has no principled way to represent "this specific
field is missing" and would (as the Phase 4 confidence-bump hack showed)
require a manual override to ever resolve.

For action_types with no entry here, there's no known field shape to
check completeness against, so decide() falls back to the confidence
threshold as it did before this change -- unchanged behavior, not a new
uncertainty mechanism. That fallback has a consequence worth being
explicit about: since resolving ASK_USER now requires the proposal itself
to change (see clarification_service._resolve, which no longer bumps
confidence), a low-confidence proposal of an unregistered action_type has
no field a user can "supply" to fix it. It will simply re-ask up to
MAX_CLARIFICATION_ROUNDS and then fail closed to BLOCK. That's intentional
under the "decision only changes if the proposal changes" invariant, not
an oversight -- if this becomes a real pattern, the fix is to register the
action_type here, not to reintroduce a confidence-bump escape hatch.
"""

from __future__ import annotations

from app.core.schemas import ActionRequest

# action_type -> payload fields that must be present (and non-empty) before
# the proposal is complete enough to evaluate. Keys are the action_type
# strings as they appear on ActionRequest, matching whatever produced them
# (agent, CLI, etc.) rather than any connector-internal naming.
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "FILE_READ": ("path",),
    "EMAIL_SEND": ("to", "subject"),
    "GITHUB_REPO_METADATA": ("owner", "repo"),
}


def is_registered(action_type: str) -> bool:
    """True if action_type has a completeness requirement registered here."""
    return action_type in REQUIRED_FIELDS


def check_completeness(action: ActionRequest) -> tuple[bool, list[str]]:
    """
    Returns (is_complete, missing_fields) for action_types registered in
    REQUIRED_FIELDS. For unregistered action_types, always returns
    (True, []) -- there's nothing to check, so decide() should fall back
    to its confidence-only path instead of treating this as complete-and-
    therefore-confidence-irrelevant. Callers must check is_registered()
    separately to distinguish "verified complete" from "not applicable".
    """
    required = REQUIRED_FIELDS.get(action.action_type)
    if not required:
        return True, []

    missing = [field for field in required if not action.payload.get(field)]
    return (not missing, missing)
