"""FastAPI alkalmazás. TODO: /audit/recent, /audit/stats, /metrics"""

from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from ai_triage import build_triager
from automation import AutomationService
from config import Settings, get_settings
from models import ProcessingOutcome
from security import verify_hmac_signature, verify_shared_secret
from servicenow import ServiceNowClient, ServiceNowError, validate_sys_id

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    sn = ServiceNowClient(
        settings.servicenow_instance_url,
        settings.servicenow_user,
        settings.servicenow_password.get_secret_value(),
        timeout_s=settings.servicenow_timeout_s,
        max_retries=settings.servicenow_max_retries,
    )
    app.state.service = AutomationService(sn, build_triager(settings), settings)
    if not settings.webhook_secret.get_secret_value():
        logger.warning("WEBHOOK_SECRET nincs beállítva: minden webhook-kérés elutasításra kerül")
    logger.info("Indulás: üzemmód=%s, LLM=%s", settings.autonomy_mode, settings.llm_enabled)
    try:
        yield
    finally:
        await sn.aclose()


app = FastAPI(title="AI-vezérelt IT folyamatautomatizáció", version="0.4.0", lifespan=lifespan)


# Függőségek
def get_service(request: Request) -> AutomationService:
    return request.app.state.service


def require_admin(
    x_admin_token: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    expected = settings.admin_token.get_secret_value()
    if not expected or not x_admin_token or not hmac.compare_digest(expected.encode(), x_admin_token.encode()):
        raise HTTPException(status_code=401, detail="Érvénytelen vagy hiányzó admin token")


class WebhookPayload(BaseModel):
    sys_id: str = Field(pattern=r"^[0-9a-f]{32}$")


# Végpontok
@app.post("/webhook/servicenow", status_code=202)
async def servicenow_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings),
    service: AutomationService = Depends(get_service),
    x_timestamp: str | None = Header(default=None),
    x_signature: str | None = Header(default=None),
    x_webhook_secret: str | None = Header(default=None),
) -> dict[str, str]:
    body = await request.body()
    if len(body) > settings.max_webhook_body_bytes:
        raise HTTPException(status_code=413, detail="Túl nagy kérés")

    secret = settings.webhook_secret.get_secret_value()
    if x_signature is not None:
        authenticated = verify_hmac_signature(
            secret, x_timestamp, body, x_signature, settings.webhook_max_skew_s
        )
    elif settings.allow_shared_secret_webhook:
        authenticated = verify_shared_secret(secret, x_webhook_secret)
    else:
        authenticated = False
    if not authenticated:
        raise HTTPException(status_code=401, detail="Hitelesítés sikertelen")

    try:
        payload = WebhookPayload.model_validate_json(body)
    except ValidationError:
        raise HTTPException(status_code=422, detail="Érvénytelen payload") from None

    background_tasks.add_task(service.process_safe, payload.sys_id)
    return {"status": "accepted", "sys_id": payload.sys_id}


@app.post(
    "/incidents/{sys_id}/triage",
    response_model=ProcessingOutcome,
    dependencies=[Depends(require_admin)],
)
async def manual_triage(
    sys_id: str, service: AutomationService = Depends(get_service)
) -> ProcessingOutcome:
    try:
        validate_sys_id(sys_id)
        return await service.process(sys_id, force=True)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except ServiceNowError as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail="Az incidens nem található") from None
        logger.error("ServiceNow-hiba kézi triázs közben: %s", exc)
        raise HTTPException(status_code=502, detail="ServiceNow-hiba") from None


@app.get("/health")
async def health(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    return {
        "status": "ok",
        "autonomy_mode": settings.autonomy_mode,
        "triager": "claude" if settings.llm_enabled else "rules",
    }
