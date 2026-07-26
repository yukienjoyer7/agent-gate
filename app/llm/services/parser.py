"""
Natural language prompt parser for browser **and connector** actions.

Parses a free-text instruction into structured ActionRequest parameters.
The AI (currently rule-based) automatically detects the **domain**:

- **Browser** — ``"Click the login button on playwright.dev"``
- **Gmail** — ``"Send email to john@example.com saying hello"``
- **GitHub** — ``"Get repo info for microsoft/vscode"``
- **Local file** — ``"Read file sample.txt"``

To use an actual LLM, replace the internals of ``parse_prompt()`` with a call
to an LLM provider while keeping the return signature unchanged.
"""

from __future__ import annotations

import re
from typing import Any

from app.core.browser_schema import BrowserElement

# ── Domain detection ────────────────────────────────────────────────

DOMAIN_KEYWORDS: list[tuple[str, str, str]] = [
    # (keyword, domain_name, target_system)
    # ── domain: booking (Critical) ──
    ("checkout", "booking", "browser"),
    ("cart", "booking", "browser"),
    ("order", "booking", "browser"),
    ("payment", "booking", "stripe"),
    ("pay", "booking", "stripe"),
    ("refund", "booking", "stripe"),
    ("transaction", "booking", "stripe"),
    # ── domain: code_protection (High) ──
    ("github", "code_protection", "github"),
    ("repository", "code_protection", "github"),
    ("repo", "code_protection", "github"),
    ("push", "code_protection", "github"),
    ("commit", "code_protection", "github"),
    ("server", "code_protection", "browser"),
    ("deploy", "code_protection", "browser"),
    ("shell", "code_protection", "browser"),
    ("terminal", "code_protection", "browser"),
    # ── domain: productivity (Medium) ──
    ("gmail", "productivity", "gmail"),
    ("email", "productivity", "gmail"),
    ("inbox", "productivity", "gmail"),
    ("send email", "productivity", "gmail"),
    ("archive email", "productivity", "gmail"),
    ("customer", "productivity", "browser"),
    ("complaint", "productivity", "browser"),
    ("hr", "productivity", "browser"),
    ("employee", "productivity", "browser"),
    ("salary", "productivity", "browser"),
    ("legal", "productivity", "browser"),
    ("contract", "productivity", "browser"),
    ("compliance", "productivity", "browser"),
    ("course", "productivity", "browser"),
    ("learning", "productivity", "browser"),
    ("tutorial", "productivity", "browser"),
    # ── domain: filesystem (Low) ──
    ("health record", "filesystem", "local_file"),
    ("medical", "filesystem", "local_file"),
    ("diagnosis", "filesystem", "local_file"),
    ("local file", "filesystem", "local_file"),
    ("read file", "filesystem", "local_file"),
]

# ── Browser keyword tables ──────────────────────────────────────────

ACTION_KEYWORDS: dict[str, str] = {
    "click": "BROWSER_CLICK",
    "tap": "BROWSER_CLICK",
    "press": "BROWSER_CLICK",
    "type": "BROWSER_TYPE",
    "fill in": "BROWSER_TYPE",
    "fill": "BROWSER_TYPE",
    "enter": "BROWSER_TYPE",
    "input": "BROWSER_TYPE",
    "navigate to": "BROWSER_OPEN",
    "go to": "BROWSER_OPEN",
    "open": "BROWSER_OPEN",
    "visit": "BROWSER_OPEN",
    "screenshot": "BROWSER_SCREENSHOT",
    "capture": "BROWSER_SCREENSHOT",
    "take a screenshot": "BROWSER_SCREENSHOT",
    "select": "BROWSER_SELECT",
    "choose": "BROWSER_SELECT",
    "submit": "BROWSER_SUBMIT",
    "send": "BROWSER_SUBMIT",
    "scroll": "BROWSER_SCROLL",
}

