"""Shared FastAPI dependencies (DB session, auth, etc.).

Re-exports the database session dependency for convenient import in routers.
"""

from app.database.session import get_session

__all__ = ["get_session"]
