from pydantic import BaseModel


class BrowserElement(BaseModel):
    snapshot_id: str
    element_id: str
    role: str = ""
    label: str = ""
    text: str = ""
    risk_hint: str = "unknown"
