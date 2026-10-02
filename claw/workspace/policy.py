"""The workspace storage policy an administrator can change at run time.

Stored as one `app_settings` row (no migration) and read through a short cache,
so a change made on one worker reaches the others within `ttl` seconds without
a restart. The environment (`CLAW_WORKSPACE__*`) supplies the defaults used
until an administrator saves an override.
"""

import time
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field

from claw.db.models import AppSetting

KEY = "workspace_policy"
_AUDIT_KEPT = 50


class WorkspacePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quota_mb: int = Field(default=2048, ge=0, le=100_000_000)
    quota_files: int = Field(default=50_000, ge=0, le=1_000_000_000)
    tmp_retention_days: int = Field(default=7, ge=0, le=3650)
    uploads_retention_days: int = Field(default=7, ge=0, le=3650)
    cleanup_enabled: bool = True
    # False = observe only (see WorkspaceSettings.enforce).
    enforce: bool = False

    @property
    def quota_bytes(self) -> int:
        return self.quota_mb * 1024 * 1024


class WorkspacePolicyStore:
    def __init__(self, factory, settings, ttl: float = 30.0):
        self.factory = factory
        self.settings = settings
        self.ttl = ttl
        self._cached: tuple[float, WorkspacePolicy] | None = None

    def defaults(self) -> WorkspacePolicy:
        s = self.settings.workspace
        return WorkspacePolicy(
            quota_mb=s.quota_mb,
            quota_files=s.quota_files,
            tmp_retention_days=s.tmp_retention_days,
            uploads_retention_days=s.uploads_retention_days,
            cleanup_enabled=s.cleanup_enabled,
            enforce=s.enforce,
        )

    def invalidate(self) -> None:
        self._cached = None

    async def effective(self) -> WorkspacePolicy:
        now = time.monotonic()
        if self._cached is not None and now - self._cached[0] < self.ttl:
            return self._cached[1]
        async with self.factory() as db:
            row = await db.get(AppSetting, KEY)
        policy = self.defaults()
        if row is not None and isinstance((row.value or {}).get("policy"), dict):
            try:
                policy = WorkspacePolicy.model_validate(row.value["policy"])
            except ValueError:
                # A bad stored value must not take storage limits down with it.
                policy = self.defaults()
        self._cached = (now, policy)
        return policy

    async def save(self, policy: WorkspacePolicy, actor: str) -> WorkspacePolicy:
        async with self.factory() as db:
            row = await db.get(AppSetting, KEY)
            old = dict(row.value or {}) if row else {}
            event = {
                "actor": actor,
                "at": datetime.now(timezone.utc).isoformat(),
                "previous": old.get("policy"),
                "policy": policy.model_dump(),
            }
            value = {"policy": policy.model_dump(), "audit": [*old.get("audit", []), event][-_AUDIT_KEPT:]}
            if row:
                row.value = value
            else:
                db.add(AppSetting(key=KEY, value=value))
            await db.commit()
        self.invalidate()
        return policy
