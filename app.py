"""FastAPI service for the Zoho People AI outbound calling agent.

The phone flow is a small state machine backed by in-memory sessions and an
append-only JSON transcript snapshot. Twilio's ~15s webhook budget is treated
as a hard constraint: no blocking network call is allowed to exceed it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote, urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, Response
from openai import OpenAI
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client as TwilioClient

import integrations
import security
import state
import twiml
from local_demo import fallback_reply, generate_llm_reply, sanitize_for_tts
from prompts import build_greeting, build_system_prompt
from qualification import EMAIL_RE as _EMAIL_RE
from qualification import extract_qualification

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("zoho_calling_bot")

app = FastAPI(title="Zoho People AI Calling Bot")

# State is re-exported so route code and tests share one mutable store.
DATA_DIR = state.DATA_DIR
CALL_HISTORY_PATH = state.CALL_HISTORY_PATH
STATIC_DIR = Path(__file__).parent / "static"
CALL_SESSIONS = state.CALL_SESSIONS
CALL_RECORDS = state.CALL_RECORDS
WEB_SESSIONS = state.WEB_SESSIONS
LAST_CALL_REQUEST = state.LAST_CALL_REQUEST
STATE_LOCK = state.STATE_LOCK
SESSION_TTL_SECONDS = state.SESSION_TTL_SECONDS
MAX_TRANSCRIPT_MESSAGES = state.MAX_TRANSCRIPT_MESSAGES
MAX_RECORDS = state.MAX_RECORDS
_utc_now_iso = state.utc_now_iso
_persist_records = state.persist_records
export_lead_to_webhook = state.export_lead_to_webhook
record_message = state.record_message
purge_expired_sessions = state.purge_expired_sessions
finalize_call = state.finalize_call

PREFLIGHT_CACHE: Dict[str, tuple[float, Optional[str]]] = {}
LLM_CLIENT_CACHE: Dict[tuple, OpenAI] = {}

FINAL_CALL_STATUSES = {"completed", "failed", "busy", "no-answer", "canceled"}
ACTIVE_CALL_STATUSES = {"queued", "initiated", "ringing", "in-progress"}
MAX_TURNS = 30
MAX_SPEECH_CHARS = 500
MAX_HISTORY_MESSAGES = 24
DUPLICATE_CALL_WINDOW_SECONDS = 90
PREFLIGHT_TTL_SECONDS = 60

_DEMO_INTENT_RE = re.compile(
    r"\b(?:book|schedule|set up|arrange)\b.{0,40}\b(?:demo|meeting|call)\b"
    r"|\b(?:demo|meeting)\b.{0,40}\b(?:book|schedule|please|yes|interested|works)\b"
    r"|\b(?:yes|sure|interested|would love)\b.{0,30}\bdemo\b",
    re.IGNORECASE,
)
_HANDOFF_INTENT_RE = re.compile(
    r"\b(?:human|person|agent|manager|colleague|someone from interloop|team member|transfer)\b"
    r"|\bspeak to (?:a|your) (?:person|human|manager)\b",
    re.IGNORECASE,
)
_AFFIRMATIVE_RE = re.compile(
    r"^(?:yes|yeah|yep|yup|sure|please|okay|ok|go ahead|do it|book it|confirm|that works|"
    r"sounds good|connect me|now)\b|\b(?:yes please|please book|book it|go ahead)\b",
    re.IGNORECASE,
)
_NEGATIVE_RE = re.compile(r"^(?:no|nope|not now|maybe later|cancel|never mind)\b", re.IGNORECASE)


def _redact(exc: object) -> str:
    return security.redact(exc)


def _safe_error(exc: object) -> str:
    """Short, useful, credential-free error text for API responses."""
    text = _redact(exc)
    return text[:300] if text else exc.__class__.__name__


# --- Configuration, LLM and webhook reachability --------------------------


def public_base_url() -> str:
    """Absolute URLs are required for TwiML actions, especially on trial accounts."""
    return (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")


def llm_timeout_seconds() -> float:
    from local_demo import llm_timeout_seconds as _timeout

    return _timeout()


def get_llm_client() -> tuple[OpenAI, str]:
    """Supports OpenAI or any OpenAI-compatible provider (Groq, OpenRouter, Ollama)."""
    api_key = (
        os.getenv("LLM_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("GROQ_API_KEY")
    )
    if not api_key:
        raise RuntimeError("Missing LLM_API_KEY / OPENAI_API_KEY / GROQ_API_KEY in environment.")
    base_url = os.getenv("LLM_BASE_URL") or None
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    timeout = llm_timeout_seconds()
    key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]
    cache_key = (key_hash, base_url, model, timeout)
    with STATE_LOCK:
        cached = LLM_CLIENT_CACHE.get(cache_key)
        if cached is not None:
            return cached, model
        kwargs = {"api_key": api_key, "timeout": timeout, "max_retries": 0}
        if base_url:
            kwargs["base_url"] = base_url
        client = OpenAI(**kwargs)
        LLM_CLIENT_CACHE[cache_key] = client
    if len(LLM_CLIENT_CACHE) > 8:
        with STATE_LOCK:
            LLM_CLIENT_CACHE.clear()
            LLM_CLIENT_CACHE[cache_key] = client
    logger.info("event=llm_ready model=%s provider=%s", model, "custom" if base_url else "openai")
    return client, model


def verify_webhook_url(url: str) -> None:
    """Fail before placing a paid call when Twilio could not fetch our webhook."""
    if os.getenv("SKIP_WEBHOOK_PREFLIGHT", "false").strip().lower() in {"1", "true", "yes"}:
        return
    now = time.monotonic()
    with STATE_LOCK:
        cached = PREFLIGHT_CACHE.get(url)
        if cached and now - cached[0] < PREFLIGHT_TTL_SECONDS and cached[1] is None:
            return

    config_error = security.webhook_url_error(url)
    if config_error:
        with STATE_LOCK:
            PREFLIGHT_CACHE[url] = (now, config_error)
        raise HTTPException(status_code=503, detail=config_error)

    try:
        raw = security.fetch_url(f"{url}/webhook-check", timeout=5, max_bytes=1000)
        if raw.decode("utf-8", errors="replace").strip() != "ok":
            raise ValueError("webhook probe did not report ok")
    except Exception as exc:
        message = (
            f"PUBLIC_BASE_URL ({urlparse(url).hostname}) is not reachable from this server: {_safe_error(exc)}. "
            "Restart the tunnel, then set PUBLIC_BASE_URL to the new public HTTPS URL."
        )
        with STATE_LOCK:
            PREFLIGHT_CACHE[url] = (now, message)
        logger.error("event=webhook_preflight_failed host=%s error=%s", urlparse(url).hostname, _redact(exc))
        raise HTTPException(status_code=503, detail=message) from exc

    with STATE_LOCK:
        PREFLIGHT_CACHE[url] = (now, None)


def voice_name() -> str:
    voice = (os.getenv("TWILIO_VOICE", "Polly.Aditi") or "Polly.Aditi").strip()
    return voice if re.fullmatch(r"[A-Za-z0-9._-]+", voice) else "Polly.Aditi"


def handoff_number() -> str:
    raw = (os.getenv("HANDOFF_PHONE_NUMBER") or "").strip()
    if not raw:
        return ""
    try:
        return security.normalize_phone_number(raw)
    except ValueError:
        logger.error("event=handoff_number_invalid")
        return ""


# --- Requests and call placement ------------------------------------------


class OutboundCallRequest(BaseModel):
    to_number: str = Field(..., max_length=32)
    lead_name: str = Field(default="Alex", max_length=100)
    company_name: str = Field(default="Acme", max_length=100)
    team_size: str = Field(default="", max_length=60)
    primary_interest: str = Field(default="", max_length=120)
    email: str = Field(default="", max_length=200)


class BatchCallRequest(BaseModel):
    contacts: List[OutboundCallRequest] = Field(..., max_length=20)


class LeadRegistrationRequest(BaseModel):
    lead_name: str = Field(..., max_length=100)
    company_name: str = Field(default="Acme", max_length=100)
    to_number: str = Field(default="", max_length=32)
    team_size: str = Field(default="", max_length=60)
    primary_interest: str = Field(default="", max_length=120)
    email: str = Field(default="", max_length=200)
    channel: str = Field(default="phone", pattern="^(phone|browser)$")


class WebStartRequest(BaseModel):
    lead_name: str = Field(default="Alex", max_length=100)
    company_name: str = Field(default="Acme", max_length=100)
    team_size: str = Field(default="", max_length=60)
    primary_interest: str = Field(default="", max_length=120)
    email: str = Field(default="", max_length=200)


class WebTurnRequest(BaseModel):
    session_id: str = Field(..., max_length=80)
    message: str = Field(..., max_length=1000)


class WebEndRequest(BaseModel):
    session_id: str = Field(..., max_length=80)


@dataclass
class IntentOutcome:
    reply: Optional[str] = None
    dial_to: str = ""
    end_call: bool = False


def _live_context() -> Optional[dict]:
    try:
        return integrations.get_research(allow_fetch=True)
    except Exception as exc:
        logger.warning("event=research_unavailable error=%s", _redact(exc))
        return None


def _new_session(lead_name: str, company_name: str, lead_meta: dict, to_number: str = "") -> dict:
    live = _live_context()
    return {
        "lead_name": lead_name,
        "company_name": company_name,
        "lead_meta": lead_meta,
        "to_number": to_number,
        "webhook_token": secrets.token_urlsafe(24),
        "silence_count": 0,
        "turn_count": 0,
        "touch": time.monotonic(),
        "created_at": _utc_now_iso(),
        "history": [
            {
                "role": "system",
                "content": build_system_prompt(
                    lead_name,
                    company_name,
                    team_size=lead_meta.get("team_size", ""),
                    primary_interest=lead_meta.get("primary_interest", ""),
                    live_context=live,
                ),
            }
        ],
    }


def place_outbound_call(payload: OutboundCallRequest) -> dict:
    purge_expired_sessions()
    account_sid = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
    auth_token = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
    from_number = (os.getenv("TWILIO_PHONE_NUMBER") or "").strip()
    base_url = public_base_url()

    missing = []
    if not account_sid.startswith("AC"):
        missing.append("TWILIO_ACCOUNT_SID")
    if not auth_token:
        missing.append("TWILIO_AUTH_TOKEN")
    try:
        from_number = security.normalize_phone_number(from_number)
    except ValueError:
        missing.append("TWILIO_PHONE_NUMBER (must be E.164)")
    if not base_url:
        missing.append("PUBLIC_BASE_URL")
    if missing:
        raise HTTPException(status_code=503, detail="Missing or invalid configuration: " + ", ".join(missing) + ".")

    try:
        to_number = security.normalize_phone_number(payload.to_number)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    verify_webhook_url(base_url)

    now = time.monotonic()
    with STATE_LOCK:
        recent = LAST_CALL_REQUEST.get(to_number, 0)
        if now - recent < DUPLICATE_CALL_WINDOW_SECONDS:
            raise HTTPException(status_code=409, detail="A call to this number was just initiated. Please wait before retrying.")
        if any(
            session.get("to_number") == to_number and session.get("status") in ACTIVE_CALL_STATUSES
            for session in CALL_SESSIONS.values()
        ):
            raise HTTPException(status_code=409, detail="A call to this number is already in progress.")
        LAST_CALL_REQUEST[to_number] = now

    lead_meta = {
        "team_size": payload.team_size.strip(),
        "primary_interest": payload.primary_interest.strip(),
        "email": payload.email.strip(),
    }
    lead_name = payload.lead_name.strip() or "there"
    company_name = payload.company_name.strip() or "your company"
    temp_sid = f"pending:{uuid.uuid4()}"
    session = _new_session(lead_name, company_name, lead_meta, to_number=to_number)
    session["status"] = "queued"
    record = {
        "call_sid": temp_sid,
        "channel": "phone",
        "created_at": _utc_now_iso(),
        "to_number": to_number,
        "lead_name": lead_name,
        "company_name": company_name,
        "lead_meta": lead_meta,
        "status": "queued",
        "duration": "",
        "transcript": [],
        "qualification": extract_qualification([], lead_meta),
        "exported": False,
    }
    with STATE_LOCK:
        CALL_SESSIONS[temp_sid] = session
        CALL_RECORDS[temp_sid] = record
        _persist_records()

    try:
        twilio_client = TwilioClient(account_sid, auth_token)
        call = twilio_client.calls.create(
            to=to_number,
            from_=from_number,
            url=f"{base_url}/twilio/voice?token={quote(session['webhook_token'])}",
            method="POST",
            status_callback=f"{base_url}/twilio/status",
            status_callback_event=["initiated", "ringing", "in-progress", "completed"],
        )
    except TwilioRestException as exc:
        _cleanup_pending(temp_sid, to_number)
        detail = _twilio_error_detail(exc)
        logger.error("event=twilio_call_failed code=%s status=%s error=%s", exc.code, exc.status, _redact(exc.msg))
        raise HTTPException(status_code=400, detail=detail) from exc
    except Exception as exc:
        _cleanup_pending(temp_sid, to_number)
        logger.error("event=twilio_call_failed error=%s", _redact(exc))
        raise HTTPException(status_code=502, detail=f"Twilio call request failed: {_safe_error(exc)}") from exc

    sid = str(call.sid)
    with STATE_LOCK:
        adopted = CALL_SESSIONS.pop(temp_sid, None) or CALL_SESSIONS.pop(sid, None)
        if adopted is not None:
            adopted["status"] = call.status or "queued"
            adopted["touch"] = time.monotonic()
            CALL_SESSIONS[sid] = adopted
        adopted_record = CALL_RECORDS.pop(temp_sid, None) or CALL_RECORDS.pop(sid, None)
        if adopted_record is not None:
            adopted_record["call_sid"] = sid
            adopted_record["status"] = call.status or "queued"
            CALL_RECORDS[sid] = adopted_record
            record = adopted_record
        _persist_records()

    logger.info(
        "event=call_created channel=phone sid=%s to=%s status=%s",
        sid,
        security.mask_phone(to_number),
        call.status or "queued",
    )
    return {"status": call.status or "queued", "call_sid": sid, "to": to_number}


def _cleanup_pending(temp_sid: str, to_number: str) -> None:
    with STATE_LOCK:
        CALL_SESSIONS.pop(temp_sid, None)
        CALL_RECORDS.pop(temp_sid, None)
        LAST_CALL_REQUEST.pop(to_number, None)
        _persist_records()


def _twilio_error_detail(exc: TwilioRestException) -> str:
    message = str(getattr(exc, "msg", "")) or "Twilio rejected the call."
    code = getattr(exc, "code", "")
    if code in {21219, 30007, 30023} or "not verified" in message.lower() or "trial" in message.lower():
        return (
            f"Twilio trial restriction ({code}): the recipient must be a verified number in the sign-up country. "
            "Verify this number in Twilio Console → Voice → Try out Voice, then retry."
        )
    if code in {20404, 20003} or "authenticate" in message.lower():
        return "Twilio rejected the credentials. Check TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN."
    return f"Twilio error ({code}): {_redact(message)[:220]}"


def _adopt_pending_session(call_sid: str, from_number: str) -> Optional[dict]:
    """Recover lead context if Twilio fetches the webhook before call creation returns."""
    with STATE_LOCK:
        for sid, session in list(CALL_SESSIONS.items()):
            if sid.startswith("pending:") and session.get("to_number") == from_number:
                session["touch"] = time.monotonic()
                CALL_SESSIONS[call_sid] = session
                CALL_SESSIONS.pop(sid, None)
                record = CALL_RECORDS.pop(sid, None)
                if record:
                    record["call_sid"] = call_sid
                    CALL_RECORDS[call_sid] = record
                    _persist_records()
                return session
    return None


# --- TwiML helpers ---------------------------------------------------------


def gather_twiml(text: str, *, retry: bool = False, token: str = "") -> Response:
    return twiml.gather(
        text,
        base_url=public_base_url(),
        voice=voice_name(),
        retry=retry,
        token=token,
    )


def hangup_twiml(text: str) -> Response:
    return twiml.hangup(text, voice=voice_name())


def dial_twiml(target: str, *, token: str = "") -> Response:
    try:
        caller_id = security.normalize_phone_number(os.getenv("TWILIO_PHONE_NUMBER", ""))
    except ValueError:
        caller_id = ""
    return twiml.dial(
        target,
        base_url=public_base_url(),
        voice=voice_name(),
        caller_id=caller_id,
        token=token,
    )


def session_for_webhook_token(token: str) -> Optional[dict]:
    if not token:
        return None
    for session in CALL_SESSIONS.values():
        if session.get("webhook_token") == token:
            return session
    return None


async def twilio_form(request: Request, *, expected_token: str = "") -> Dict[str, str]:
    form = await request.form()
    signed_params = {key: str(value) for key, value in form.items() if isinstance(value, str)}
    security.verify_twilio_signature(request, signed_params, fallback_token=expected_token)
    params = dict(request.query_params)
    params.update(signed_params)
    return params


# --- Calendly / handoff intent layer --------------------------------------


def _extract_email(text: str) -> str:
    match = _EMAIL_RE.search(text)
    return match.group(0) if match else ""


def _match_slot(spoken: str, slots: list[dict]) -> Optional[dict]:
    value = spoken.lower()
    ordinal = {"first": 0, "1st": 0, "one": 0, "earliest": 0, "second": 1, "2nd": 1, "two": 1, "third": 2, "3rd": 2, "three": 2}
    for word, index in ordinal.items():
        if re.search(rf"\b{word}\b", value) and index < len(slots):
            return slots[index]
    for slot in slots:
        label = slot.get("label", "").lower()
        tokens = [t for t in re.findall(r"[a-z]+|\d{1,2}:\d{2}", label) if len(t) > 3]
        if any(t in value for t in tokens):
            return slot
    return None


def handle_intents(session: dict, spoken: str, record: Optional[dict]) -> IntentOutcome:
    """Deterministic guards for real-world actions. Never books or transfers on guesswork."""
    lead_meta = session.setdefault("lead_meta", {})

    dial = handoff_number()
    if session.get("handoff_pending") and _AFFIRMATIVE_RE.search(spoken):
        session["handoff_pending"] = False
        session["handoff_dialed"] = True
        if record is not None:
            record["next_step"] = "Transferred to human"
            record["qualification"] = extract_qualification(record.get("transcript", []), lead_meta)
        return IntentOutcome(dial_to=dial) if dial else IntentOutcome(
            reply="I am sorry, live transfers are not configured right now. I can still arrange a callback for you."
        )
    if session.get("handoff_pending") and _NEGATIVE_RE.search(spoken):
        session["handoff_pending"] = False
        return IntentOutcome(reply="No problem, I am happy to continue here.")

    if _HANDOFF_INTENT_RE.search(spoken):
        if not dial:
            return IntentOutcome(reply="I can arrange for someone from Interloop Consulting to call you back.")
        session["handoff_pending"] = True
        return IntentOutcome(reply="Of course. Just to confirm, would you like me to connect you with a colleague now?")

    if not integrations.calendly_configured():
        return IntentOutcome()

    email = lead_meta.get("email") or (record or {}).get("qualification", {}).get("email", "")
    if session.get("awaiting_email"):
        found = _extract_email(spoken)
        if found:
            lead_meta["email"] = found
            session["awaiting_email"] = False
        else:
            return IntentOutcome(reply="Could you say the email address once more? For example, name at company dot com.")

    pending_booking = session.get("pending_booking")
    if pending_booking:
        if _AFFIRMATIVE_RE.search(spoken):
            try:
                booking = integrations.book_slot(
                    start_time=pending_booking["start_time"],
                    lead_name=session.get("lead_name", "Prospect"),
                    email=lead_meta.get("email", ""),
                    timezone_name=os.getenv("CALENDLY_INVITEE_TIMEZONE", "Asia/Kolkata"),
                )
            except Exception as exc:
                logger.warning("event=calendly_booking_failed error=%s", _redact(exc))
                session["pending_booking"] = None
                return IntentOutcome(reply="I could not complete the booking just now. Let me find another time for you.")
            lead_meta["booking"] = booking
            session["pending_booking"] = None
            session["booked"] = True
            if record is not None:
                record["calendly_booking"] = booking
                record["qualification"] = extract_qualification(record.get("transcript", []), lead_meta)
            return IntentOutcome(
                reply=f"Done. Your 15-minute demo is booked for {pending_booking['label']}. You will get a calendar invitation shortly."
            )
        if _NEGATIVE_RE.search(spoken):
            session["pending_booking"] = None
            return IntentOutcome(reply="No problem, I have not booked anything.")
        return IntentOutcome(reply="Please say yes to confirm the booking, or no to cancel.")

    pending_slots = session.get("pending_slots")
    if pending_slots:
        chosen = _match_slot(spoken, pending_slots)
        if chosen:
            session["pending_slots"] = None
            session["pending_booking"] = chosen
            return IntentOutcome(
                reply=f"Great, {chosen['label']}. Just to confirm, should I book that 15-minute demo for you?"
            )
        session["pending_slots"] = None
        return IntentOutcome(reply="I could not match that time. Let me know if you would like me to check again.")

    if _DEMO_INTENT_RE.search(spoken):
        if not email:
            session["awaiting_email"] = True
            return IntentOutcome(reply="Happy to set that up. What email address should I send the invitation to?")
        try:
            slots = integrations.available_slots()
        except Exception as exc:
            logger.warning("event=calendly_slots_failed error=%s", _redact(exc))
            return IntentOutcome()
        session["pending_slots"] = slots
        ordinal_words = ["one", "two", "three"]
        offer = (
            "I have "
            + ", ".join(
                f"option {ordinal_words[i] if i < len(ordinal_words) else i + 1}, {slot['label']}"
                for i, slot in enumerate(slots)
            )
            + ". Which works for you?"
        )
        return IntentOutcome(reply=offer)

    return IntentOutcome()


async def llm_reply(session: dict) -> Optional[str]:
    try:
        client, model = get_llm_client()
        return await asyncio.wait_for(
            run_in_threadpool(generate_llm_reply, client, model, session["history"]),
            timeout=llm_timeout_seconds() + 2,
        )
    except Exception as exc:
        logger.warning("event=llm_fallback error=%s", _redact(exc))
        return None


def append_turn(session: dict, role: str, text: str) -> None:
    session["history"].append({"role": role, "content": text})
    if len(session["history"]) > MAX_HISTORY_MESSAGES:
        del session["history"][1 : len(session["history"]) - MAX_HISTORY_MESSAGES + 1]


# --- Routes: pages and health ---------------------------------------------


@app.get("/")
def root_console():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health():
    llm_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("GROQ_API_KEY")
    twilio_ok = bool(
        (os.getenv("TWILIO_ACCOUNT_SID") or "").startswith("AC")
        and os.getenv("TWILIO_AUTH_TOKEN")
        and os.getenv("TWILIO_PHONE_NUMBER")
    )
    public_ok = bool(public_base_url().startswith("https://"))
    return {
        "status": "ok",
        "active_calls": len(CALL_SESSIONS),
        "active_web_sessions": len(WEB_SESSIONS),
        "recorded_calls": len(CALL_RECORDS),
        "llm_configured": bool(llm_key),
        "llm_model": os.getenv("LLM_MODEL", "gpt-4o-mini") if llm_key else "offline-kb-rules",
        "twilio_configured": twilio_ok,
        "public_base_url_configured": public_ok,
        "ready_for_phone_calls": twilio_ok and public_ok,
        "console_auth_required": security.call_token_configured(),
        "twilio_signature_validation": security.signature_validation_enabled(),
        "calendly_configured": integrations.calendly_configured(),
        "handoff_configured": bool(handoff_number()),
        "research": integrations.research_status(),
    }


@app.get("/webhook-check")
def webhook_check():
    """Cheap public probe used by the preflight check; exposes no configuration."""
    return PlainTextResponse("ok")


@app.post("/call")
async def trigger_outbound_call(request: Request, payload: OutboundCallRequest):
    security.require_call_token(request)
    security.rate_limit(request, "call", limit=10)
    return await run_in_threadpool(place_outbound_call, payload)


@app.post("/leads")
async def register_lead(request: Request, payload: LeadRegistrationRequest):
    security.rate_limit(request, "leads", limit=10)
    lead_name = payload.lead_name.strip() or "there"
    company_name = payload.company_name.strip() or "your company"
    if payload.channel == "browser":
        return _web_start(
            WebStartRequest(
                lead_name=lead_name,
                company_name=company_name,
                team_size=payload.team_size,
                primary_interest=payload.primary_interest,
                email=payload.email,
            )
        )
    security.require_call_token(request)
    if not payload.to_number.strip():
        raise HTTPException(status_code=400, detail="Phone number is required for phone calls.")
    return await run_in_threadpool(
        place_outbound_call,
        OutboundCallRequest(
            to_number=payload.to_number,
            lead_name=lead_name,
            company_name=company_name,
            team_size=payload.team_size,
            primary_interest=payload.primary_interest,
            email=payload.email,
        ),
    )


@app.get("/web/calls")
def browser_call_dashboard():
    return FileResponse(STATIC_DIR / "calls.html")


@app.post("/web/calls")
async def web_launch_calls(request: Request, payload: BatchCallRequest):
    security.require_call_token(request)
    security.rate_limit(request, "batch", limit=3)
    results = []
    for contact in payload.contacts:
        try:
            results.append(await run_in_threadpool(place_outbound_call, contact))
        except HTTPException as exc:
            results.append({"status": "error", "to": contact.to_number, "error": exc.detail})
        except Exception as exc:
            results.append({"status": "error", "to": contact.to_number, "error": _safe_error(exc)})
    return {"calls": results}


@app.get("/web/calls/status")
def web_call_status(request: Request):
    security.require_call_token(request)
    security.rate_limit(request, "status", limit=60)
    calls = list(reversed(list(CALL_RECORDS.values())))
    return {"calls": calls}


# --- Routes: Twilio --------------------------------------------------------


@app.api_route("/twilio/voice", methods=["GET", "POST"])
async def twilio_voice_entry(request: Request):
    query_token = request.query_params.get("token", "")
    expected_token = query_token if session_for_webhook_token(query_token) else ""
    params = await twilio_form(request, expected_token=expected_token)
    call_sid = params.get("CallSid", "").strip()
    if not call_sid:
        return hangup_twiml("I cannot identify this call. Goodbye!")
    speech = params.get("SpeechResult", "").strip()
    logger.info("event=twilio_voice sid=%s speech_length=%d", call_sid, len(speech))
    if speech:
        return await twilio_respond_request(request, params, call_sid, speech)

    session = CALL_SESSIONS.get(call_sid)
    if not session:
        session = _adopt_pending_session(call_sid, params.get("From", "").strip())
    if not session:
        session = _new_session("there", "your team", {})
        session["to_number"] = params.get("To", "")
    session["status"] = "in-progress"
    session["touch"] = time.monotonic()
    CALL_SESSIONS[call_sid] = session

    with STATE_LOCK:
        record = CALL_RECORDS.get(call_sid)
        if record is None:
            adopted = CALL_RECORDS.pop(f"adopted:{call_sid}", None)
            record = adopted or {
                "call_sid": call_sid,
                "channel": "phone",
                "created_at": _utc_now_iso(),
                "to_number": params.get("From", ""),
                "lead_name": session["lead_name"],
                "company_name": session["company_name"],
                "lead_meta": session.get("lead_meta", {}),
                "transcript": [],
                "exported": False,
            }
            CALL_RECORDS[call_sid] = record
        record["status"] = "in-progress"
        _persist_records()

    lead_meta = session.get("lead_meta", {})
    greeting = build_greeting(
        session["lead_name"],
        session["company_name"],
        team_size=lead_meta.get("team_size", ""),
        primary_interest=lead_meta.get("primary_interest", ""),
    )
    append_turn(session, "assistant", greeting)
    record_message(call_sid, "assistant", greeting)
    return gather_twiml(greeting, token=session.get("webhook_token", ""))


@app.api_route("/twilio/respond", methods=["GET", "POST"])
async def twilio_respond(request: Request):
    query_token = request.query_params.get("token", "")
    session = session_for_webhook_token(query_token)
    params = await twilio_form(
        request,
        expected_token=session.get("webhook_token", "") if session else "",
    )
    call_sid = params.get("CallSid", "").strip()
    if not call_sid:
        return hangup_twiml("I cannot identify this call. Goodbye!")
    return await twilio_respond_request(request, params, call_sid, params.get("SpeechResult", "").strip())


async def twilio_respond_request(request: Request, params: dict, call_sid: str, speech: str) -> Response:
    del request, params  # signature was verified by the caller
    session = CALL_SESSIONS.get(call_sid)
    if not session:
        return hangup_twiml("Thanks for your time today. Goodbye!")
    token = session.get("webhook_token", "")

    session["touch"] = time.monotonic()
    record = CALL_RECORDS.get(call_sid)
    if record is not None and record.get("status") != "completed":
        record["status"] = "in-progress"

    if not speech:
        session["silence_count"] = session.get("silence_count", 0) + 1
        if session["silence_count"] >= 2:
            closing = "It seems we have a quiet line, so I will wrap up for now. Feel free to reach out anytime. Goodbye!"
            record_message(call_sid, "assistant", closing)
            finalize_call(call_sid, "completed")
            await run_in_threadpool(export_lead_to_webhook, record or {})
            return hangup_twiml(closing)
        return gather_twiml("Sorry, I didn't quite catch that—could you repeat?", retry=True, token=token)

    session["silence_count"] = 0
    speech = speech[:MAX_SPEECH_CHARS]
    append_turn(session, "user", speech)
    record_message(call_sid, "user", speech)

    outcome = await run_in_threadpool(handle_intents, session, speech, record)
    if outcome.dial_to:
        record_message(call_sid, "assistant", "Connecting you with a colleague now.")
        return dial_twiml(outcome.dial_to, token=token)
    if outcome.reply:
        reply = sanitize_for_tts(outcome.reply)
        append_turn(session, "assistant", reply)
        record_message(call_sid, "assistant", reply)
        return gather_twiml(reply, token=token)

    response = await llm_reply(session)
    if not response:
        response = fallback_reply(speech)
    spoken = sanitize_for_tts(response)
    append_turn(session, "assistant", spoken)
    record_message(call_sid, "assistant", spoken)

    end_call = "[END_CALL]" in response or session.get("turn_count", 0) >= MAX_TURNS
    session["turn_count"] = session.get("turn_count", 0) + 1
    if end_call:
        finalize_call(call_sid, "completed")
        if record is not None:
            await run_in_threadpool(export_lead_to_webhook, record)
        return hangup_twiml(spoken)
    return gather_twiml(spoken, token=token)


@app.post("/twilio/status")
async def twilio_status(request: Request):
    query_token = request.query_params.get("token", "")
    session = session_for_webhook_token(query_token)
    params = await twilio_form(
        request,
        expected_token=session.get("webhook_token", "") if session else "",
    )
    call_sid = params.get("CallSid", "").strip()
    status = params.get("CallStatus", "").strip()
    record = CALL_RECORDS.get(call_sid) if call_sid else None
    if record is not None:
        with STATE_LOCK:
            if status:
                record["status"] = status
            if params.get("CallDuration"):
                record["duration"] = params["CallDuration"]
            _persist_records()
    if status in FINAL_CALL_STATUSES:
        finalize_call(call_sid, status)
        if record is not None and status == "completed":
            await run_in_threadpool(export_lead_to_webhook, record)
    return {"received": True}


@app.post("/twilio/handoff-status")
async def twilio_handoff_status(request: Request):
    query_token = request.query_params.get("token", "")
    session = session_for_webhook_token(query_token)
    params = await twilio_form(
        request,
        expected_token=session.get("webhook_token", "") if session else "",
    )
    call_sid = params.get("CallSid", "").strip()
    status = params.get("DialCallStatus", "").strip()
    call_sid = call_sid or params.get("ParentCallSid", "")
    if call_sid in CALL_RECORDS:
        CALL_RECORDS[call_sid]["handoff_status"] = status or "unknown"
        record_message(call_sid, "assistant", f"Human handoff result: {status or 'unknown'}")
    message = (
        "Thanks for holding. Have a great day!"
        if status in {"completed", "in-progress", "answered"}
        else "I could not connect that call right now. Someone from the team will call you back. Goodbye!"
    )
    finalize_call(call_sid, "completed")
    return hangup_twiml(message)


# --- Routes: browser demo --------------------------------------------------


@app.get("/web")
def browser_voice_demo():
    return FileResponse(STATIC_DIR / "voice.html")


def _web_start(payload: WebStartRequest) -> dict:
    purge_expired_sessions()
    lead_meta = {
        "team_size": payload.team_size.strip(),
        "primary_interest": payload.primary_interest.strip(),
        "email": payload.email.strip(),
    }
    lead_name = payload.lead_name.strip() or "Alex"
    company_name = payload.company_name.strip() or "Acme"
    greeting = build_greeting(
        lead_name,
        company_name,
        team_size=lead_meta["team_size"],
        primary_interest=lead_meta["primary_interest"],
    )
    session_id = str(uuid.uuid4())
    session = _new_session(lead_name, company_name, lead_meta)
    session["history"].append({"role": "assistant", "content": greeting})
    WEB_SESSIONS[session_id] = session
    CALL_RECORDS[session_id] = {
        "call_sid": session_id,
        "channel": "browser",
        "created_at": _utc_now_iso(),
        "to_number": "Browser Mic",
        "lead_name": lead_name,
        "company_name": company_name,
        "lead_meta": lead_meta,
        "status": "in-progress",
        "duration": "",
        "transcript": [{"role": "assistant", "text": greeting}],
        "qualification": extract_qualification([{"role": "assistant", "text": greeting}], lead_meta),
        "exported": False,
    }
    _persist_records()
    return {"session_id": session_id, "greeting": greeting}


@app.post("/web/start")
def web_start(request: Request, payload: WebStartRequest):
    security.rate_limit(request, "web_start", limit=20)
    return _web_start(payload)


@app.post("/web/turn")
async def web_turn(request: Request, payload: WebTurnRequest):
    security.rate_limit(request, "web_turn", limit=40)
    session = WEB_SESSIONS.get(payload.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Unknown web session. Start a new call.")
    message = payload.message.strip()[:MAX_SPEECH_CHARS]
    if not message:
        raise HTTPException(status_code=400, detail="Empty message.")
    session["touch"] = time.monotonic()
    append_turn(session, "user", message)
    record_message(payload.session_id, "user", message)
    record = CALL_RECORDS.get(payload.session_id)

    outcome = await run_in_threadpool(handle_intents, session, message, record)
    if outcome.reply:
        raw_reply = outcome.reply
        mode = "integrations"
    else:
        reply_text = await llm_reply(session)
        mode = os.getenv("LLM_MODEL", "gpt-4o-mini") if reply_text else "offline KB rules"
        raw_reply = reply_text or fallback_reply(message)
    reply = sanitize_for_tts(raw_reply)
    append_turn(session, "assistant", reply)
    record_message(payload.session_id, "assistant", reply)

    end_call = "[END_CALL]" in raw_reply or session.get("turn_count", 0) >= MAX_TURNS
    session["turn_count"] = session.get("turn_count", 0) + 1
    if end_call:
        WEB_SESSIONS.pop(payload.session_id, None)
        finalize_call(payload.session_id, "completed")
        if record is not None:
            export_lead_to_webhook(record)
    return {
        "reply": reply,
        "end_call": end_call,
        "mode": mode,
        "qualification": record.get("qualification") if record else None,
    }


@app.post("/web/end")
def web_end(payload: WebEndRequest):
    WEB_SESSIONS.pop(payload.session_id, None)
    finalize_call(payload.session_id, "completed")
    record = CALL_RECORDS.get(payload.session_id)
    if record is not None:
        export_lead_to_webhook(record)
    return {"ended": True}


# Restrictive by default: same-origin requests work, arbitrary origins do not.
_allowed_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
if _allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Authorization", "X-API-Token"],
    )
