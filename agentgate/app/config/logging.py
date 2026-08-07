import logging

from pythonjsonlogger import jsonlogger

from app.config.settings import get_settings


def configure_logging() -> None:
    """Configure structured JSON logging (see Technical Foundation §12)."""
    settings = get_settings()

    handler = logging.StreamHandler()
    handler.setFormatter(
        jsonlogger.JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={"asctime": "timestamp", "levelname": "level"},
        )
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.LOG_LEVEL.upper())