ROLE_KEYWORDS: dict[str, str] = {
    "button": "button",
    "btn": "button",
    "link": "link",
    "search box": "searchbox",
    "searchbar": "searchbox",
    "search": "searchbox",
    "textbox": "textbox",
    "text box": "textbox",
    "input field": "textbox",
    "field": "textbox",
    "checkbox": "checkbox",
    "radio button": "radio",
    "dropdown": "combobox",
    "menu": "menuitem",
    "tab": "tab",
    "dialog": "dialog",
}

STOP_WORDS = frozenset(
    {
        "the", "a", "an", "on", "in", "at", "into", "to", "for",
        "of", "with", "and", "or", "please", "can", "you", "i",
        "would", "like", "need", "want", "could",
    }
)

# ── Risk keyword detection ──────────────────────────────────────────
# These keywords in a prompt override the default risk_hint.
# Ordered from longest phrase to shortest for greedy matching.

RISK_KEYWORDS: list[tuple[str, str]] = [
    # → BLOCK (destructive)
    ("delete", "destructive"),
    ("remove", "destructive"),
    ("destroy", "destructive"),
    ("erase", "destructive"),
    ("wipe", "destructive"),
    ("purge", "destructive"),
    ("drop", "destructive"),
    ("kill", "destructive"),
    ("terminate", "destructive"),
    ("shutdown", "destructive"),
    # → BLOCK (unauthorized access)
    ("unauthorized", "unauthorized"),
    ("bypass", "unauthorized"),
    ("hack", "unauthorized"),
    # → BLOCK (data exfiltration)
    ("exfiltrate", "data_exfiltration"),
    ("steal", "data_exfiltration"),
    ("leak", "data_exfiltration"),
]


def parse_prompt(prompt: str) -> dict[str, Any]:
    """
    Parse a natural-language instruction into ActionRequest fields.

    Automatically detects the domain (browser / Gmail / GitHub / file)
    and extracts the relevant fields.
    """
    prompt_lower = prompt.lower().strip()
    target_system, domain_name = _detect_domain_info(prompt_lower)

    if target_system == "gmail":
        return _parse_gmail(prompt, prompt_lower)
    if target_system == "github":
        return _parse_github(prompt, prompt_lower)
    if target_system == "local_file":
        return _parse_local_file(prompt, prompt_lower)

    return _parse_browser(prompt, prompt_lower, domain_name)


def parse_prompt_plan(prompt: str) -> dict[str, Any]:
    """
    Parse a natural-language prompt into a **multi-step plan**.

    For **browser** actions, expands a single instruction into the
    sequence of steps needed (e.g. ``BROWSER_OPEN`` then ``BROWSER_CLICK``).

    For **connector** actions (Gmail, GitHub, file), returns a single-step
    plan since API calls don't need page navigation.
    """
    single = parse_prompt(prompt)
    parsed = single["parsed"]
    target_system = parsed.get("target_system", "browser")

    # Connector actions are always single-step
    if target_system != "browser":
        return {
            "plan": [parsed],
            "llm_provider": single["llm_provider"],
            "raw_prompt": prompt,
            "human_readable": "\n" + single["human_readable"],
        }

    # Browser actions: may expand to BROWSER_OPEN + action
    url = parsed.get("target") or parsed["payload"].get("url", "")
    action_type = parsed["action_type"]
    interactive_actions = {
        "BROWSER_CLICK", "BROWSER_TYPE", "BROWSER_SUBMIT",
        "BROWSER_SELECT", "BROWSER_SCROLL", "BROWSER_SCREENSHOT",
    }

    detected_domain = single.get("parsed", {}).get("domain", "browser")
    steps: list[dict[str, Any]] = []
    if url and action_type in interactive_actions:
        steps.append({
            "source": "chat",
            "domain": detected_domain,
            "action_type": "BROWSER_OPEN",
            "target_system": "browser",
            "target": url,
            "risk_hint": "unknown",
            "payload": {"url": url},
        })
    steps.append(parsed)

    lines: list[str] = []
    for i, s in enumerate(steps, 1):
        tgt = _step_target(s)
        lbl, role, val = _step_details(s)
        lines.append(f"  {i}. {_describe(s['action_type'], tgt, lbl, role, val)}")

    return {
        "plan": steps,
        "llm_provider": single["llm_provider"],
        "raw_prompt": prompt,
        "human_readable": "\n" + "\n".join(lines),
    }


