"""Admin Skill Security: the review table, one skill's report, decisions and the share policy.

Owners read their own reports through /api/skills/{id}/assessment (claw/api/manage.py), which uses
`report_json` from here so both views agree.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from claw.api.deps import AppState, get_state, require_admin
from claw.db.models import User

router = APIRouter(prefix="/api/admin/skill-security")


def verdict_json(security) -> dict:
    from claw.skills.security_policy import widest_visibility

    return {
        "scope": security.scope,
        "reason": security.reason,
        "max_severity": security.max_severity,
        "ast_codes": list(security.ast_codes),
        "widest_visibility": widest_visibility(security.scope),
    }


def report_json(report: dict, *, show_decision: bool) -> dict:
    """A skill's review as the owner and admins see it. `show_decision` adds the request or decision."""
    from claw.skills.assessment import PLATFORM_STATUS, SCANNER_VERSION

    row, decision = report["assessment"], report["decision"]
    out: dict = {
        "id": report["id"],
        "name": report["name"],
        "visibility": report["visibility"],
        "source": report["source"],
        "platform": PLATFORM_STATUS,
        "policy": verdict_json(report["verdict"]),
        "status": "pending",
        # The last review gave up after its retries (this process); a rescan or a save tries again.
        "review_failed": report["review_failed"],
    }
    if row is not None:
        # The report only carries a review of the current content; only a newer scanner makes it outdated.
        out.update(
            status="current" if row.scanner_version == SCANNER_VERSION else "outdated",
            max_severity=row.max_severity,
            sha256=row.content_sha256,
            scanner_version=row.scanner_version,
            checked_at=row.checked_at.isoformat(),
            findings=row.findings,
        )
    if show_decision and decision is not None:
        out["decision"] = {
            "decision": decision.decision,
            # allow and requested only hold for the content they were made for
            "current": decision.decision == "block" or (row is not None and decision.content_sha256 == row.content_sha256),
            "scope": decision.scope,
            "request_reason": decision.request_reason,
            "admin_note": decision.admin_note,
            "updated_at": decision.updated_at.isoformat(),
        }
    return out


@router.get("/summary")
async def summary(_: User = Depends(require_admin), state: AppState = Depends(get_state)) -> dict:
    return await state.skills.security_summary()


@router.get("/skills")
async def list_skills(
    severity: str = "",
    ast: str = Query("", pattern=r"^(AST(0[1-9]|10))?$"),
    visibility: Literal["", "private", "group", "public"] = "",
    status: Literal["", "requested", "allow", "reject", "block"] = "",
    q: str = Query("", max_length=100),
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
    _: User = Depends(require_admin),
    state: AppState = Depends(get_state),
) -> dict:
    severities = tuple(x for x in severity.split(",") if x in {"pending", "none", "low", "medium", "high", "critical"})
    rows, total = await state.skills.security_list(
        severities=severities, ast=ast, visibility=visibility, status=status, q=q.strip(), offset=offset, limit=limit
    )
    owners = await state.users.display_names(list({r["owner_id"] for r in rows}))
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [
            {
                "id": r["id"],
                "name": r["name"],
                "owner_name": owners.get(r["owner_id"], ""),
                "visibility": r["visibility"],
                "enabled": r["enabled"],
                "subscribers": r["subscribers"],
                "checked_at": r["checked_at"].isoformat() if r["checked_at"] else None,
                "review_failed": r["review_failed"],
                "decision": r["decision"],
                "policy": verdict_json(r["verdict"]),
            }
            for r in rows
        ],
    }


@router.get("/skills/{skill_id}")
async def skill_report(skill_id: str, _: User = Depends(require_admin), state: AppState = Depends(get_state)) -> dict:
    report = await state.skills.security_report(skill_id)
    if report is None:
        raise HTTPException(status_code=404, detail="skill not found")
    owners = await state.users.display_names([report["owner_id"]])
    return {**report_json(report, show_decision=True), "owner_name": owners.get(report["owner_id"], "")}


class DecisionBody(BaseModel):
    decision: Literal["allow", "reject", "block", "clear"]
    scope: Literal["public", "group", "owner"] | None = None
    note: str = Field("", max_length=2000)
    sha256: str = Field("", max_length=64)  # the review the admin read; required for allow


_AUDIT_KIND = {"allow": "skill_sec.exception", "reject": "skill_sec.reject", "block": "skill_sec.block", "clear": "skill_sec.clear"}


@router.post("/skills/{skill_id}/decision")
async def decide(
    skill_id: str, body: DecisionBody, admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    if body.decision in ("allow", "block") and not body.note.strip():
        raise HTTPException(status_code=400, detail="Give a reason for this decision")
    if body.decision == "allow" and not body.sha256:
        raise HTTPException(status_code=400, detail="sha256 of the reviewed version is required")
    try:
        row = await state.skills.decide_security(skill_id, body.decision, admin.id, body.scope, body.note, body.sha256)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    report = await state.skills.security_report(skill_id)
    if report is None:
        raise HTTPException(status_code=404, detail="skill not found")
    await state.audit.log(
        _AUDIT_KIND[body.decision],
        {"skill_id": skill_id, "skill": report["name"], "scope": body.scope if body.decision == "allow" else None,
         "sha256": row.content_sha256 if row is not None else None, "note": body.note.strip()[:500]},
        user_id=admin.id,
    )
    return report_json(report, show_decision=True)


@router.post("/skills/{skill_id}/rescan")
async def rescan(skill_id: str, _: User = Depends(require_admin), state: AppState = Depends(get_state)) -> dict:
    """Review the skill again even if this scanner already reviewed this content. A long review keeps
    running after the response, which then shows the previous result."""
    await state.skills.rescan(skill_id)
    report = await state.skills.security_report(skill_id)
    if report is None:
        raise HTTPException(status_code=404, detail="skill not found")
    return report_json(report, show_decision=True)


class PolicyBody(BaseModel):
    policy: dict[str, str]


@router.get("/policy")
async def get_policy(_: User = Depends(require_admin), state: AppState = Depends(get_state)) -> dict:
    from claw.skills.security_policy import DEFAULT_POLICY, LEVELS, SCOPES

    return {"policy": await state.skills.security_policy(), "defaults": DEFAULT_POLICY, "levels": LEVELS, "scopes": SCOPES}


@router.post("/policy/preview")
async def preview_policy(body: PolicyBody, _: User = Depends(require_admin), state: AppState = Depends(get_state)) -> dict:
    try:
        return await state.skills.policy_impact(body.policy)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/policy")
async def set_policy(body: PolicyBody, admin: User = Depends(require_admin), state: AppState = Depends(get_state)) -> dict:
    before = await state.skills.security_policy()
    try:
        impact = await state.skills.policy_impact(body.policy)
        after = await state.skills.set_security_policy(body.policy)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await state.audit.log("skill_sec.policy", {"before": before, "after": after, "impact": impact}, user_id=admin.id)
    return {"policy": after, "impact": impact}
