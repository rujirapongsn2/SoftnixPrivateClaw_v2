"""File-backed broker for client-side browser-extension automation.

Ported from the reference project (softnix-agenticclaw). The agent enqueues a
task; a paired Chrome extension polls for it over HTTP, runs it against the
user's real browser tab, and posts the result back. State is file-backed and
lives under one root (``workspaces_root/_browser_broker``); every pairing,
extension, and task carries a ``user_id`` so multiple users stay isolated even
though they share a single store.

Design note: the extension is a Manifest-V3 service worker, which cannot hold a
persistent WebSocket, so the transport is short HTTP polling rather than a live
socket — the broker is the durable queue that bridges the two sides.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Any

# Keys whose values are scrubbed before anything is written to disk or returned.
SENSITIVE_KEYS = {"password", "passcode", "otp", "token", "secret", "cookie", "authorization"}


def now_ts() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(18).replace('-', '').replace('_', '').lower()[:24]}"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def safe_id(value: str) -> str:
    raw = str(value or "").strip()
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in raw) or "default"


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if any(part in key_text.lower() for part in SENSITIVE_KEYS):
                out[key_text] = "[redacted]"
            else:
                out[key_text] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class BrowserBrokerStore:
    """A small durable queue shared by all users of one deployment."""

    def __init__(self, root: Path, *, pairing_ttl_seconds: int = 600, online_after_seconds: int = 180):
        self.root = Path(root)
        self.tasks_dir = self.root / "tasks"
        self.results_dir = self.root / "results"
        self.screenshots_dir = self.root / "screenshots"
        self.pairings_path = self.root / "pairings.json"
        self.extensions_path = self.root / "extensions.json"
        self.audit_path = self.root / "audit.jsonl"
        self.pairing_ttl_seconds = pairing_ttl_seconds
        self.online_after_seconds = online_after_seconds

    def ensure(self) -> None:
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)

    def _read_json(self, path: Path, default: Any) -> Any:
        try:
            if path.is_file():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
        return default

    def _write_json(self, path: Path, payload: Any) -> None:
        self.ensure()
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)

    def append_audit(self, event_type: str, payload: dict[str, Any]) -> None:
        self.ensure()
        entry = {"ts": now_ts(), "event_type": event_type, "payload": redact(payload)}
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")

    # -- pairing ------------------------------------------------------------

    def create_pairing(self, *, user_id: str, label: str = "") -> dict[str, Any]:
        self.ensure()
        pairings = self._read_json(self.pairings_path, {})
        ticket = new_id("brp")
        item = {
            "ticket": ticket,
            "user_id": user_id,
            "label": label,
            "created_at": now_ts(),
            "expires_at": now_ts() + self.pairing_ttl_seconds,
            "used": False,
        }
        pairings[ticket] = item
        self._write_json(self.pairings_path, pairings)
        self.append_audit("browser.pairing_created", {"user_id": user_id, "label": label})
        return item

    def complete_pairing(self, *, ticket: str, extension_label: str = "") -> dict[str, Any]:
        pairings = self._read_json(self.pairings_path, {})
        item = pairings.get(ticket)
        if not isinstance(item, dict) or item.get("used") or float(item.get("expires_at") or 0) < now_ts():
            raise ValueError("Invalid or expired pairing ticket")
        extension_id = new_id("bre")
        token = secrets.token_urlsafe(32)
        extensions = self._read_json(self.extensions_path, {})
        extension = {
            "extension_id": extension_id,
            "token_hash": token_hash(token),
            "user_id": item["user_id"],
            "label": extension_label or item.get("label") or "Browser Extension",
            "created_at": now_ts(),
            "last_seen": now_ts(),
            "status": "active",
        }
        extensions[extension_id] = extension
        item["used"] = True
        pairings[ticket] = item
        self._write_json(self.extensions_path, extensions)
        self._write_json(self.pairings_path, pairings)
        self.append_audit("browser.extension_paired", {"extension_id": extension_id, "user_id": item["user_id"]})
        public = dict(extension)
        public.pop("token_hash", None)
        public["extension_token"] = token
        # The extension stores instance_id and refuses to poll without it; there
        # is no separate instance concept here, so key it to the user.
        public["instance_id"] = extension["user_id"]
        return public

    def authenticate_extension(self, *, extension_id: str, extension_token: str) -> dict[str, Any]:
        extensions = self._read_json(self.extensions_path, {})
        extension = extensions.get(extension_id)
        if not isinstance(extension, dict) or extension.get("status") != "active":
            raise PermissionError("Invalid browser extension")
        expected = str(extension.get("token_hash") or "")
        if not expected or not hmac.compare_digest(expected, token_hash(extension_token)):
            raise PermissionError("Invalid browser extension token")
        extension["last_seen"] = now_ts()
        extensions[extension_id] = extension
        self._write_json(self.extensions_path, extensions)
        return extension

    def unpair(self, *, user_id: str) -> int:
        """Deactivate every extension belonging to a user. Returns the count removed."""
        extensions = self._read_json(self.extensions_path, {})
        removed = 0
        for ext_id, item in list(extensions.items()) if isinstance(extensions, dict) else []:
            if isinstance(item, dict) and str(item.get("user_id") or "") == user_id:
                extensions.pop(ext_id, None)
                removed += 1
        if removed:
            self._write_json(self.extensions_path, extensions)
            self.append_audit("browser.extension_unpaired", {"user_id": user_id, "count": removed})
        return removed

    # -- task queue ---------------------------------------------------------

    def enqueue_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.ensure()
        task_id = payload.get("task_id") or new_id("brt")
        task = {
            **payload,
            "task_id": task_id,
            "status": "pending",
            "created_at": now_ts(),
            "updated_at": now_ts(),
        }
        self._write_json(self.tasks_dir / f"{safe_id(task_id)}.json", redact(task))
        self.append_audit("browser.task_created", {"task_id": task_id, "action": payload.get("action"), "url": payload.get("url")})
        return task

    def read_task(self, task_id: str) -> dict[str, Any] | None:
        data = self._read_json(self.tasks_dir / f"{safe_id(task_id)}.json", None)
        return data if isinstance(data, dict) else None

    def write_task(self, task: dict[str, Any]) -> None:
        task["updated_at"] = now_ts()
        self._write_json(self.tasks_dir / f"{safe_id(str(task.get('task_id') or ''))}.json", redact(task))

    def poll_task(self, *, extension: dict[str, Any]) -> dict[str, Any] | None:
        self.ensure()
        extension_user_id = str(extension.get("user_id") or "").strip()
        candidates: list[dict[str, Any]] = []
        for path in sorted(self.tasks_dir.glob("*.json")):
            task = self._read_json(path, None)
            if not isinstance(task, dict):
                continue
            if task.get("status") not in {"pending", "approved"}:
                continue
            if task.get("requires_confirmation") and task.get("status") != "approved":
                continue
            task_user_id = str(task.get("user_id") or "").strip()
            if task_user_id and extension_user_id and task_user_id != extension_user_id:
                continue
            candidates.append(task)
        if not candidates:
            return None
        task = candidates[0]
        task["status"] = "running"
        task["extension_id"] = extension.get("extension_id")
        self.write_task(task)
        return task

    def submit_result(self, *, task_id: str, extension: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        task = self.read_task(task_id)
        if not task:
            raise ValueError("Unknown browser task")
        if str(task.get("extension_id") or extension.get("extension_id")) != str(extension.get("extension_id")):
            raise PermissionError("Browser task belongs to another extension")
        status = str(result.get("status") or "completed")
        clean_result = redact(self._materialize_screenshot(task_id, result))
        clean_result["task_id"] = task_id
        clean_result["extension_id"] = extension.get("extension_id")
        clean_result["created_at"] = now_ts()
        self._write_json(self.results_dir / f"{safe_id(task_id)}.json", clean_result)
        task["status"] = "failed" if status == "failed" else "completed"
        self.write_task(task)
        self.append_audit("browser.task_result", {"task_id": task_id, "status": task["status"]})
        return clean_result

    def _materialize_screenshot(self, task_id: str, result: dict[str, Any]) -> dict[str, Any]:
        """Persist screenshot data URLs as files instead of storing large blobs in JSON."""
        screenshot = result.get("screenshot")
        if not isinstance(screenshot, str) or not screenshot.startswith("data:image/"):
            return result
        header, _, payload = screenshot.partition(",")
        if not payload or ";base64" not in header:
            return result
        ext = "png"
        media_type = header.removeprefix("data:").split(";", 1)[0]
        if "/" in media_type:
            candidate = media_type.rsplit("/", 1)[-1].lower()
            if candidate in {"png", "jpeg", "jpg", "webp"}:
                ext = "jpg" if candidate == "jpeg" else candidate
        raw = base64.b64decode(payload)
        self.ensure()
        path = self.screenshots_dir / f"{safe_id(task_id)}.{ext}"
        path.write_bytes(raw)
        clean = dict(result)
        clean.pop("screenshot", None)
        clean["screenshot_path"] = str(path)
        clean["screenshot_mime"] = media_type or f"image/{ext}"
        clean["screenshot_bytes"] = len(raw)
        return clean

    def read_result(self, task_id: str) -> dict[str, Any] | None:
        data = self._read_json(self.results_dir / f"{safe_id(task_id)}.json", None)
        return data if isinstance(data, dict) else None

    def approve_task(self, *, task_id: str, approver: str) -> dict[str, Any]:
        task = self.read_task(task_id)
        if not task:
            raise ValueError("Unknown browser task")
        token = new_id("bra")
        task["status"] = "approved"
        task["approval_token"] = token
        task["approved_by"] = approver
        task["approved_at"] = now_ts()
        self.write_task(task)
        self.append_audit("browser.task_approved", {"task_id": task_id, "approver": approver})
        return {"task_id": task_id, "status": "approved", "approval_token": token}

    # -- status -------------------------------------------------------------

    def extension_status(self, *, user_id: str = "") -> dict[str, Any]:
        extensions = self._read_json(self.extensions_path, {})
        now = now_ts()
        user = str(user_id or "").strip()
        items: list[dict[str, Any]] = []
        for item in extensions.values() if isinstance(extensions, dict) else []:
            if not isinstance(item, dict):
                continue
            if user and str(item.get("user_id") or "").strip() != user:
                continue
            items.append(
                {
                    "extension_id": item.get("extension_id"),
                    "user_id": item.get("user_id"),
                    "label": item.get("label"),
                    "status": item.get("status"),
                    "created_at": item.get("created_at"),
                    "last_seen": item.get("last_seen"),
                    "online": bool(float(item.get("last_seen") or 0) >= now - self.online_after_seconds),
                }
            )
        items.sort(key=lambda entry: float(entry.get("last_seen") or 0), reverse=True)
        return {
            "paired": bool(items),
            "online": any(bool(entry.get("online")) for entry in items),
            "extensions": items,
        }

    def is_online(self, user_id: str) -> bool:
        return bool(self.extension_status(user_id=user_id).get("online"))
