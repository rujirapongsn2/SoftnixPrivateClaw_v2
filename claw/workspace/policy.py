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
    ai_retention_days: int = Field(default=30, ge=0, le=3650)
    trash_retention_days: int = Field(default=30, ge=1, le=3650)
    trash_cap_mb: int = Field(default=1024, ge=0, le=100_000_000)
    permanent_delete_enabled: bool = True
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
            ai_retention_days=s.ai_retention_days,
            trash_retention_days=s.trash_retention_days,
            trash_cap_mb=s.trash_cap_mb,
            permanent_delete_enabled=s.permanent_delete_enabled,
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
            # A saved row predates any field added since; those take the
            # environment default, and fields since removed are dropped.
            stored = {k: v for k, v in row.value["policy"].items() if k in WorkspacePolicy.model_fields}
            try:
                policy = WorkspacePolicy.model_validate({**policy.model_dump(), **stored})
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
