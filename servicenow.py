"""ServiceNow Table API kliens (incident, kb_knowledge, sys_user_group).

Szerepkörök a szolgáltatásfióknak: itil + knowledge.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

from models import Incident, KBArticle

logger = logging.getLogger(__name__)

SYS_ID_RE = re.compile(r"^[0-9a-f]{32}$")
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
INCIDENT_FIELDS = (
    "sys_id,number,short_description,description,priority,state,category,"
    "assignment_group,sys_updated_on"
)
_QUERY_UNSAFE_RE = re.compile(r"[^\w\-]", re.UNICODE)


class ServiceNowError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def validate_sys_id(sys_id: str) -> str:
    if not isinstance(sys_id, str) or not SYS_ID_RE.fullmatch(sys_id):
        raise ValueError("Érvénytelen sys_id (32 hexadecimális karakter szükséges)")
    return sys_id


class ServiceNowClient:
    def __init__(
        self,
        instance_url: str,
        user: str,
        password: str,
        *,
        timeout_s: float = 15.0,
        max_retries: int = 3,
        backoff_base_s: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._max_retries = max_retries
        self._backoff_base_s = backoff_base_s
        self._group_cache: dict[str, str | None] = {}
        self._client = httpx.AsyncClient(
            base_url=instance_url.rstrip("/"),
            auth=httpx.BasicAuth(user, password),
            timeout=timeout_s,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Kérés exponenciális visszalépéssel. Csak idempotens műveleteket (GET, PATCH) küldünk,
        ezért az újrapróbálkozás nem okoz kettős írást."""
        last_error = "ismeretlen hiba"
        for attempt in range(self._max_retries + 1):
            delay = self._backoff_base_s * (2**attempt)
            try:
                resp = await self._client.request(method, path, params=params, json=json)
            except httpx.TransportError as exc:
                last_error = f"hálózati hiba ({type(exc).__name__})"
            else:
                if resp.status_code in RETRYABLE_STATUS:
                    last_error = f"HTTP {resp.status_code}"
                    retry_after = resp.headers.get("Retry-After", "")
                    if retry_after.isdigit():
                        delay = min(float(retry_after), 30.0)
                elif resp.is_error:
                    raise ServiceNowError(f"{method} {path}: HTTP {resp.status_code}", resp.status_code)
                else:
                    return resp.json()
            if attempt < self._max_retries:
                logger.warning(
                    "ServiceNow %s %s sikertelen (%s), újrapróbálkozás %.1f s múlva",
                    method, path, last_error, delay,
                )
                await asyncio.sleep(delay)
        raise ServiceNowError(
            f"{method} {path}: {last_error} ({self._max_retries + 1} próbálkozás után)"
        )

    # Incidensek
    async def get_incident(self, sys_id: str) -> Incident:
        validate_sys_id(sys_id)
        data = await self._request(
            "GET",
            f"/api/now/table/incident/{sys_id}",
            params={"sysparm_fields": INCIDENT_FIELDS, "sysparm_exclude_reference_link": "true"},
        )
        return Incident.from_servicenow(data.get("result", {}))

    async def update_incident(self, sys_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        validate_sys_id(sys_id)
        data = await self._request(
            "PATCH",
            f"/api/now/table/incident/{sys_id}",
            params={"sysparm_exclude_reference_link": "true", "sysparm_fields": "sys_id,number,state"},
            json=fields,
        )
        return data.get("result", {})

    # Tudásbázis
    async def search_kb(self, terms: list[str], limit: int = 3) -> list[KBArticle]:
        """Publikált KB-cikkek keresése ServiceNow szövegkereséssel (123TEXTQUERY321).

        A keresőszavakból minden olyan karaktert eltávolítunk, amely az encoded query
        szintaxisát megtörhetné (pl. ``^``)."""
        clean = [t for t in (_QUERY_UNSAFE_RE.sub("", term) for term in terms) if len(t) >= 3][:5]
        if not clean:
            return []
        data = await self._request(
            "GET",
            "/api/now/table/kb_knowledge",
            params={
                "sysparm_query": "workflow_state=published^123TEXTQUERY321=" + " ".join(clean),
                "sysparm_fields": "sys_id,number,short_description,text",
                "sysparm_limit": str(limit),
            },
        )
        return [KBArticle.from_servicenow(r) for r in data.get("result", [])]

    # Csoportok
    async def find_group_sys_id(self, name: str) -> str | None:
        """Hozzárendelési csoport sys_id-ja név alapján (gyorsítótárazva)."""
        if not name or "^" in name:
            return None
        if name in self._group_cache:
            return self._group_cache[name]
        data = await self._request(
            "GET",
            "/api/now/table/sys_user_group",
            params={"sysparm_query": f"name={name}^active=true", "sysparm_fields": "sys_id", "sysparm_limit": "1"},
        )
        results = data.get("result", [])
        sys_id = results[0].get("sys_id") if results else None
        if sys_id is None:
            logger.warning("A(z) %r hozzárendelési csoport nem található a ServiceNow-ban", name)
        self._group_cache[name] = sys_id
        return sys_id
