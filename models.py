"""Adatmodellek."""
from enum import Enum

from pydantic import BaseModel, Field


class Category(str, Enum):
    NETWORK = "network"
    HARDWARE = "hardware"
    SOFTWARE = "software"
    ACCESS = "access"  # jelszó, jogosultság, fiók
    EMAIL = "email"
    SECURITY = "security"
    OTHER = "other"


class Incident(BaseModel):
    sys_id: str
    number: str
    short_description: str = ""
    description: str = ""
    caller: str = ""
    state: str = ""
    category: str = ""
    assignment_group: str = ""


class TriageResult(BaseModel):
    category: Category
    priority: int = Field(ge=1, le=4, description="1=kritikus ... 4=alacsony")
    assignment_group: str
    summary: str
    suggested_resolution: str = ""
    auto_resolvable: bool = False
    confidence: float = Field(ge=0.0, le=1.0)
    # Megalapozás: a lekérdezett tudásbázis-cikk száma, amelyre a megoldás épül.
    kb_reference: str = ""
    # Származás: melyik modell állította elő az eredményt.
    model: str = ""


class KnowledgeArticle(BaseModel):
    sys_id: str
    number: str
    short_description: str
    text: str = ""


class AutomationOutcome(BaseModel):
    incident_number: str
    action: str  # resolved | routed | escalated | shadow | skipped
    triage: TriageResult | None = None
    gates_failed: list[str] = Field(default_factory=list)
    detail: str = ""
