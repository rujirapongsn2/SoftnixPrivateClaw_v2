"""Authenticated owner workspace files, independent of conversation existence."""
import asyncio

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from loguru import logger
from pydantic import BaseModel

from claw.workspace.browse import KINDS, OTHER, ListingCache, browse, kind_of
from claw.workspace.lifecycle import FileConflict, FileLifecycle, safe_path
from claw.workspace.policy import WorkspacePolicyStore


# Upper bound on one walk of a workspace; the view says when it was reached.
WALK_LIMIT = 20_000
_listings = ListingCache()


class FileRequest(BaseModel):
    path: str


class PurgeRequest(BaseModel):
    confirm_permanent: bool = False
    expected_path: str


def _policy(state):
    store = getattr(state, "workspace_policy", None) or WorkspacePolicyStore(state.sessions.factory, state.settings)
    return store.effective()


def create_files_router(get_state, current_user, mode):
    router = APIRouter(prefix="/api/files", tags=["files"])

    async def operation(fn, owner: str):
        try:
            return await fn()
        except (FileNotFoundError, ValueError):
            raise HTTPException(404, "File not found") from None
        except FileConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except OSError:
            logger.exception("File operation failed")
            raise HTTPException(503, "The file could not be changed right now. Try again later.") from None
        finally:
            _listings.forget((mode, owner))

    @router.get("")
    async def list_files(
        q: str = Query("", max_length=200),
        kind: str = "",
        sort: Literal["recent", "name", "size"] = "recent",
        offset: int = Query(0, ge=0),
        limit: int = Query(40, ge=1, le=200),
        since: float | None = Query(None, ge=0),
        zone: Literal["", "ai", "user", "scratch"] = "",
        refresh: bool = False,
        user=Depends(current_user),
        state=Depends(get_state),
    ):
        if kind and kind != OTHER and kind not in KINDS:
            raise HTTPException(422, "Unknown file kind")
        policy = await _policy(state)

        def walk():
            listing = FileLifecycle(state, mode).list_files(user.id, policy, limit=WALK_LIMIT)
            for entry in listing["files"]:
                entry["kind"] = kind_of(entry["path"])
            return listing

        def page():
            listing = _listings.get((mode, user.id), walk, fresh=refresh)
            return {
                **browse(listing["files"], q=q, kind=kind, sort=sort, offset=offset, limit=limit, since=since, zone=zone),
                "truncated": listing["truncated"],
                "walk_limit": WALK_LIMIT,
            }

        return await asyncio.to_thread(page)

    @router.get("/download/{path:path}")
    async def download(path: str, user=Depends(current_user), state=Depends(get_state)):
        from claw.api.routes import _workspace_file_headers

        try:
            source = safe_path(FileLifecycle(state, mode).workspace(user.id), path)
        except FileNotFoundError:
            raise HTTPException(404, "File not found") from None
        return FileResponse(source, headers=_workspace_file_headers(str(source)))

    @router.get("/trash")
    async def list_trash(
        limit: int = Query(200, ge=1, le=1000), user=Depends(current_user), state=Depends(get_state)
    ):
        policy = await _policy(state)
        listing = await asyncio.to_thread(
            lambda: FileLifecycle(state, mode).list_trash(
                user.id, retention_days=policy.trash_retention_days, limit=limit
            )
        )
        return {
            **listing,
            "retention_days": policy.trash_retention_days,
            "cap_mb": policy.trash_cap_mb,
            "permanent_delete_enabled": policy.permanent_delete_enabled,
        }

    @router.post("/trash")
    async def trash(body: FileRequest, user=Depends(current_user), state=Depends(get_state)):
        return await operation(lambda: FileLifecycle(state, mode).trash(user.id, body.path, actor=user.id), user.id)

    @router.post("/trash/{ident}/restore")
    async def restore(ident: str, user=Depends(current_user), state=Depends(get_state)):
        return await operation(lambda: FileLifecycle(state, mode).restore(user.id, ident), user.id)

    async def purge(ident: str, body: PurgeRequest, user=Depends(current_user), state=Depends(get_state)):
        if not (await _policy(state)).permanent_delete_enabled:
            raise HTTPException(403, "Permanent deletion is disabled by the administrator.")
        return await operation(lambda: FileLifecycle(state, mode).purge(
            user.id, ident, confirmed=body.confirm_permanent, expected_path=body.expected_path), user.id)

    router.add_api_route("/trash/{ident}/purge", purge, methods=["POST"])
    router.add_api_route("/trash/{ident}", purge, methods=["DELETE"])
    return router
