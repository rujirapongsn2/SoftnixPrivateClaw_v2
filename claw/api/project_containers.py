"""Control Plane endpoints for project-container readiness and image builds."""

from fastapi import APIRouter, Depends, HTTPException
import asyncio
from datetime import datetime, timezone
import secrets
import ssl
import socket
import time

import httpx
from pydantic import BaseModel

from claw.api.deps import AppState, get_state, require_admin
from claw.db.models import User

router = APIRouter(prefix="/api/admin/project-containers")


class ProjectContainersBody(BaseModel):
    enabled: bool


_VERIFY_MIN_INTERVAL = 15.0
_verify_gate = asyncio.Lock()
_verify_last_started = 0.0
_verify_in_flight = False


def _verify_check(
    check_id: str,
    status: str,
    detail: str,
    hint: str = "",
    *,
    message_key: str,
    hint_key: str = "",
    params: dict[str, str] | None = None,
) -> dict:
    return {
        "id": check_id,
        "status": status,
        "detail": detail,
        "hint": hint,
        "message_key": message_key,
        "hint_key": hint_key,
        "params": params or {},
    }


async def _verify_public_ingress(settings) -> dict:
    """Probe the configured public ingress without changing its state.

    The probe hostname is deliberately synthetic: DNS and TLS can be checked
    without exposing a real project, while the expected PrivateClaw 404 body
    confirms that the request reached this application's ingress middleware.
    """
    domain = str(getattr(settings, "project_ingress_domain", "") or "").strip().lower().rstrip(".")
    scheme = str(getattr(settings, "project_ingress_scheme", "https") or "https")
    checks: list[dict] = []
    if not domain:
        checks.append(_verify_check(
            "privateclaw", "passed", "PrivateClaw admin API is responding.",
            message_key="admin.projects.verifyMessage.privateclaw",
        ))
        checks.append(_verify_check(
            "configuration", "failed", "Project ingress domain is not configured.",
            "Set CLAW_SANDBOX__PROJECT_INGRESS_DOMAIN and restart PrivateClaw.",
            message_key="admin.projects.verifyMessage.configurationMissing",
            hint_key="admin.projects.verifyHintText.configurationMissing",
        ))
        return {"ready": False, "checked_at": datetime.now(timezone.utc).isoformat(), "checks": checks}

    checks.append(_verify_check(
        "privateclaw", "passed", "PrivateClaw admin API is responding.",
        message_key="admin.projects.verifyMessage.privateclaw",
    ))
    feature_enabled = bool(
        getattr(settings, "enabled", True)
        and getattr(settings, "projects_enabled", True)
        and getattr(settings, "project_public_ingress_enabled", False)
    )
    checks.append(_verify_check(
        "configuration",
        "passed" if feature_enabled else "warning",
        f"Using {scheme}://*.{domain}." if feature_enabled else "The domain is configured, but public project ingress is disabled.",
        "Enable project containers and Public project ingress after the network checks pass." if not feature_enabled else "",
        message_key="admin.projects.verifyMessage.configurationReady" if feature_enabled else "admin.projects.verifyMessage.configurationDisabled",
        hint_key="admin.projects.verifyHintText.configurationDisabled" if not feature_enabled else "",
        params={"domain": domain},
    ))
    probe_host = f"pc-verify-{secrets.token_hex(4)}.{domain}"
    dns_resolves = False
    dns_cname_ok = False
    try:
        addresses = await asyncio.wait_for(
            asyncio.to_thread(socket.getaddrinfo, probe_host, 443, type=socket.SOCK_STREAM), timeout=5
        )
        resolved = sorted({item[4][0] for item in addresses})
        dns_resolves = bool(resolved)
        cname_targets: list[str] = []
        cname_checked = False
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=5.0), trust_env=False) as dns_client:
                dns_response = await dns_client.get(
                    "https://cloudflare-dns.com/dns-query",
                    params={"name": probe_host, "type": "CNAME"},
                    headers={"accept": "application/dns-json"},
                )
            dns_response.raise_for_status()
            cname_checked = True
            cname_targets = [
                str(answer.get("data", "")).rstrip(".").lower()
                for answer in dns_response.json().get("Answer", [])
                if answer.get("type") == 5 and answer.get("data")
            ]
            dns_cname_ok = any(target.endswith(".cfargotunnel.com") for target in cname_targets)
        except (httpx.HTTPError, ValueError, TypeError):
            pass
        dns_status = "passed" if dns_resolves and dns_cname_ok else "failed" if cname_checked else "warning"
        if dns_resolves and dns_cname_ok:
            dns_message = f"{probe_host} resolves to {', '.join(cname_targets[:2])}."
        elif dns_resolves and cname_checked:
            dns_message = f"{probe_host} resolves, but it does not point to a Cloudflare Tunnel."
        elif dns_resolves:
            dns_message = f"{probe_host} resolves, but the CNAME target could not be verified."
        else:
            dns_message = "The wildcard hostname did not resolve."
        checks.append(_verify_check(
            "dns", dns_status, dns_message,
            f"Create a proxied wildcard CNAME for *.{domain} pointing to the Tunnel hostname.",
            message_key=(
                "admin.projects.verifyMessage.dnsPassed" if dns_resolves and dns_cname_ok
                else "admin.projects.verifyMessage.dnsTargetFailed" if cname_checked
                else "admin.projects.verifyMessage.dnsTargetUnknown" if dns_resolves
                else "admin.projects.verifyMessage.dnsFailed"
            ),
            hint_key="admin.projects.verifyHintText.dnsFailed" if not (dns_resolves and dns_cname_ok) else "",
            params={"host": probe_host, "target": ", ".join(cname_targets[:2])},
        ))
    except (OSError, TimeoutError, asyncio.TimeoutError):
        checks.append(_verify_check(
            "dns", "failed", f"{probe_host} could not be resolved.",
            "Check the wildcard CNAME, zone, and DNS propagation in Cloudflare.",
            message_key="admin.projects.verifyMessage.dnsFailed",
            hint_key="admin.projects.verifyHintText.dnsFailed",
            params={"host": probe_host},
        ))

    if scheme != "https":
        checks.append(_verify_check(
            "tls", "warning", "HTTPS is not enabled for project ingress.",
            "Use HTTPS in production and ensure the wildcard certificate covers the project hostname.",
            message_key="admin.projects.verifyMessage.tlsHttp",
            hint_key="admin.projects.verifyHintText.tlsHttp",
        ))
        return {"ready": False, "checked_at": datetime.now(timezone.utc).isoformat(), "checks": checks}

    tls_ok = False
    tunnel_ok = False
    if dns_resolves:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(5.0, connect=5.0), follow_redirects=False, trust_env=False
            ) as client:
                response = await client.get(f"https://{probe_host}/")
            tls_ok = True
            checks.append(_verify_check(
                "tls", "passed", "TLS handshake and certificate validation succeeded.",
                message_key="admin.projects.verifyMessage.tlsPassed",
            ))
            body = response.text[:512]
            reached_app = any(marker in body for marker in (
                "Project ingress is disabled", "Project route not found", "Project application is not running",
            ))
            if reached_app:
                tunnel_ok = True
                checks.append(_verify_check(
                    "tunnel", "passed", "The request reached PrivateClaw through Cloudflare Tunnel.",
                    message_key="admin.projects.verifyMessage.tunnelPassed",
                ))
            elif response.status_code in {401, 302, 303, 307, 308}:
                checks.append(_verify_check(
                    "tunnel", "warning", "Cloudflare Access requires authentication before the request can be checked.",
                    "Confirm the Access policy, then verify a running project URL while signed in.",
                    message_key="admin.projects.verifyMessage.tunnelAccess",
                    hint_key="admin.projects.verifyHintText.tunnelAccess",
                ))
            else:
                checks.append(_verify_check(
                    "tunnel", "failed", f"The public endpoint returned HTTP {response.status_code}.",
                    "Check that the Tunnel service is active and its wildcard route points to http://127.0.0.1:8700.",
                    message_key="admin.projects.verifyMessage.tunnelFailedStatus",
                    hint_key="admin.projects.verifyHintText.tunnelFailed",
                    params={"status": str(response.status_code)},
                ))
        except httpx.ConnectError as exc:
            detail = "TLS connection failed."
            if isinstance(exc.__cause__, ssl.SSLCertVerificationError):
                detail = "TLS certificate validation failed."
            checks.append(_verify_check(
                "tls", "failed", detail, "Check the wildcard certificate and Cloudflare SSL settings.",
                message_key="admin.projects.verifyMessage.tlsFailed",
                hint_key="admin.projects.verifyHintText.tlsFailed",
            ))
            checks.append(_verify_check(
                "tunnel", "failed", "The public endpoint could not be reached.", "Check the Tunnel service and wildcard DNS route.",
                message_key="admin.projects.verifyMessage.tunnelFailed",
                hint_key="admin.projects.verifyHintText.tunnelFailed",
            ))
        except (httpx.TimeoutException, httpx.HTTPError):
            checks.append(_verify_check(
                "tls", "failed", "The public endpoint did not respond in time.", "Check the Tunnel service and firewall.",
                message_key="admin.projects.verifyMessage.tlsTimeout",
                hint_key="admin.projects.verifyHintText.tlsTimeout",
            ))
            checks.append(_verify_check(
                "tunnel", "failed", "The Tunnel could not be verified.", "Check that the Tunnel service is active.",
                message_key="admin.projects.verifyMessage.tunnelFailed",
                hint_key="admin.projects.verifyHintText.tunnelFailed",
            ))
    else:
        checks.append(_verify_check(
            "tls", "skipped", "TLS check skipped because DNS did not resolve.",
            message_key="admin.projects.verifyMessage.tlsSkipped",
        ))
        checks.append(_verify_check(
            "tunnel", "skipped", "Tunnel check skipped because DNS did not resolve.",
            message_key="admin.projects.verifyMessage.tunnelSkipped",
        ))
    return {
        "ready": bool(feature_enabled and dns_cname_ok and tls_ok and tunnel_ok),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
    }


