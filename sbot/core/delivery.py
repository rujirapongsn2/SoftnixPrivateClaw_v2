"""Hash-verified local delivery; retry checks existing bytes before writing."""

import base64
import hashlib
from pathlib import PurePosixPath

from sbot.local_workspaces import MAX_BYTES


class LocalDelivery:
    def __init__(self, broker, owner, target: dict, guard=None):
        self.broker, self.owner, self.target, self.guard = broker, owner, target, guard

    async def send(self, artifacts: list[dict], workspace) -> tuple[dict, list[dict]]:
        stages = {"created": "completed", "published": "completed", "local": "pending"}
        receipts = []
        if self.broker is None:
            return {**stages, "local": "not_connected"}, receipts
        wid = self.target.get("workspace_id")
        mapping = self.target.get("paths") or {}
        if not isinstance(wid, str) or not wid or not isinstance(mapping, dict):
            return {**stages, "local": "invalid_target"}, receipts
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
                data = (workspace / check["artifact"]).read_bytes()
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
            except (ValueError, OSError, KeyError):
                return {**stages, "local": "not_confirmed"}, receipts
        return {**stages, "local": "completed"}, receipts
