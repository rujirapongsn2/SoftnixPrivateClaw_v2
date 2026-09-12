"""Hash-verified local delivery; retry checks existing bytes before writing."""

import asyncio
import base64
import hashlib
import logging
from pathlib import Path, PurePosixPath

from sbot.local_workspaces import MAX_BYTES, OfflineError

logger = logging.getLogger(__name__)


def resolve_artifact(workspace: Path, raw: str) -> Path:
    """Resolve a cloud-workspace-relative artifact, refusing to escape the root."""
    root = Path(workspace).resolve()
    resolved = (root / raw).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"path escapes the workspace: {raw}")
    return resolved


def read_capped(source: Path) -> bytes:
    """Read at most one byte past the limit, so an oversized file cannot be loaded."""
    with source.open("rb") as handle:
        return handle.read(MAX_BYTES + 1)


class LocalDelivery:
    def __init__(self, broker, owner, target: dict, guard=None, root: Path | None = None):
        self.broker, self.owner, self.target, self.guard = broker, owner, target, guard
        self.root = root

    def queue_path(self, workspace, artifact: str) -> str:
        """Owner-root-relative path, which is what the background worker resolves.

        A mission or isolated assignment runs in a scoped subdirectory and
        ``rebase_result`` only rewrites its paths after the turn ends, so a path
        captured here would otherwise be unresolvable at delivery time.
        """
        source = resolve_artifact(workspace, artifact)
        root = Path(self.root).resolve() if self.root else Path(workspace).resolve()
        return source.relative_to(root).as_posix()

    async def send(self, artifacts: list[dict], workspace) -> tuple[dict, list[dict]]:
        stages = {"created": "completed", "published": "completed", "local": "pending"}
        receipts = []
        if self.broker is None:
            return {**stages, "local": "not_connected"}, receipts
        wid = self.target.get("workspace_id")
        mapping = self.target.get("paths") or {}
        if not isinstance(wid, str) or not wid or not isinstance(mapping, dict):
            return {**stages, "local": "invalid_target"}, receipts
        queued = False
        for check in artifacts:
            destination = mapping.get(check["path"], PurePosixPath(check["path"]).name)
            path = PurePosixPath(destination)
            if path.is_absolute() or ".." in path.parts or not path.name:
                return {**stages, "local": "invalid_path"}, receipts
            args = {
                "workspace_id": wid,
                "action": "export",
                "path": destination,
                "cloud_path": check["artifact"],
            }
            if self.guard:
                guarded, error = self.guard("local_workspace", args)
                if error or guarded != args:
                    return {**stages, "local": "blocked"}, receipts
            try:
                self.broker.owned(self.owner, wid)
                # A response lost after a successful write must not create a
                # second file. Existing identical content is already delivered.
                found = await self.broker.call(self.owner, wid, {"action": "read", "path": destination})
                if found.get("sha256") == check["sha256"]:
                    receipts.append(
                        {
                            "kind": "local_delivery",
                            "workspace_id": wid,
                            "path": destination,
                            "sha256": check["sha256"],
                            "status": "passed",
                        }
                    )
                    continue
                if "data" in found:
                    return {**stages, "local": "conflict"}, receipts
                data = read_capped(resolve_artifact(workspace, check["artifact"]))
                if len(data) > MAX_BYTES or hashlib.sha256(data).hexdigest() != check["sha256"]:
                    return {**stages, "local": "invalid_artifact"}, receipts
                result = await self.broker.call(
                    self.owner,
                    wid,
                    {"action": "write", "path": destination, "data": base64.b64encode(data).decode()},
                )
                if result.get("written") != destination or result.get("sha256") != check["sha256"]:
                    return {**stages, "local": "not_confirmed"}, receipts
                receipts.append(
                    {
                        "kind": "local_delivery",
                        "workspace_id": wid,
                        "path": destination,
                        "sha256": check["sha256"],
                        "status": "passed",
                    }
                )
            except OfflineError:
                # The work is finished and the bytes are safe in the cloud
                # workspace; hand it to the queue instead of losing it.
                try:
                    self.broker.enqueue(self.owner, wid, self.queue_path(workspace, check["artifact"]),
                                        check["sha256"], destination)
                except (ValueError, OSError):
                    return {**stages, "local": "not_connected"}, receipts
                queued = True
            except (ValueError, OSError, KeyError):
                return {**stages, "local": "not_confirmed"}, receipts
        return {**stages, "local": "queued" if queued else "completed"}, receipts