# ── Domain detection ────────────────────────────────────────────────


def _detect_domain_info(prompt_lower: str) -> tuple[str, str]:
    """
    Detect both ``target_system`` and ``domain_name`` from prompt keywords.

    Returns ``(target_system, domain_name)``, defaulting to ``("browser", "browser")``.
    """
    for keyword, domain_name, target_sys in DOMAIN_KEYWORDS:
        if keyword in prompt_lower:
            return target_sys, domain_name
    return "browser", "browser"


def _detect_domain(prompt_lower: str) -> str:
    """Detect target system from prompt keywords. Returns ``'browser'`` if unknown."""
    target_sys, _domain = _detect_domain_info(prompt_lower)
    return target_sys


# ── Browser parser (existing logic) ─────────────────────────────────


def _parse_browser(prompt: str, prompt_lower: str, domain_name: str = "browser") -> dict[str, Any]:
    url = _extract_url(prompt)
    action_type = _extract_action(prompt_lower)
    element_label, element_role = _extract_element_info(prompt, prompt_lower)
    input_value = _extract_input_value(prompt)

    # Prompt-level risk overrides domain-level classification
    prompt_risk = _classify_prompt_risk(prompt_lower)
    risk_hint = prompt_risk or _classify_risk(action_type, element_label)

    payload: dict[str, Any] = {"url": url or ""}
    if action_type in ("BROWSER_TYPE", "BROWSER_SELECT", "BROWSER_SUBMIT"):
        payload["action_type"] = action_type
        if element_label:
            payload["element_id"] = element_label.lower().replace(" ", "_")
            payload["label"] = element_label
        if element_role:
            payload["role"] = element_role
        if input_value:
            payload["value"] = input_value
    elif action_type in ("BROWSER_CLICK",):
        payload["action_type"] = action_type
        if element_label:
            payload["element_id"] = element_label.lower().replace(" ", "_")
            payload["label"] = element_label
        if element_role:
            payload["role"] = element_role

    action_request: dict[str, Any] = {
        "source": "chat",
        "domain": domain_name,
        "action_type": action_type,
        "target_system": "browser",
        "target": url or "",
        "risk_hint": risk_hint,
        "payload": payload,
    }

    if element_label:
        try:
            be = BrowserElement(
                snapshot_id="", element_id="",
                role=element_role or "", label=element_label,
                risk_hint=risk_hint,
            )
            action_request["browser_element"] = be.model_dump(mode="json")
        except Exception:
            pass

    return {
        "parsed": action_request,
        "llm_provider": "dummy",
        "raw_prompt": prompt,
        "human_readable": _describe(action_type, url, element_label, element_role, input_value),
    }


# ── Connector parsers ───────────────────────────────────────────────


def _parse_gmail(prompt: str, prompt_lower: str) -> dict[str, Any]:
    """Parse Gmail-related prompts into ActionRequest fields."""
    action = "read"
    recipient = ""
    body = ""
    query = ""

    if any(kw in prompt_lower for kw in ("send", "send email", "send to")):
        action = "send"
        # Extract quoted body
        body = _extract_input_value(prompt) or ""
        # Extract recipient after "to" or "send to"
        to_match = re.search(r"(?:to|send to)\s+([^\s,;]+)", prompt_lower)
        if to_match:
            recipient = to_match.group(1)
    elif any(kw in prompt_lower for kw in ("archive", "archive email")):
        action = "archive"
        # Extract search query after "about" or "regarding"
        q_match = re.search(r"(?:about|regarding)\s+(.+)", prompt_lower)
        if q_match:
            query = q_match.group(1).strip()

    target = recipient or "inbox"
    payload: dict[str, Any] = {
        "action": action,
        "query": query or "",
    }
    if recipient:
        payload["to"] = recipient
    if body:
        payload["body"] = body

    action_request: dict[str, Any] = {
        "source": "chat",
        "domain": "productivity",
        "action_type": "API_CALL",
        "target_system": "gmail",
        "target": target,
        "risk_hint": _classify_prompt_risk(prompt_lower) or ("external_send" if action == "send" else "unknown"),
        "payload": payload,
    }

    human = f"Gmail → {action}" + (f" → \"{body}\"" if body else "") + (f" → to {recipient}" if recipient else "")
    if query:
        human += f" → about \"{query}\""

    return {
        "parsed": action_request,
        "llm_provider": "dummy",
        "raw_prompt": prompt,
        "human_readable": human,
    }


