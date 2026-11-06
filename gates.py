"""Az automatikus lezárás kapui. Mindegyik kiértékelődik (később abláció miatt)."""

from __future__ import annotations

from dataclasses import dataclass

from models import TriageResult

GATE_NAMES: tuple[str, ...] = (
    "mode",                 # 1. csak autonomous üzemmódban
    "can_auto_resolve",     # 2. a modell lezárhatónak ítélte
    "resolution_present",   # 3. van érdemi megoldásszöveg
    "confidence",           # 4. megbízhatóság >= küszöb
    "category_allowlist",   # 5. a kategória engedélyezett
    "priority",             # 6. elég alacsony prioritás
    "kb_grounding",         # 7. a hivatkozott KB-cikk valóban a keresési találatok között van
    "no_injection",         # 8. nincs prompt-injection gyanú
)

MIN_RESOLUTION_CHARS = 20


@dataclass(frozen=True)
class GateConfig:
    mode: str
    min_confidence: float
    allowed_categories: frozenset[str]
    min_priority_number: int  # 1 = kritikus ... 5 = tervezési


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    failed: tuple[str, ...]


def evaluate_auto_close(
    triage: TriageResult,
    *,
    incident_priority: int,
    kb_numbers: set[str],
    injection_suspected: bool,
    config: GateConfig,
) -> GateDecision:
    # A ServiceNow és a modell szerinti prioritás közül a súlyosabbat (kisebb számot) vesszük.
    effective_priority = min(incident_priority, triage.priority)
    resolution = (triage.resolution or "").strip()

    checks: dict[str, bool] = {
        "mode": config.mode == "autonomous",
        "can_auto_resolve": triage.can_auto_resolve,
        "resolution_present": len(resolution) >= MIN_RESOLUTION_CHARS,
        "confidence": triage.confidence >= config.min_confidence,
        "category_allowlist": triage.category in config.allowed_categories,
        "priority": effective_priority >= config.min_priority_number,
        "kb_grounding": triage.kb_article is not None and triage.kb_article in kb_numbers,
        "no_injection": not injection_suspected,
    }
    failed = tuple(name for name in GATE_NAMES if not checks[name])
    return GateDecision(allowed=not failed, failed=failed)
