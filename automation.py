"""Feldolgozás: tisztítás -> KB-keresés -> triázs -> kapuk -> visszaírás.

shadow: csak napló, assist: csoport + munkajegyzet, autonomous: + lezárás, ha a kapuk engedik.
TODO: audit napló és idempotencia SQLite-ba (audit.py), most csak memóriában.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Callable, Protocol

from ai_triage import Triager
from config import Settings
from gates import GateConfig, GateDecision, evaluate_auto_close
from models import Incident, KBArticle, ProcessingOutcome, TriageResult
from security import detect_injection, mask_pii, sanitize_text
from servicenow import ServiceNowError, validate_sys_id

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("audit")

# ServiceNow alapértelmezett állapotkódok: 6 = Resolved, 7 = Closed, 8 = Canceled.
CLOSED_STATES = frozenset({"6", "7", "8"})

_STOPWORDS = frozenset(
    """a az egy és vagy de hogy nem is meg már még csak mert ami amely ezt azt nincs van vannak
    lesz volt nem tudok tudom nekem engem kell kellene kérem kérném segítség segítséget probléma
    hiba hibát hibás the and for with not cannot can't does doesn't please help issue problem
    error from this that have when after""".split()
)
_WORD_RE = re.compile(r"[\wÀ-ž]+", re.UNICODE)


def extract_keywords(text: str, limit: int = 5) -> list[str]:
    """Egyszerű kulcsszó-kinyerés a KB-kereséshez (szótövezés nélkül)."""
    seen: list[str] = []
    for word in _WORD_RE.findall(text.lower()):
        if len(word) >= 3 and word not in _STOPWORDS and not word.isdigit() and word not in seen:
            seen.append(word)
        if len(seen) >= limit:
            break
    return seen


class ServiceNowPort(Protocol):
    async def get_incident(self, sys_id: str) -> Incident: ...
    async def search_kb(self, terms: list[str], limit: int = 3) -> list[KBArticle]: ...
    async def find_group_sys_id(self, name: str) -> str | None: ...
    async def update_incident(self, sys_id: str, fields: dict[str, Any]) -> dict[str, Any]: ...


class AutomationService:
    def __init__(
        self,
        servicenow: ServiceNowPort,
        triager: Triager,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sn = servicenow
        self._triager = triager
        self._settings = settings
        self._clock = clock
        self._processed: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._gate_config = GateConfig(
            mode=settings.autonomy_mode,
            min_confidence=settings.auto_close_min_confidence,
            allowed_categories=settings.auto_close_category_set,
            min_priority_number=settings.auto_close_min_priority_number,
        )

    @property
    def mode(self) -> str:
        return self._settings.autonomy_mode

    # Idempotencia (memóriabeli, egy példányra)
    # A saját visszaírásunk (munkajegyzet) a ServiceNow üzleti szabályán keresztül újabb
    # webhookot válthat ki; a TTL-es tár ezt a visszacsatolási hurkot is megszakítja.

    async def _claim(self, sys_id: str) -> bool:
        async with self._lock:
            now = self._clock()
            ttl = self._settings.idempotency_ttl_s
            self._processed = {k: t for k, t in self._processed.items() if now - t < ttl}
            if sys_id in self._processed:
                return False
            self._processed[sys_id] = now
            return True

    async def _release(self, sys_id: str) -> None:
        async with self._lock:
            self._processed.pop(sys_id, None)

    # Belépési pontok
    async def process(self, sys_id: str, *, force: bool = False) -> ProcessingOutcome:
        validate_sys_id(sys_id)
        claimed = await self._claim(sys_id)
        if not claimed and not force:
            outcome = ProcessingOutcome(
                sys_id=sys_id, mode=self.mode, action="skipped",
                detail="Ismételt esemény az idempotencia-ablakon belül",
            )
            self._audit(outcome)
            return outcome
        try:
            outcome = await self._run(sys_id)
        except Exception:
            await self._release(sys_id)  # hiba esetén a következő esemény újra feldolgozhatja
            raise
        self._audit(outcome)
        return outcome

    async def process_safe(self, sys_id: str) -> None:
        """Háttérfeladathoz: a kivételt naplózza, nem dobja tovább."""
        try:
            await self.process(sys_id)
        except Exception:  # noqa: BLE001
            logger.exception("Az incidens feldolgozása sikertelen: %s", sys_id)

    # A folyamat
    async def _run(self, sys_id: str) -> ProcessingOutcome:
        incident = await self._sn.get_incident(sys_id)
        if incident.state in CLOSED_STATES:
            return ProcessingOutcome(
                sys_id=sys_id, number=incident.number, mode=self.mode, action="skipped",
                detail=f"Az incidens már lezárt állapotú (state={incident.state})",
            )

        # 1. Bemenet-védelem: tisztítás, injekciógyanú (maszkolás ELŐTT), PII-maszkolás.
        short = sanitize_text(incident.short_description, 300)
        desc = sanitize_text(incident.description, self._settings.max_input_chars)
        injection_signals = detect_injection(f"{short}\n{desc}")
        safe_incident = incident.model_copy(
            update={"short_description": mask_pii(short), "description": mask_pii(desc)}
        )

        # 2. KB-keresés - hiba esetén a folyamat KB nélkül megy tovább (így nem lesz lezárás).
        try:
            kb_articles = await self._sn.search_kb(extract_keywords(safe_incident.short_description))
        except ServiceNowError as exc:
            logger.warning("KB-keresés sikertelen (%s), folytatás KB nélkül", exc)
            kb_articles = []
        kb_articles = [
            a.model_copy(update={"text": mask_pii(sanitize_text(a.text, 1500))}) for a in kb_articles
        ]

        # 3. Triázs és 4. kapuk.
        triage = await self._triager.triage(safe_incident, kb_articles)
        decision = evaluate_auto_close(
            triage,
            incident_priority=incident.priority,
            kb_numbers={a.number for a in kb_articles},
            injection_suspected=bool(injection_signals),
            config=self._gate_config,
        )

        outcome = ProcessingOutcome(
            sys_id=sys_id, number=incident.number, mode=self.mode, action="logged",
            triage=triage, gate_failures=list(decision.failed), injection_signals=injection_signals,
        )
        if self.mode == "shadow":
            outcome.detail = "Shadow üzemmód: nincs visszaírás"
            return outcome

        # 5. Visszaírás (assist / autonomous).
        fields: dict[str, Any] = {"work_notes": self._work_note(triage, decision, injection_signals)}
        if not incident.assignment_group and triage.assignment_group:
            group_id = await self._sn.find_group_sys_id(triage.assignment_group)
            if group_id:
                fields["assignment_group"] = group_id

        if decision.allowed:  # a "mode" kapu miatt ez csak autonomous üzemmódban lehet igaz
            fields.update(
                state=self._settings.servicenow_resolved_state,
                close_code=self._settings.servicenow_close_code,
                close_notes=self._close_notes(triage),
            )
            outcome.action = "resolved"
            outcome.detail = f"Automatikusan megoldva ({triage.kb_article} alapján)"
        else:
            outcome.action = "routed"
            outcome.detail = (
                "Csoport beállítva" if "assignment_group" in fields else "Csak munkajegyzet"
            )

        await self._sn.update_incident(sys_id, fields)
        return outcome

    # Szövegek
    @staticmethod
    def _work_note(triage: TriageResult, decision: GateDecision, injection_signals: list[str]) -> str:
        lines = [
            "[AI-triázs]",
            f"Kategória: {triage.category} | Javasolt prioritás: {triage.priority} "
            f"| Megbízhatóság: {triage.confidence:.2f} | Forrás: {triage.source}",
            f"Javasolt csoport: {triage.assignment_group or '-'}",
            f"Indoklás: {triage.reasoning}",
        ]
        if triage.kb_article:
            lines.append(f"Hivatkozott KB-cikk: {triage.kb_article}")
        if injection_signals:
            lines.append(
                "FIGYELEM: a leírás utasításszerű szöveget tartalmaz "
                f"({', '.join(injection_signals)}); kézi ellenőrzés javasolt."
            )
        if decision.failed:
            lines.append(f"Automatikus lezárás nem történt (nem teljesült: {', '.join(decision.failed)}).")
        return "\n".join(lines)

    @staticmethod
    def _close_notes(triage: TriageResult) -> str:
        return (
            f"{(triage.resolution or '').strip()}\n\n"
            f"Részletek: {triage.kb_article}\n"
            "Ezt az incidenst az AI-triázs automatikusan oldotta meg. Ha a probléma nem szűnt meg, "
            "kérjük, nyissa újra a jegyet."
        )

    @staticmethod
    def _audit(outcome: ProcessingOutcome) -> None:
        # Ideiglenes audit: egy JSON-sor eseményenként. A 6. héttől SQLite (audit.py).
        audit_logger.info(json.dumps(outcome.model_dump(mode="json"), ensure_ascii=False))
