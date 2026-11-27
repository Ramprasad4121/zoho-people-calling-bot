"""Optional integrations: Calendly scheduling, lead export and live product research.

All integrations degrade gracefully. Nothing here reports success unless the
remote API actually confirmed it.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import security
from security import UnsafeURLError, fetch_url, redact

logger = logging.getLogger("zoho_calling_bot.integrations")

CALENDLY_API = "https://api.calendly.com"


class IntegrationError(RuntimeError):
    """An integration is misconfigured or its provider rejected the request."""


# --- Calendly -------------------------------------------------------------


def calendly_configured() -> bool:
    return bool(
        os.getenv("CALENDLY_ACCESS_TOKEN", "").strip()
        and os.getenv("CALENDLY_EVENT_TYPE_URI", "").strip().startswith(f"{CALENDLY_API}/event_types/")
    )


def calendly_missing_config() -> str:
    missing = []
    if not os.getenv("CALENDLY_ACCESS_TOKEN", "").strip():
        missing.append("CALENDLY_ACCESS_TOKEN")
    if not os.getenv("CALENDLY_EVENT_TYPE_URI", "").strip().startswith(f"{CALENDLY_API}/event_types/"):
        missing.append("CALENDLY_EVENT_TYPE_URI (https://api.calendly.com/event_types/<uuid>)")
    return "Missing " + ", ".join(missing)


def _calendly_request(
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, str]] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not calendly_configured():
        raise IntegrationError(calendly_missing_config())

    url = f"{CALENDLY_API}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"
    headers = {
        "Authorization": f"Bearer {os.environ['CALENDLY_ACCESS_TOKEN'].strip()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    try:
        raw = fetch_url(url, method=method, headers=headers, body=body, timeout=6, max_bytes=100_000)
        data = json.loads(raw.decode("utf-8"))
    except UnsafeURLError as exc:
        raise IntegrationError(f"Calendly URL rejected: {exc}") from exc
    except Exception as exc:
        logger.warning("event=calendly_request_failed method=%s path=%s error=%s", method, path, redact(exc))
        raise IntegrationError("Calendly is unavailable right now. Please try again later.") from exc
    if not isinstance(data, dict):
        raise IntegrationError("Calendly returned an unexpected response.")
    return data


def available_slots(*, days: int = 7, limit: int = 3, now: Optional[datetime] = None) -> List[Dict[str, str]]:
    """Return a few real, bookable slots in a caller-friendly timezone."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    end = current + timedelta(days=max(1, min(int(days), 31)))
    data = _calendly_request(
        "GET",
        "/event_type_available_times",
        params={
            "event_type": os.environ["CALENDLY_EVENT_TYPE_URI"].strip(),
            "start_time": current.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_time": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    try:
        tz_name = os.getenv("CALENDLY_DISPLAY_TIMEZONE", "Asia/Kolkata").strip() or "Asia/Kolkata"
        display_tz = ZoneInfo(tz_name)
    except Exception:
        display_tz = timezone.utc

    slots: List[Dict[str, str]] = []
    for item in data.get("collection", []):
        if not isinstance(item, dict) or item.get("status") != "available":
            continue
        if float(item.get("invitees_remaining") or 0) <= 0:
            continue
        start = str(item.get("start_time", ""))
        try:
            moment = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(display_tz)
        except ValueError:
            continue
        slots.append(
            {
                "start_time": start,
                "label": moment.strftime("%A %I:%M %p %Z"),
                "scheduling_url": str(item.get("scheduling_url", "")),
            }
        )
        if len(slots) >= limit:
            break
    if not slots:
        raise IntegrationError("There are no demo slots available in the next week.")
    return slots


def book_slot(*, start_time: str, lead_name: str, email: str, timezone_name: str = "Asia/Kolkata") -> Dict[str, str]:
    """Create a Calendly invitee. Only call after explicit prospect confirmation."""
    invitee: Dict[str, str] = {"name": lead_name.strip() or "Prospect", "email": email.strip(), "timezone": timezone_name}
    payload: Dict[str, Any] = {
        "event_type": os.environ["CALENDLY_EVENT_TYPE_URI"].strip(),
        "start_time": start_time,
        "invitee": invitee,
    }
    location_kind = os.getenv("CALENDLY_LOCATION_KIND", "").strip()
    if location_kind:
        payload["location"] = {"kind": location_kind}
    data = _calendly_request("POST", "/invitees", payload=payload)
    resource = data.get("resource") if isinstance(data.get("resource"), dict) else {}
    if not resource:
        raise IntegrationError("Calendly did not confirm the booking.")
    return {
        "uri": str(resource.get("uri", "")),
        "start_time": str(resource.get("start_time", start_time)),
        "cancel_url": str(resource.get("cancel_url", "")),
        "reschedule_url": str(resource.get("reschedule_url", "")),
    }


# --- Lead export ----------------------------------------------------------


def post_lead_webhook(record: Dict[str, Any]) -> None:
    """Best-effort POST to an operator-configured HTTPS receiver. Never raises."""
    url = os.getenv("SALES_WEBHOOK_URL", "").strip()
    if not url:
        return
    allow_private = os.getenv("ALLOW_PRIVATE_WEBHOOK", "false").strip().lower() in {"1", "true", "yes"}
    try:
        security.validate_public_url(url, allow_private=allow_private)
        fetch_url(
            url,
            method="POST",
            headers={"Content-Type": "application/json"},
            body=json.dumps(record).encode("utf-8"),
            timeout=3,
            max_bytes=10_000,
            allow_private=allow_private,
        )
    except Exception as exc:
        logger.warning("event=lead_export_failed error=%s", redact(exc))


# --- Live product research ------------------------------------------------

FEATURES_URL = "https://www.zoho.com/people/features.html"
PRICING_URL = "https://www.zoho.com/people/zohopeople-pricing.html"
INTERLOOP_URL = "https://interloopconsulting.com/"

_RESEARCH_CACHE: Dict[str, Any] = {"data": None, "expires_at": 0.0, "fetched_at": ""}


class _TextExtractor:
    """Tiny HTML-to-text helper; avoids adding a scraping dependency."""

    def __init__(self) -> None:
        self.parts: List[str] = []
        self._skip_depth = 0

    def feed(self, chunk: str) -> None:
        import re

        chunk = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", chunk)
        chunk = re.sub(r"(?s)<!--.*?-->", " ", chunk)
        chunk = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", chunk)
        chunk = re.sub(r"(?s)<[^>]+>", " ", chunk)
        self.parts.append(chunk)

    def text(self) -> str:
        import html
        import re

        value = html.unescape(" ".join(self.parts))
        value = re.sub(r"[ \t\r\f\v]+", " ", value)
        value = re.sub(r"\n\s*\n+", "\n", value)
        return value.strip()


def _fetch_text(url: str) -> str:
    raw = fetch_url(
        url,
        timeout=5,
        max_bytes=400_000,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    extractor = _TextExtractor()
    extractor.feed(raw.decode("utf-8", errors="replace"))
    return extractor.text()


def refresh_research() -> Optional[Dict[str, str]]:
    """Fetch official pages; keep only short snippets that can be read aloud."""
    fetched: Dict[str, str] = {}
    for key, url in (
        ("zoho_features", FEATURES_URL),
        ("zoho_pricing", PRICING_URL),
        ("interloop", INTERLOOP_URL),
    ):
        try:
            text = _fetch_text(url)
            if text:
                fetched[key] = text[:700]
                logger.info("event=research_fetched source=%s chars=%d", key, len(text))
        except Exception as exc:
            logger.warning("event=research_failed source=%s error=%s", key, redact(exc))

    if not fetched:
        logger.warning("event=research_unavailable using_local_kb=true")
        return None

    data = {
        "zoho_features": fetched.get("zoho_features", ""),
        "zoho_pricing": fetched.get("zoho_pricing", ""),
        "interloop": fetched.get("interloop", ""),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "sources": ", ".join(sorted(fetched)),
    }
    ttl = int(os.getenv("RESEARCH_CACHE_TTL_SECONDS", "3600") or 3600)
    import time

    _RESEARCH_CACHE.update(
        {"data": data, "expires_at": time.monotonic() + max(60, ttl), "fetched_at": data["fetched_at"]}
    )
    return data


def get_research(*, allow_fetch: bool = False) -> Optional[Dict[str, str]]:
    """Return cached official-site facts, optionally refreshing them."""
    import time

    cached = _RESEARCH_CACHE.get("data")
    if cached and time.monotonic() < _RESEARCH_CACHE.get("expires_at", 0):
        return cached
    if allow_fetch:
        return refresh_research()
    return cached


def research_status() -> Dict[str, Any]:
    import time

    cached = _RESEARCH_CACHE.get("data")
    return {
        "live_context_available": bool(cached and time.monotonic() < _RESEARCH_CACHE.get("expires_at", 0)),
        "fetched_at": _RESEARCH_CACHE.get("fetched_at", "") if cached else "",
    }
