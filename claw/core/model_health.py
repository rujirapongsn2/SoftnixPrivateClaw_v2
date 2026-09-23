"""Low-cost daily checks for admin-global chat models.

Only a confirmed permanent routing/billing/auth failure can turn off the
administrator's switch. Uncertain failures require a second check before a
model is temporarily removed from chat; success restores it.
"""

import asyncio
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from loguru import logger
from sqlalchemy import or_, select, update

from claw.db.models import LLMModel, LLMProvider
from claw.db.stores import LLMConfigStore
from claw.providers.base import LLMProvider as ProviderProtocol, ProviderError

_CHECK_INTERVAL = timedelta(hours=23)
_CONFIRM_INTERVAL = timedelta(minutes=1)
_RETRY_INTERVAL = timedelta(minutes=5)
_LEASE = timedelta(minutes=3)
_POLL_SECONDS = 60
_PROBE_TIMEOUT = 30
_PERMANENT = {"auth_failed", "permission_denied", "quota_exhausted", "model_not_found"}
_PROBE_TOOLS = [{
    "type": "function",
    "function": {
        "name": "health_ping",
        "description": "Optional health-check function.",
        "parameters": {"type": "object", "properties": {}},
    },
}]


def _due_at(now: datetime):
    """Retry uncertain failures soon; retain the daily cadence for healthy models."""
    return or_(
        LLMModel.health_checked_at.is_(None),
        LLMModel.health_checked_at <= now - _CHECK_INTERVAL,
        (LLMModel.health_status == "warning") &
        (LLMModel.health_checked_at <= now - _CONFIRM_INTERVAL),
        (LLMModel.health_status == "quarantined") &
        (LLMModel.health_checked_at <= now - _RETRY_INTERVAL),
    )


def classify_error(exc: Exception) -> tuple[str, bool]:
    """Return a safe reason code; never expose an upstream error body or key."""
    if not isinstance(exc, ProviderError):
        return "connection_error", False
    code = (exc.error_type or "").lower()
    # Some OpenAI-compatible gateways expose only generic `rate_limit_error`
    # metadata and put the precise billing code in a JSON error body.
    match = re.search(
        r'''["'](?:code|type)["']\s*:\s*["'](insufficient_quota|credit_balance_exhausted|billing_hard_limit_reached|model_not_found|invalid_model)["']''',
        str(exc), re.IGNORECASE,
    )
    if match:
        code = match.group(1).lower()
    # Only explicit billing codes qualify. HTTP 429 alone is often a short burst.
    if any(marker in code for marker in ("insufficient_quota", "credit_balance", "billing_hard_limit")):
        return "quota_exhausted", True
    if code in {"invalid_api_key", "authentication_error", "authenticationerror"}:
        return "auth_failed", True
    if exc.status_code == 401:
        return "auth_failed", True
    if code in {"permission_denied", "access_denied"}:
        return "permission_denied", True
    if code in {"model_not_found", "invalid_model"}:
        return "model_not_found", True
    if exc.status_code == 403 and any(marker in code for marker in ("permission_denied", "access_denied")):
        return "permission_denied", True
    if exc.status_code == 404 and any(marker in code for marker in ("model_not_found", "invalid_model")):
        return "model_not_found", True
    if exc.status_code == 429:
        return "rate_limited", False
    if exc.status_code is not None and exc.status_code >= 500:
        return "service_unavailable", False
    return "probe_failed", False