def _parse_github(prompt: str, prompt_lower: str) -> dict[str, Any]:
    """Parse GitHub-related prompts into ActionRequest fields."""
    owner = ""
    repo = ""

    # Extract owner/repo from "for owner/repo" or "of owner/repo"
    repo_match = re.search(r"(?:for|of)\s+([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)", prompt_lower)
    if repo_match:
        parts = repo_match.group(1).split("/", 1)
        owner, repo = parts[0], parts[1]

    payload: dict[str, Any] = {
        "action": "repo_metadata",
        "owner": owner,
        "repo": repo,
    }
    target = f"{owner}/{repo}" if owner and repo else ""

    action_request: dict[str, Any] = {
        "source": "chat",
        "domain": "code_protection",
        "action_type": "API_CALL",
        "target_system": "github",
        "target": target,
        "risk_hint": _classify_prompt_risk(prompt_lower) or "unknown",
        "payload": payload,
    }

    human = f"GitHub → repo_metadata" + (f" → {target}" if target else "")

    return {
        "parsed": action_request,
        "llm_provider": "dummy",
        "raw_prompt": prompt,
        "human_readable": human,
    }


def _parse_local_file(prompt: str, prompt_lower: str) -> dict[str, Any]:
    """Parse local-file-related prompts into ActionRequest fields."""
    path = ""

    # Extract filename — try "read file <path>" first, then "file <path>", then "read <path>"
    for file_pattern in [
        r"read\s+file\s+([a-zA-Z0-9_.\-/\\]+(?:\.[a-zA-Z0-9]+)?)",
        r"file\s+([a-zA-Z0-9_.\-/\\]+(?:\.[a-zA-Z0-9]+)?)",
        r"read\s+([a-zA-Z0-9_.\-/\\]+(?:\.[a-zA-Z0-9]+)?)",
    ]:
        file_match = re.search(file_pattern, prompt_lower)
        if file_match:
            path = file_match.group(1)
            break

    payload: dict[str, Any] = {
        "action": "read",
        "path": path,
    }

    action_request: dict[str, Any] = {
        "source": "chat",
        "domain": "filesystem",
        "action_type": "FILE_READ",
        "target_system": "local_file",
        "target": path,
        "risk_hint": _classify_prompt_risk(prompt_lower) or "file_read",
        "payload": payload,
    }

    human = f"File → read" + (f" → {path}" if path else "")

    return {
        "parsed": action_request,
        "llm_provider": "dummy",
        "raw_prompt": prompt,
        "human_readable": human,
    }


# ── Shared helpers ──────────────────────────────────────────────────