def _unavailable() -> dict:
    return {
        "mode_available": False,
        "enabled": False,
        "docker_available": False,
        "image_available": False,
        "image": "sbot-developer:latest",
        "building": False,
        "build_error": "",
        "build_started_at": None,
        "ready": False,
        "public_ingress_enabled": False,
        "public_ingress_configured": False,
        "public_ingress_domain": "",
        "public_ingress_scheme": "https",
        "public_ingress_port": 8000,
        "metrics_available": False,
        "containers": {"running": 0, "stopped": 0, "total": 0},
        "cpu_percent": 0.0,
        "memory_usage_bytes": 0,
        "memory_limit_bytes": 0,
        "disk_usage_bytes": 0,
        "disk_usage_complete": True,
    }


@router.get("")
async def project_container_status(
    admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    manager = state.project_containers
    if manager is None:
        return _unavailable()
    return {"mode_available": True, **await manager.status()}


@router.post("/public-ingress/verify")
async def verify_project_public_ingress(
    admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    global _verify_in_flight, _verify_last_started
    manager = state.project_containers
    if manager is None:
        raise HTTPException(status_code=409, detail="Bot Mode is disabled")
    async with _verify_gate:
        if _verify_in_flight:
            raise HTTPException(
                status_code=429,
                detail="Verification is already in progress. Try again shortly.",
                headers={"Retry-After": "1"},
            )
        now = time.monotonic()
        remaining = _VERIFY_MIN_INTERVAL - (now - _verify_last_started)
        if remaining > 0:
            raise HTTPException(
                status_code=429,
                detail="Verification was run recently. Try again shortly.",
                headers={"Retry-After": str(max(1, int(remaining + 0.999)))},
            )
        _verify_last_started = now
        _verify_in_flight = True
    try:
        result = await _verify_public_ingress(manager.settings)
        await state.audit.log(
            "admin",
            {"event": "project_public_ingress_verified", "ready": result["ready"], "by": admin.id},
            user_id=admin.id,
        )
        return result
    finally:
        async with _verify_gate:
            _verify_in_flight = False


@router.put("")
async def set_project_containers(
    body: ProjectContainersBody,
    admin: User = Depends(require_admin),
    state: AppState = Depends(get_state),
) -> dict:
    manager = state.project_containers
    if manager is None:
        raise HTTPException(status_code=409, detail="Bot Mode is disabled")
    result = await manager.set_enabled(body.enabled)
    await state.audit.log(
        "admin",
        {"event": "project_containers_updated", "enabled": body.enabled, "by": admin.id},
        user_id=admin.id,
    )
    return {"mode_available": True, **result}


@router.post("/build")
async def build_project_container_image(
    admin: User = Depends(require_admin), state: AppState = Depends(get_state)
) -> dict:
    manager = state.project_containers
    if manager is None:
        raise HTTPException(status_code=409, detail="Bot Mode is disabled")
    result = await manager.start_build()
    await state.audit.log(
        "admin",
        {"event": "project_container_image_build_requested", "image": result["image"], "by": admin.id},
        user_id=admin.id,
    )
    return {"mode_available": True, **result}


@router.put("/public-ingress")
async def set_project_public_ingress(
    body: ProjectContainersBody,
    admin: User = Depends(require_admin),
    state: AppState = Depends(get_state),
) -> dict:
    manager = state.project_containers
    if manager is None:
        raise HTTPException(status_code=409, detail="Bot Mode is disabled")
    try:
        result = await manager.set_public_ingress_enabled(body.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await state.audit.log(
        "admin",
        {"event": "project_public_ingress_updated", "enabled": body.enabled, "by": admin.id},
        user_id=admin.id,
    )
    return {"mode_available": True, **result}