class ModelHealthService:
    def __init__(self, store: LLMConfigStore, provider: ProviderProtocol):
        self.store = store
        self.provider = provider
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="model-health")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.check_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Model health scan failed")
            await asyncio.sleep(_POLL_SECONDS)

    async def check_due(self) -> int:
        """Check due global chat models sequentially; never probe private keys."""
        now = datetime.now(timezone.utc)
        async with self.store.factory() as db:
            rows = await db.execute(
                select(LLMModel.id)
                .join(LLMProvider, LLMProvider.id == LLMModel.provider_id)
                .where(
                    LLMProvider.owner_id.is_(None), LLMProvider.enabled.is_(True),
                    LLMModel.kind == "chat",
                    or_(LLMModel.enabled.is_(True), LLMModel.health_auto_disabled.is_(True)),
                    _due_at(now),
                    or_(LLMModel.health_claim_until.is_(None), LLMModel.health_claim_until <= now),
                )
                .order_by(LLMModel.health_checked_at, LLMModel.id)
                .limit(100)
            )
            ids = list(rows.scalars())
        checked = 0
        # No parallel fan-out: a provider with a low rate limit won't see a burst.
        for model_id in ids:
            if await self.check_model(model_id):
                checked += 1
            await asyncio.sleep(1)
        return checked

    async def check_model(self, model_id: str, *, force: bool = False) -> bool:
        """Claim a model once across workers, probe, and apply only if config is unchanged."""
        now = datetime.now(timezone.utc)
        lease_until = now + _LEASE
        async with self.store.factory() as db:
            claim = await db.execute(
                update(LLMModel)
                .where(
                    LLMModel.id == model_id,
                    or_(LLMModel.health_claim_until.is_(None), LLMModel.health_claim_until <= now),
                    _due_at(now) if not force else True,
                    or_(LLMModel.enabled.is_(True), LLMModel.health_auto_disabled.is_(True)),
                    LLMModel.kind == "chat",
                    LLMModel.provider_id.in_(select(LLMProvider.id).where(LLMProvider.owner_id.is_(None), LLMProvider.enabled.is_(True))),
                )
                .values(health_claim_until=lease_until)
            )
            if claim.rowcount != 1:
                await db.rollback()
                return False
            model = await db.get(LLMModel, model_id)
            provider = await db.get(LLMProvider, model.provider_id)
            snapshot = (model.model_id, provider.api_key, provider.api_base)
            previous_status = model.health_status
            model_name = model.model_id
            try:
                key = self.store._dec(provider.api_key)
                key_error = False
            except Exception:
                key, key_error = "", True
            base = provider.api_base or None
            await db.commit()

        status, reason, permanent = "healthy", "", False
        try:
            parsed = urlsplit(base) if base else None
            invalid_base = parsed is not None and (parsed.scheme not in {"http", "https"} or not parsed.netloc)
        except ValueError:
            invalid_base = True
        if key_error or invalid_base:
            status, reason = "warning", "config_error"
        else:
            try:
                # Chat turns send function definitions. Verify that route too,
                # while keeping the probe's output and token use tiny.
                await asyncio.wait_for(
                    self.provider.chat(
                        [{"role": "user", "content": "Reply OK."}],
                        tools=_PROBE_TOOLS, model=model_name,
                        max_tokens=8, temperature=0,
                        api_key=key or None, api_base=base,
                    ),
                    timeout=_PROBE_TIMEOUT,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason, permanent = classify_error(exc)
                status = "unavailable" if permanent else "warning"

        finished = datetime.now(timezone.utc)
        async with self.store.factory() as db:
            model = await db.get(LLMModel, model_id)
            provider = await db.get(LLMProvider, model.provider_id) if model else None
            claimed = model.health_claim_until if model else None
            if claimed is not None and claimed.tzinfo is None:
                claimed = claimed.replace(tzinfo=timezone.utc)
            if not model or not provider or claimed != lease_until:
                return False
            # Admin edits during a probe must win. In particular, turning off
            # auto-disable while an HTTP request is in flight cannot disable it.
            if not provider.enabled or (model.model_id, provider.api_key, provider.api_base) != snapshot:
                model.health_claim_until = None
                await db.commit()
                return False
            model.health_status = status
            if status == "warning" and previous_status in {"warning", "quarantined"}:
                model.health_status = "quarantined"
            model.health_reason = reason
            model.health_checked_at = finished
            model.health_claim_until = None
            if permanent and provider.auto_disable_models and model.enabled:
                model.enabled = False
                model.health_auto_disabled = True
            await db.commit()
        logger.info("Model health check: model_id={} status={} reason={}", model_id, status, reason)
        return True
