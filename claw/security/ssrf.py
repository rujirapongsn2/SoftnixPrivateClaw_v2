"""SSRF guard for connector-initiated outbound requests (claw/tools/api.py).

A generic API connector's base URL and every operation are user-controlled:
any authenticated user can create one and point it anywhere, then have the
agent call it. Checking the host once, at connector-save time, is not
enough — DNS can be re-pointed afterwards (rebinding) and the target API can
3xx-redirect the request elsewhere — so the check must run per-request,
against every hop (see claw/tools/api.py's redirect loop).

Validating a hostname is not sufficient either: a check that only resolves the
name leaves the socket free to resolve it a second time and get a different
answer (a TTL-0 record alternating between a public IP and 127.0.0.1). That is
why `resolve_public_ip` returns the address it validated — the caller connects
to that exact IP, so the address that was checked and the address that is
dialled are guaranteed to be the same one.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

_ALLOWED_SCHEMES = {"http", "https"}

# IPv6 ranges that carry an IPv4 address inside them and are routed onward to
# that address by a translator. ipaddress judges these on the IPv6 prefix
# alone, so 64:ff9b::7f00:1 reports is_global=True even though it means
# 127.0.0.1 — on any network with a NAT64 gateway that is a straight loopback
# hit. The embedded v4 is the last 32 bits in every one of these prefixes.
# (::ffff:0:0/96 needs no entry: ipaddress already unwraps v4-mapped
# addresses and reports ::ffff:127.0.0.1 as private.)
_V4_EMBEDDING_PREFIXES = (
    ipaddress.ip_network("64:ff9b::/96"),  # RFC 6052 well-known NAT64
    ipaddress.ip_network("64:ff9b:1::/48"),  # RFC 8215 local-use NAT64
)


class UnsafeUrlError(ValueError):
    pass


async def assert_public_url(url: str, *, resolve: bool = True) -> None:
    """Raise UnsafeUrlError unless every address `url`'s host could resolve
    to is a public, globally-routable IP. Blocks private/loopback/link-local
    (incl. the 169.254.169.254 cloud metadata endpoint)/reserved/multicast
    ranges for both IPv4 and IPv6.

    `resolve=False` skips the DNS lookup and only rejects a literal IP host —
    for connector-save-time validation, which must not make save (a normal
    CRUD operation, exercised offline in tests) depend on network/DNS being
    reachable. The authoritative check is `resolve_public_ip` at call time,
    which additionally pins the connection to the address it validated."""
    if resolve:
        await resolve_public_ip(url)
        return
    host = _host_of(url)
    literal = _as_ip(host)
    if literal is not None:
        _assert_public("URL host is", literal)


async def resolve_public_ips(url: str) -> list[str]:
    """Validate `url`'s host and return every public IP the caller may dial.

    Returns [] when the host is already an IP literal — there is nothing to
    pin, the URL can be used as-is. Otherwise EVERY address the name resolves
    to must be public, and all of them are returned in resolver order;
    connecting to one of those literals instead of re-resolving the name is
    what closes the rebinding window described in the module docstring.

    All of them, not just the first: getaddrinfo returns A and AAAA records
    together, and on a dual-stack name the AAAA usually sorts first. Pinning
    to that single address makes an ordinary public API permanently
    unreachable from a host without working IPv6 — there is no fallback left,
    because the whole point of pinning is that the socket no longer re-resolves
    the name. The caller tries them in order instead (see claw/tools/api.py).
    """
    host = _host_of(url)
    literal = _as_ip(host)
    if literal is not None:
        _assert_public("URL host is", literal)
        return []

    loop = asyncio.get_running_loop()
    try:
        # SOCK_STREAM, not the default: without it getaddrinfo returns the
        # same address once per socket type, so every address is validated
        # three times and the "first" one is whichever socket type the
        # resolver happens to list first.
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"could not resolve host: {host}") from exc

    addresses: list[str] = []
    for info in infos:
        address = info[4][0]
        try:
            resolved = ipaddress.ip_address(address)
        except ValueError as exc:
            # e.g. a scoped link-local "fe80::1%en0". Unparseable means
            # unvalidatable, which has to mean refused, not skipped.
            raise UnsafeUrlError(f"URL host {host} resolved to an unusable address: {address}") from exc
        _assert_public(f"URL host {host} resolves to", resolved)
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise UnsafeUrlError(f"could not resolve host: {host}")
    return addresses


async def resolve_public_ip(url: str) -> str | None:
    """First address from `resolve_public_ips`, or None for an IP literal."""
    addresses = await resolve_public_ips(url)
    return addresses[0] if addresses else None


def _assert_public(subject: str, address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if not address.is_global:
        raise UnsafeUrlError(f"{subject} a non-public address: {address}")
    if isinstance(address, ipaddress.IPv6Address):
        for prefix in _V4_EMBEDDING_PREFIXES:
            if address in prefix:
                embedded = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
                if not embedded.is_global:
                    raise UnsafeUrlError(
                        f"{subject} a non-public address: {address} (embeds {embedded})"
                    )


def _host_of(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise UnsafeUrlError(f"unsupported URL scheme: {parts.scheme or '(none)'}")
    if not parts.hostname:
        raise UnsafeUrlError("URL has no host")
    return parts.hostname


def _as_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


class ResponseTooLarge(ValueError):
    pass


async def read_bounded(response: httpx.Response, max_bytes: int) -> bytes:
    """Read a streamed response body, refusing anything over `max_bytes`.

    For a response whose only legitimate shape is a small JSON document (an
    OAuth token or metadata response), oversize means hostile or broken, so
    this raises rather than truncating — a truncated token document is not
    a token document."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise ResponseTooLarge(f"response body exceeds {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


class PinnedTransport(httpx.AsyncBaseTransport):
    """Dial every request at an address `resolve_public_ips` validated.

    The request is re-aimed at that IP with the Host header and TLS SNI left
    on the real hostname, so certificate verification still checks the name
    the operator configured. Pinning removes the resolver's own address
    fallback, so it is reimplemented here: each validated address is tried
    until one connects. Only a connect-level failure moves on — once the peer
    has answered, its response (or its error) is the answer.

    One inner transport per hostname, not one shared pool: httpcore keys its
    connection pool on the dialled origin, which is now the IP, so two vhosts
    behind one CDN address would otherwise reuse a TLS connection negotiated
    for the other name.

    `require_https` is for OAuth-flow clients, where a cleartext token or
    metadata exchange is never acceptable; the generic API connector keeps
    allowing http because the operator chose that URL.
    """

    def __init__(
        self,
        *,
        require_https: bool = False,
        inner: Callable[[], httpx.AsyncBaseTransport] = httpx.AsyncHTTPTransport,
    ) -> None:
        self._require_https = require_https
        self._make_inner = inner
        self._inner: dict[str, httpx.AsyncBaseTransport] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if self._require_https and url.scheme != "https":
            raise UnsafeUrlError(f"https is required, got {url.scheme or '(none)'}://")
        addresses = await resolve_public_ips(str(url))
        inner = self._inner.get(url.host)
        if inner is None:
            inner = self._inner[url.host] = self._make_inner()
        if not addresses:
            # Host is already an IP literal; there is nothing to pin.
            return await inner.handle_async_request(request)

        headers = request.headers.copy()
        headers["Host"] = url.netloc.decode("ascii")
        last_error: httpx.TransportError | None = None
        for address in addresses:
            pinned = httpx.Request(
                request.method,
                url.copy_with(host=address),
                headers=headers,
                stream=request.stream,
                extensions={**request.extensions, "sni_hostname": url.host},
            )
            try:
                return await inner.handle_async_request(pinned)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_error = exc
        raise last_error  # type: ignore[misc]

    async def aclose(self) -> None:
        for inner in self._inner.values():
            await inner.aclose()
        self._inner.clear()


def pinned_client(
    *,
    require_https: bool = False,
    inner: Callable[[], httpx.AsyncBaseTransport] | None = None,
    **client_kwargs: Any,
) -> httpx.AsyncClient:
    """An httpx client whose every request goes through `PinnedTransport`.

    Redirects are never followed by the client: a redirect target is a new
    URL the peer chose, so the caller either re-validates and re-sends the
    hop itself (claw/tools/api.py) or treats a 3xx as an error (OAuth)."""
    transport = PinnedTransport(require_https=require_https, **({"inner": inner} if inner else {}))
    return httpx.AsyncClient(transport=transport, follow_redirects=False, **client_kwargs)
