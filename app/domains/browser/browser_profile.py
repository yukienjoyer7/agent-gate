"""Single source of truth for the Chrome fingerprint used across the project.

Every Playwright ``new_page()`` call (browser prototype agent, accessibility
tree tool, demo scripts) must use this UA + extra headers so the browser
profile stays consistent and hard to fingerprint as automation.

Note: the ``sec-ch-ua`` headers advertise Chrome 124, so they are coupled to
``BROWSER_USER_AGENT``. If you override the UA in settings, update the header
values here too (or the fingerprint becomes inconsistent).
"""

from __future__ import annotations

from app.config.settings import get_settings

# Chrome 124 desktop fingerprint (Windows). Overridable via
# ``BROWSER_USER_AGENT`` in settings — see :func:`user_agent`.
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_EXTRA_HEADERS: dict[str, str] = {
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9," "image/avif,image/webp,*/*;q=0.8"
    ),
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Upgrade-Insecure-Requests": "1",
}


def user_agent() -> str:
    """Return the configured browser user agent (defaults to Chrome 124)."""
    return get_settings().BROWSER_USER_AGENT or _DEFAULT_USER_AGENT
