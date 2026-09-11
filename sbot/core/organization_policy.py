"""Persisted administrator resource policy, shared by integrated and Bot Mode."""

from datetime import datetime, timezone
from pydantic import BaseModel, Field, PositiveInt, ConfigDict
from sbot.db.models import AppSetting

KEY = "team_resource_policy"


class OrganizationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    automatic_resources: bool = True
    max_job_tokens: int = Field(default=5_000_000, ge=1000)
    max_job_seconds: int = Field(default=21600, ge=60)
    max_resource_adjustments: int = Field(default=4, ge=0, le=20)
    resource_headroom: float = Field(default=1.5, ge=1, le=3)
    max_step_recoveries: int = Field(default=2, ge=0, le=5)
    model_output_limits: dict[str, PositiveInt] = Field(default_factory=dict)


async def load_policy(factory, settings):
    async with factory() as db:
        row = await db.get(AppSetting, KEY)
        values = (
            row.value["policy"]
            if row
            else {
                **{
                    k: getattr(settings.team_work, k)
                    for k in OrganizationPolicy.model_fields
                    if k != "model_output_limits"
                },
                "model_output_limits": settings.llm.model_output_limits,
            }
        )
    policy = OrganizationPolicy.model_validate(values)
    for key, value in policy.model_dump().items():
        if key == "model_output_limits":
            settings.llm.model_output_limits = value
        else:
            setattr(settings.team_work, key, value)
    return policy


async def save_policy(factory, settings, policy, actor):
    async with factory() as db:
        row = await db.get(AppSetting, KEY)
        old = row.value if row else {}
        event = {
            "actor": actor,
            "at": datetime.now(timezone.utc).isoformat(),
            "previous": old.get("policy"),
            "policy": policy.model_dump(),
        }
        value = {"policy": policy.model_dump(), "audit": [*old.get("audit", []), event][-100:]}
        if row:
            row.value = value
        else:
            db.add(AppSetting(key=KEY, value=value))
        await db.commit()
    return await load_policy(factory, settings)
