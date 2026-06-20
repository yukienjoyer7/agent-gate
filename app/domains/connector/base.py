from abc import ABC, abstractmethod
from typing import Any


class BaseConnector(ABC):
    """Common interface all connectors must implement (Technical Foundation §9)."""

    @abstractmethod
    async def execute(self, action: str, payload: dict[str, Any]) -> Any:
        ...
