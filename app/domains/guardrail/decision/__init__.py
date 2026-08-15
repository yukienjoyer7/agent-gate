from app.domains.guardrail.decision.hybrid import adecide
from app.domains.guardrail.decision.llm import decide_llm
from app.domains.guardrail.decision.simple import decide_rule

# Sync alias kept for legacy callers — deterministic rules only.
decide = decide_rule

__all__ = ["adecide", "decide", "decide_llm", "decide_rule"]
