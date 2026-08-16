"""SSRF guard (claw/security/ssrf.py) for connector-initiated outbound calls.

Any authenticated user can create a generic API connector pointing anywhere, so
this is the only thing standing between a chat turn and the cloud metadata
endpoint. These tests cover the parts that are easy to get subtly wrong:
validating EVERY resolved address rather than the first, returning all of them
so a dual-stack name stays reachable, and the IPv6 prefixes that smuggle a
private IPv4 address past `ipaddress.is_global`.
"""

import asyncio
import socket

import pytest

from claw.security.ssrf import (
    UnsafeUrlError,
    assert_public_url,
    resolve_public_ip,
    resolve_public_ips,
)


def _patch_dns(monkeypatch, addresses, *, calls=None):
    """Make getaddrinfo return `addresses` (in order), recording its kwargs."""
    loop = asyncio.get_running_loop()

    async def fake_getaddrinfo(host, port, *, family=0, type=0, proto=0, flags=0):
        if calls is not None:
            calls.append({"host": host, "port": port, "type": type})
        infos = []
        for address in addresses:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sockaddr = (address, 0, 0, 0) if family == socket.AF_INET6 else (address, 0)
            infos.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr))
        return infos

    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)


async def test_public_ip_literal_needs_no_pinning(monkeypatch):
    assert await resolve_public_ips("https://93.184.216.34/x") == []


async def test_private_ip_literal_is_rejected():
    for host in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "[::1]", "[fe80::1]"):
        with pytest.raises(UnsafeUrlError):
            await resolve_public_ips(f"https://{host}/x")


async def test_every_resolved_address_is_validated_not_just_the_first(monkeypatch):
    """A name whose FIRST record is public but whose second is loopback must be
    refused outright: the caller falls back through the list, so validating
    only infos[0] would let the fallback reach 127.0.0.1."""
    _patch_dns(monkeypatch, ["93.184.216.34", "127.0.0.1"])
    with pytest.raises(UnsafeUrlError) as exc:
        await resolve_public_ips("https://rebind.invalid/x")
    assert "127.0.0.1" in str(exc.value)


async def test_all_public_addresses_are_returned_in_order_and_deduped(monkeypatch):
    """Pinning to a single address removes the resolver's own retry, so a
    dual-stack name on an IPv4-only host would be permanently unreachable."""
    _patch_dns(monkeypatch, ["2606:2800:220::1", "93.184.216.34", "93.184.216.34"])
    assert await resolve_public_ips("https://dual.invalid/x") == [
        "2606:2800:220::1",
        "93.184.216.34",
    ]


async def test_lookup_asks_for_stream_sockets_only(monkeypatch):
    """Without type=SOCK_STREAM getaddrinfo returns each address once per
    socket type — three duplicates, and an arbitrary "first"."""
    calls = []
    _patch_dns(monkeypatch, ["93.184.216.34"], calls=calls)
    await resolve_public_ips("https://ok.invalid/x")
    assert calls[0]["type"] == socket.SOCK_STREAM


async def test_nat64_prefix_embedding_a_private_v4_is_rejected(monkeypatch):
    """64:ff9b::7f00:1 is is_global=True by IPv6 prefix alone, but on a network
    with a NAT64 gateway it is a straight 127.0.0.1 hit."""
    for address in ("64:ff9b::7f00:1", "64:ff9b::a00:5", "64:ff9b::a9fe:a9fe"):
        _patch_dns(monkeypatch, [address])
        with pytest.raises(UnsafeUrlError) as exc:
            await resolve_public_ips("https://nat64.invalid/x")
        assert "embeds" in str(exc.value)


async def test_nat64_prefix_embedding_a_public_v4_is_allowed(monkeypatch):
    _patch_dns(monkeypatch, ["64:ff9b::5db8:d822"])  # 93.184.216.34
    assert await resolve_public_ips("https://nat64ok.invalid/x") == ["64:ff9b::5db8:d822"]


async def test_v4_mapped_loopback_is_rejected(monkeypatch):
    _patch_dns(monkeypatch, ["::ffff:127.0.0.1"])
    with pytest.raises(UnsafeUrlError):
        await resolve_public_ips("https://mapped.invalid/x")


async def test_scoped_link_local_is_rejected(monkeypatch):
    _patch_dns(monkeypatch, ["fe80::1%en0"])
    with pytest.raises(UnsafeUrlError):
        await resolve_public_ips("https://scoped.invalid/x")


async def test_unparseable_address_is_refused_not_skipped(monkeypatch):
    """Anything ipaddress can't parse is unvalidatable, and unvalidatable has
    to mean refused — never "skip it and try the next one"."""
    _patch_dns(monkeypatch, ["not-an-address", "93.184.216.34"])
    with pytest.raises(UnsafeUrlError) as exc:
        await resolve_public_ips("https://weird.invalid/x")
    assert "unusable" in str(exc.value)


async def test_resolution_failure_is_an_unsafe_url_error(monkeypatch):
    loop = asyncio.get_running_loop()

    async def boom(*args, **kwargs):
        raise socket.gaierror("nope")

    monkeypatch.setattr(loop, "getaddrinfo", boom)
    with pytest.raises(UnsafeUrlError):
        await resolve_public_ips("https://nxdomain.invalid/x")


async def test_empty_resolution_is_refused(monkeypatch):
    _patch_dns(monkeypatch, [])
    with pytest.raises(UnsafeUrlError):
        await resolve_public_ips("https://empty.invalid/x")


async def test_non_http_schemes_are_refused():
    for url in ("file:///etc/passwd", "gopher://x.invalid/", "ftp://x.invalid/", "/no-scheme"):
        with pytest.raises(UnsafeUrlError):
            await resolve_public_ips(url)


async def test_resolve_public_ip_returns_the_first_address(monkeypatch):
    _patch_dns(monkeypatch, ["93.184.216.34", "93.184.216.35"])
    assert await resolve_public_ip("https://first.invalid/x") == "93.184.216.34"
    assert await resolve_public_ip("https://93.184.216.34/x") is None


async def test_save_time_check_skips_dns_but_still_blocks_ip_literals(monkeypatch):
    """resolve=False must not depend on DNS (save is exercised offline), but a
    literal private IP is decidable without it."""
    loop = asyncio.get_running_loop()

    async def must_not_be_called(*args, **kwargs):
        raise AssertionError("resolve=False must not hit DNS")

    monkeypatch.setattr(loop, "getaddrinfo", must_not_be_called)

    await assert_public_url("https://api.example.com/v1", resolve=False)
    with pytest.raises(UnsafeUrlError):
        await assert_public_url("http://169.254.169.254/latest/meta-data/", resolve=False)
    with pytest.raises(UnsafeUrlError):
        await assert_public_url("https://[64:ff9b::7f00:1]/x", resolve=False)
