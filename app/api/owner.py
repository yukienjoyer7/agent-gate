"""Current application identity bridge for the unauthenticated demo.

Production authentication should replace this header with its authenticated
principal. Telegram linking never treats a Telegram chat ID as an owner ID.
"""

from fastapi import HTTPException


def owner_id_from_header(value: str | None) -> str:
    owner = (value or "default").strip()
    if not owner or len(owner) > 255 or any(ord(char) < 32 for char in owner):
        raise HTTPException(status_code=400, detail="invalid AgentGate owner identity")
    return owner
