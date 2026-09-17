from app.domains.guardrail.decision.hybrid import adecide
from app.domains.guardrail.decision.llm import decide_llm
from app.domains.guardrail.decision.simple import decide_rule


def decide(action):
    """Synchronous entry point with the same backend as async execution."""
    from app.config.settings import get_settings

    if get_settings().GUARDRAIL_BACKEND == "agentgate":
        from app.domains.guardrail.decision.agentgate import decide_agentgate

        return decide_agentgate(action)
    return decide_rule(action)


__all__ = ["adecide", "decide", "decide_llm", "decide_rule"]
