"""Security helpers for the internet-facing calling bot.

Everything in this module is dependency-light on purpose: it protects the
call-triggering endpoints, the Twilio callbacks and all outbound HTTP fetches.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import re
import secrets
import socket
import threading
import time
from typing import Dict, Iterable, Optional
from urllib.parse import urlparse
from urllib.request import (
    HTTPError,
    HTTPRedirectHandler,
    Request as UrlRequest,
    build_opener,
)

from fastapi import HTTPException, Request

logger = logging.getLogger("zoho_calling_bot.security")

_PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")
_SECRET_ENV_NAMES = (
    "TWILIO_AUTH_TOKEN",
    "TWILIO_ACCOUNT_SID",
    "OPENAI_API_KEY",
    "LLM_API_KEY",
    "GROQ_API_KEY",
    "CALENDLY_ACCESS_TOKEN",
    "CALL_API_TOKEN",
)

_RATE_LIMITS: Dict[tuple[str, str], list[float]] = {}
_RATE_LOCK = threading.Lock()


class UnsafeURLError(ValueError):
    """Raised when an outbound URL is not safe to fetch."""


def normalize_phone_number(number: str) -> str:
    """Return a strict E.164 number or raise ValueError."""
    if not isinstance(number, str):
        raise ValueError("Phone number must be a string.")
    cleaned = re.sub(r"[\s\-().]", "", number.strip())
    if not _PHONE_RE.fullmatch(cleaned):
        raise ValueError("Invalid phone number; use E.164 format like +15551234567.")
    return cleaned


def mask_phone(number: str) -> str:
    """Avoid writing full phone numbers (PII) to logs."""
    return f"+***{number[-4:]}" if len(number) >= 4 else "***"


def redact(text: object) -> str:
    """Remove configured secret values from an error string before logging/returning it."""
    value = str(text)
    for name in _SECRET_ENV_NAMES:
        secret = os.getenv(name, "").strip()
        if secret and len(secret) >= 4:
            value = value.replace(secret, "***")
    return value


def call_token_configured() -> bool:
    return bool(os.getenv("CALL_API_TOKEN", "").strip())


def require_call_token(request: Request) -> None:
    """Guard endpoints that can spend money or expose call transcripts."""
    token = os.getenv("CALL_API_TOKEN", "").strip()
    if not token:
        logger.error("event=console_auth_denied reason=token_not_configured")
        raise HTTPException(
            status_code=503,
            detail="Phone calls and call history are disabled until CALL_API_TOKEN is set in the server environment.",
        )
    supplied = request.headers.get("x-api-token", "").strip()
    if not supplied:
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
    if not supplied or not secrets.compare_digest(supplied, token):
        logger.warning("event=console_auth_failed path=%s", request.url.path)
        raise HTTPException(status_code=401, detail="Invalid or missing console API token.")


def rate_limit(request: Request, bucket: str, limit: int = 20, window_seconds: int = 60) -> None:
    """Small in-process abuse guard. It is intentionally simple and fails closed under load only."""
    if os.getenv("RATE_LIMIT_ENABLED", "true").strip().lower() in {"0", "false", "no"}:
        return
    client = request.client.host if request.client else "unknown"
    key = (client, bucket)
    now = time.monotonic()
    with _RATE_LOCK:
        hits = [t for t in _RATE_LIMITS.get(key, []) if now - t < window_seconds]
        if len(hits) >= limit:
            logger.warning("event=rate_limit path=%s bucket=%s", request.url.path, bucket)
            raise HTTPException(status_code=429, detail="Too many requests. Please wait a minute and try again.")
        hits.append(now)
        _RATE_LIMITS[key] = hits
        if len(_RATE_LIMITS) > 2000:
            for stale_key, stale_hits in list(_RATE_LIMITS.items()):
                if not any(now - t < window_seconds for t in stale_hits):
                    _RATE_LIMITS.pop(stale_key, None)


def signature_validation_enabled() -> bool:
    return os.getenv("TWILIO_VERIFY_SIGNATURES", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def verify_twilio_signature(
    request: Request,
    params: Dict[str, str],
    *,
    fallback_token: str = "",
) -> None:
    """Verify X-Twilio-Signature, with a per-call token as a safe fallback.

    Twilio signs the exact URL (including query string) and the POST parameters.
    Behind a tunnel, Starlette's request.url can differ in minor ways, so we
    also try PUBLIC_BASE_URL + path. Some trial webhooks arrive unsigned; for
    those we accept only a high-entropy token that was generated for this exact
    call and embedded in the callback URL. Signature verification is enabled by
    default and can only be disabled explicitly for local development/tests.
    """
    if not signature_validation_enabled():
        return

    signature = request.headers.get("x-twilio-signature", "")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
    if signature and auth_token:
        try:
            from twilio.request_validator import RequestValidator
        except ImportError as exc:  # pragma: no cover - only when SDK is absent
            logger.error("event=twilio_validator_unavailable")
            raise HTTPException(status_code=503, detail="Twilio signature validation is unavailable.") from exc

        query = request.url.query
        candidates = {str(request.url)}
        base = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
        if base:
            path = request.url.path
            suffix = f"?{query}" if query else ""
            candidates.add(f"{base}{path}{suffix}")
            candidates.add(f"{base}{path.rstrip('/')}{suffix}")

        validator = RequestValidator(auth_token)
        if any(validator.validate(url, params, signature) for url in candidates):
            return

    supplied_token = request.query_params.get("token", "")
    if fallback_token and supplied_token and hmac.compare_digest(supplied_token, fallback_token):
        logger.info("event=twilio_token_fallback path=%s", request.url.path)
        return

    if not signature or not auth_token:
        logger.warning("event=twilio_signature_missing path=%s", request.url.path)
        raise HTTPException(status_code=403, detail="Twilio signature is missing.")

    logger.warning("event=twilio_signature_invalid path=%s", request.url.path)
    raise HTTPException(status_code=403, detail="Invalid Twilio signature.")


def _resolved_ips(host: str) -> Iterable[ipaddress._BaseAddress]:
    for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM):
        try:
            yield ipaddress.ip_address(info[4][0])
        except ValueError:
            continue


def validate_public_url(url: str, *, allow_private: bool = False, require_https: bool = True) -> str:
    """Reject malformed URLs and private-network targets (SSRF protection)."""
    parsed = urlparse(url)
    if require_https and parsed.scheme != "https":
        raise UnsafeURLError("Only HTTPS URLs are allowed.")
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeURLError("Unsupported URL scheme.")
    if parsed.username or parsed.password or not parsed.hostname:
        raise UnsafeURLError("URL must not contain credentials and must include a hostname.")
    if allow_private:
        return url
    try:
        addresses = list(_resolved_ips(parsed.hostname))
    except socket.gaierror as exc:
        raise UnsafeURLError(f"Could not resolve hostname {parsed.hostname!r}.") from exc
    if not addresses:
        raise UnsafeURLError(f"Could not resolve hostname {parsed.hostname!r}.")
    for address in addresses:
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
        ):
            raise UnsafeURLError(f"Refusing to fetch non-public address for {parsed.hostname!r}.")
    return url


class _RefuseRedirects(HTTPRedirectHandler):
    """Redirects could move an allowlisted request to a private address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        logger.warning("event=redirect_blocked host=%s", urlparse(newurl).hostname or "unknown")
        return None