def _extract_url(prompt: str) -> str | None:
    """Return a full ``https://...`` URL or ``None``."""
    m = re.search(r"https?://[^\s)\"]+", prompt)
    if m:
        url = m.group(0)
        url = re.sub(r"[!?.,;:]+$", "", url)
        return url

    url_prefixes: list[tuple[str, str]] = [
        (r"\bon\b", "on"), (r"\bat\b", "at"), (r"\bto\b", "to"),
        (r"\bopen\b", "open"), (r"\bvisit\b", "visit"),
        (r"\bnavigate\s+to\b", "navigate to"), (r"\bgo\s+to\b", "go to"),
    ]
    prompt_lower = prompt.lower()

    for word_pattern, _label in url_prefixes:
        match = re.search(word_pattern, prompt_lower)
        if not match:
            continue
        after = prompt[match.end():].strip()
        if not after:
            continue
        domain_match = re.match(
            r"((?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}(?::\d+)?(?:/[^\s)\"]*)?)",
            after,
        )
        if domain_match:
            domain = domain_match.group(1)
            domain = re.sub(r"[!?.,;:]+$", "", domain)
            if not domain.startswith(("http://", "https://")):
                domain = f"https://{domain}"
            return domain
    return None


def _extract_action(prompt_lower: str) -> str:
    for phrase in sorted(ACTION_KEYWORDS, key=len, reverse=True):
        if phrase in prompt_lower:
            return ACTION_KEYWORDS[phrase]
    return "BROWSER_OPEN"


def _extract_element_info(prompt: str, prompt_lower: str) -> tuple[str | None, str | None]:
    label: str | None = None
    role: str | None = None

    for keyword, mapped_role in sorted(ROLE_KEYWORDS.items(), key=len, reverse=True):
        if keyword in prompt_lower:
            role = mapped_role
            idx = prompt_lower.find(keyword)
            before = prompt[:idx].strip()
            for w in sorted(ACTION_KEYWORDS, key=len, reverse=True):
                before = re.sub(rf"\b{re.escape(w)}\b", "", before, flags=re.IGNORECASE)
            tokens = [t for t in before.split() if t.lower() not in STOP_WORDS]
            if tokens:
                label = " ".join(tokens).title()
            break

    if not label:
        cleaned = prompt_lower
        for action_word in sorted(ACTION_KEYWORDS, key=len, reverse=True):
            cleaned = re.sub(rf"\b{re.escape(action_word)}\b", "", cleaned).strip()
        tokens = [
            t for t in cleaned.split()
            if t not in STOP_WORDS
               and not t.startswith(("http", "www"))
               and not t.startswith(".")
        ]
        meaningful = [t for t in tokens if re.match(r"^[a-zA-Z]{2,}", t)]
        if meaningful:
            label = " ".join(meaningful[:3]).title()
    return label, role


def _extract_input_value(prompt: str) -> str | None:
    m = re.search(r"""['"]([^'"]+)['"]""", prompt)
    return m.group(1) if m else None


def _classify_prompt_risk(prompt_lower: str) -> str:
    """
    Check the prompt for high-risk keywords (destructive, unauthorized,
    data exfiltration). Returns the matched ``risk_hint`` or ``None``.

    This is checked **before** domain-specific risk classification, so
    destructive actions are always caught regardless of domain.
    """
    for keyword, risk_hint in RISK_KEYWORDS:
        if keyword in prompt_lower:
            return risk_hint
    return None


def _classify_risk(action_type: str, element_label: str | None) -> str:
    if action_type in ("BROWSER_SUBMIT", "BROWSER_SELECT"):
        return "external_send" if element_label else "unknown"
    return "unknown"


def _describe(action_type: str, url: str | None, label: str | None,
              role: str | None, value: str | None) -> str:
    parts = [action_type.replace("BROWSER_", "").title()]
    if role or label:
        desc = " ".join(filter(None, [role, f'"{label}"' if label else ""]))
        parts.append(desc)
    if value:
        parts.append(f"value={value!r}")
    if url:
        parts.append(f"on {url}")
    return " → ".join(parts)


def _step_target(step: dict[str, Any]) -> str | None:
    t = step.get("target") or ""
    if t:
        return t
    p = step.get("payload")
    if isinstance(p, dict):
        return p.get("url") or p.get("path") or ""
    return ""


def _step_details(step: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    p = step.get("payload")
    if not isinstance(p, dict):
        return None, None, None
    return p.get("label"), p.get("role"), p.get("value")