class LocalDeliveryWorker:
    """Ships files queued while a paired folder was offline, once it reconnects.

    Authorization is re-checked at send time, not at enqueue time: a folder the
    user disconnected, downgraded to read-only, or a guardrail that changed in
    the meantime must stop the delivery.
    """

    def __init__(self, broker, workspaces_root: Path, guard_for_owner=None, interval: float = 5.0, batch: int = 5):
        self.broker, self.workspaces_root = broker, Path(workspaces_root)
        self.guard_for_owner, self.interval, self.batch = guard_for_owner, interval, batch
        self._task = None

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self.run())

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("local delivery worker tick failed")
            await asyncio.sleep(self.interval)

    async def tick(self):
        for _ in range(self.batch):
            row = self.broker.claim_delivery()
            if row is None:
                return
            try:
                await self.deliver(row)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A claimed row that is never finished stays 'running', where it
                # can be neither retried nor cancelled until the stale sweep.
                logger.exception("queued delivery failed unexpectedly")
                self.broker.finish_delivery(row["id"], False, "Unexpected error while delivering")

    def _guarded(self, row) -> bool:
        if not self.guard_for_owner:
            return True
        args = {"workspace_id": row["workspace"], "action": "export", "path": row["dest"], "cloud_path": row["cloud_path"]}
        try:
            guard = self.guard_for_owner(row["owner"])
            checked, error = guard("local_workspace", args)
        except Exception:
            logger.exception("could not evaluate guardrails for queued delivery")
            return False
        return not error and checked == args

    async def deliver(self, row):
        did, owner, wid, dest = row["id"], row["owner"], row["workspace"], row["dest"]
        if not self._guarded(row):
            self.broker.finish_delivery(did, False, "Blocked by guardrails", final=True)
            return
        try:
            source = resolve_artifact(self.workspaces_root / owner, row["cloud_path"])
            data = await asyncio.to_thread(read_capped, source)
        except (ValueError, OSError):
            self.broker.finish_delivery(did, False, "The cloud file was removed before it could be delivered", final=True)
            return
        if len(data) > MAX_BYTES or hashlib.sha256(data).hexdigest() != row["sha256"]:
            self.broker.finish_delivery(did, False, "The cloud file changed after it was queued; nothing was written", final=True)
            return
        try:
            # Re-check ownership and permission now, then compare bytes before
            # writing so a replayed delivery cannot duplicate or clobber a file.
            if not self.broker.owned(owner, wid)["writable"]:
                self.broker.finish_delivery(did, False, "The folder is now read-only", final=True)
                return
            found = await self.broker.call(owner, wid, {"action": "read", "path": dest})
            if found.get("sha256") == row["sha256"]:
                self.broker.finish_delivery(did, True)
                return
            if "data" in found:
                self.broker.finish_delivery(did, False, "A different file already uses that name locally", final=True)
                return
            result = await self.broker.call(owner, wid, {"action": "write", "path": dest, "data": base64.b64encode(data).decode()})
            if result.get("written") == dest and result.get("sha256") == row["sha256"]:
                self.broker.finish_delivery(did, True)
            else:
                self.broker.finish_delivery(did, False, str(result.get("error") or "The folder did not confirm the write"))
        except OfflineError:
            self.broker.finish_delivery(did, False, "Waiting for the folder to reconnect", transient=True)
        except (ValueError, OSError, KeyError) as exc:
            self.broker.finish_delivery(did, False, str(exc))