HTTP_OPENER = build_opener(_RefuseRedirects())


def fetch_url(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout: float = 5.0,
    max_bytes: int = 200_000,
    allow_private: bool = False,
    require_https: bool = True,
) -> bytes:
    """Fetch a validated public URL with a timeout, size cap and no redirects."""
    validate_public_url(url, allow_private=allow_private, require_https=require_https)
    request = UrlRequest(url, data=body, headers=headers or {}, method=method)
    with HTTP_OPENER.open(request, timeout=timeout) as response:  # noqa: S310 - URL validated above
        if getattr(response, "status", 200) >= 400:
            raise HTTPError(url, response.status, "HTTP error", response.headers, None)
        return response.read(max_bytes + 1)[:max_bytes]


def webhook_url_error(url: str) -> Optional[str]:
    """Return an actionable error when PUBLIC_BASE_URL cannot serve webhooks."""
    if not url:
        return "PUBLIC_BASE_URL is empty. Start a tunnel and set it to your public HTTPS URL."
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return "PUBLIC_BASE_URL must be a public HTTPS URL such as https://abc.trycloudflare.com."
    try:
        validate_public_url(url)
    except UnsafeURLError as exc:
        return f"PUBLIC_BASE_URL is unusable ({exc}). Restart the tunnel and update PUBLIC_BASE_URL."
    return None
