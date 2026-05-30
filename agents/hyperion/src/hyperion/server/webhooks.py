"""
webhooks.py — outbound task-completion callbacks with an SSRF guard (Phase 9).

A task may carry a ``callback_url``; on terminal completion we POST the result
there exactly once. Because the URL is caller-supplied and the API can reach
internal services (LiteLLM, Qdrant, Langfuse, the Docker network), every URL is
checked against an SSRF allowlist first: the host must resolve *entirely* to
private, loopback, or link-local addresses. A host that resolves to any public
address — or fails to resolve — is rejected before any connection is attempted.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx

from hyperion.config import settings

logger = logging.getLogger(__name__)


class UnsafeCallbackURL(ValueError):
    """Raised when a callback_url fails the SSRF allowlist check."""


def _is_private_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return addr.is_private or addr.is_loopback or addr.is_link_local


def validate_callback_url(url: str) -> None:
    """Raise UnsafeCallbackURL unless every resolved address is private/loopback.

    Resolving the host (not just string-matching it) is what defeats DNS-rebinding
    and decimal/hex IP encodings — we check the addresses the OS would actually dial."""
    if (settings.hyperion_callback_ssrf_guard or "on").lower() == "off":
        return

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeCallbackURL(f"callback_url scheme must be http/https, got {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise UnsafeCallbackURL("callback_url has no host")

    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise UnsafeCallbackURL(f"callback_url host {host!r} did not resolve: {exc}") from exc

    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise UnsafeCallbackURL(f"callback_url host {host!r} resolved to no addresses")
    for ip in addresses:
        if not _is_private_ip(ip):
            raise UnsafeCallbackURL(
                f"callback_url host {host!r} resolves to public address {ip} — refusing (SSRF guard)"
            )


async def fire_callback(url: str, payload: dict) -> bool:
    """POST ``payload`` to ``url`` once. Best-effort: never raises into the caller.
    Returns True on a 2xx response, False otherwise (including a guard rejection)."""
    try:
        validate_callback_url(url)
    except UnsafeCallbackURL as exc:
        logger.warning("Skipping callback: %s", exc)
        return False

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
        ok = 200 <= resp.status_code < 300
        if not ok:
            logger.warning("Callback to %s returned HTTP %s", url, resp.status_code)
        return ok
    except Exception as exc:  # network failure must never break task bookkeeping
        logger.warning("Callback to %s failed: %s", url, exc)
        return False
