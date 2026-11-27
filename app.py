import json
import logging
import os
import re
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import FileResponse, Response
from openai import OpenAI
from pydantic import BaseModel
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import Gather, VoiceResponse

from local_demo import fallback_reply, generate_llm_reply, sanitize_for_tts
from prompts import build_greeting, build_system_prompt

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("zoho_calling_bot")

app = FastAPI(title="Zoho People AI Calling Bot")

DATA_DIR = Path(__file__).parent / "data"
CALL_HISTORY_PATH = DATA_DIR / "call_history.json"
STATIC_DIR = Path(__file__).parent / "static"

# In-memory call session store (backed by JSON history snapshot for inspection)
CALL_SESSIONS: Dict[str, dict] = {}
CALL_RECORDS: Dict[str, dict] = {}
WEB_SESSIONS: Dict[str, dict] = {}

FINAL_CALL_STATUSES = {"completed", "failed", "busy", "no-answer", "canceled"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_saved_records() -> None:
    if not CALL_HISTORY_PATH.exists():
        return
    try:
        data = json.loads(CALL_HISTORY_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            for rec in data:
                sid = rec.get("call_sid")
                if sid:
                    CALL_RECORDS[sid] = rec
    except Exception as exc:
        logger.warning("Could not load saved call history: %s", exc)


def _persist_records() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        records = list(CALL_RECORDS.values())[-100:]
        CALL_HISTORY_PATH.write_text(json.dumps(records, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not persist call history: %s", exc)


_load_saved_records()


def get_llm_client() -> tuple[OpenAI, str]:
    """Supports OpenAI or any OpenAI-compatible provider (e.g. Groq, Ollama)."""
    api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("Missing LLM_API_KEY / OPENAI_API_KEY / GROQ_API_KEY in environment.")
    base_url = os.getenv("LLM_BASE_URL") or None
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")
    if base_url:
        logger.info("Using LLM base_url=%s model=%s", base_url, model)
    client = (
        OpenAI(api_key=api_key, base_url=base_url, timeout=20.0, max_retries=0)
        if base_url
        else OpenAI(api_key=api_key, timeout=20.0, max_retries=0)
    )
    return client, model


def extract_qualification(transcript: list[dict], lead_meta: dict | None = None) -> dict:
    """Derive a structured sales qualification summary from the conversation transcript."""
    lead_meta = lead_meta or {}
    user_lines = [m["text"] for m in transcript if m.get("role") == "user"]
    user_text = " ".join(user_lines)
    full_user_text = user_text.lower()
    all_text = " ".join(m.get("text", "") for m in transcript).lower()

    detected_size = lead_meta.get("team_size") or ""
    if not detected_size:
        size_match = re.search(
            r"\b(\d{1,5})\s*(?:employees?|people|staff|users?|members?|folks)\b",
            full_user_text,
        )
        if size_match:
            detected_size = size_match.group(1)

    topic_map = {
        "Attendance & Shifts": ["attendance", "shift", "roster", "biometric", "geo", "timesheet"],
        "Leave Management": ["leave", "time-off", "time off", "holiday"],
        "Onboarding & Recruitment": ["onboard", "recruit", "hire", "document", "offboard"],
        "Performance & OKRs": ["performance", "okr", "appraisal", "kpi", "kra", "review"],
        "Payroll & Expense": ["payroll", "salary", "expense", "reimburse", "payslip"],
        "LMS & Training": ["lms", "learning", "training", "course"],
        "HR Help Desk": ["help desk", "helpdesk", "ticket", "grievance", "sla"],
        "Pricing & Trial": ["price", "pricing", "cost", "plan", "trial", "quote"],
    }
    interests = [
        label for label, keywords in topic_map.items() if any(kw in full_user_text for kw in keywords)
    ]
    if lead_meta.get("primary_interest") and lead_meta["primary_interest"] not in interests:
        interests.insert(0, lead_meta["primary_interest"])

    email_match = re.search(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", full_user_text)
    email = email_match.group(0) if email_match else lead_meta.get("email", "")

    next_step = "Needs follow-up"
    if any(w in all_text for w in ["demo", "schedule", "15-minute", "calendar"]):
        next_step = "Live demo discussed"
    if any(w in all_text for w in ["free trial", "trial link"]):
        next_step = "Free trial / Demo follow-up"
    if any(w in full_user_text for w in ["not interested", "no thanks", "don't need"]):
        next_step = "Not interested"

    budget_match = re.search(
        r"\b(?:budget|spend|spending|afford|price range)\b[^.!?]{0,40}"
        r"(?:(?:rs\.?|inr|₹|rupees?)\s*)?\d[\d,]*(?:\.\d+)?\s*"
        r"(?:lakh|lac|lakhs|lacs|crore|crores|cr|thousand|k)?",
        user_text,
        re.IGNORECASE,
    )
    if not budget_match:
        budget_match = re.search(
            r"(?:(?:rs\.?|inr|₹|rupees?)\s*\d[\d,]*(?:\.\d+)?"
            r"|\d[\d,]*(?:\.\d+)?\s*(?:lakh|lac|lakhs|lacs|crore|crores|cr))\b",
            user_text,
            re.IGNORECASE,
        )
    budget = budget_match.group(0).strip() if budget_match else ""

    authority = ""
    if re.search(
        r"\b(?:i(?:'m| am)|we(?:'re| are))\s+(?:the\s+)?(?:final\s+)?decision[- ]?mak(?:er|ers)\b"
        r"|\b(?:i|we)\s+(?:make|own|approve|sign off on)\s+(?:the\s+)?(?:final\s+)?(?:decision|call|budget|purchase)\b"
        r"|\b(?:i|we)\s+(?:can|get to)\s+(?:decide|approve)\b"
        r"|\b(?:i|we)\s+decide\b"
        r"|\bdecision is (?:mine|ours)\b",
        full_user_text,
    ):
        authority = "Decision maker"
    elif re.search(
        r"\b(?:need|needs|need to|have to|get|getting|requires?|wait for)\s+(?:to\s+)?"
        r"(?:approval|approve|sign[- ]?off|buy[- ]?in|manager|boss|founder|ceo|cfo|finance|hr head|leadership|committee)\b"
        r"|\b(?:check|clear|run it by)\s+(?:with\s+)?(?:my|our)\s+[a-z]+\b"
        r"|\b(?:my|our)\s+(?:manager|boss|ceo|founder|finance team|hr head|leadership)\s+"
        r"(?:approves?|decides?|signs? off)\b",
        full_user_text,
    ):
        authority = "Needs approval"

    timeline = ""
    for pattern, label in (
        (r"\basap\b|\bas soon as possible\b|\bimmediately\b|\burgently\b|\bright away\b|\bthis week\b", "ASAP"),
        (r"\bthis month\b|\bby (?:the )?end of (?:this )?month\b", "this month"),
        (r"\bnext month\b", "next month"),
        (r"\bthis quarter\b|\bby (?:the )?end of (?:this )?quarter\b", "this quarter"),
        (r"\bnext quarter\b", "next quarter"),
        (r"\b(?:in|within|over)\s+(?:the\s+)?next\s+\d+\s+(?:days|weeks|months)\b", ""),
    ):
        match = re.search(pattern, full_user_text)
        if match:
            timeline = label or match.group(0)
            break

    time_phrase = (
        r"(?:\s+(?:at|around|by|for)?\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)"
        r"|\s+(?:morning|afternoon|evening))?"
    )
    appointment = ""
    for pattern in (
        r"\b(?:mon(?:day)?|tues(?:day)?|wednes(?:day)?|thurs(?:day)?|fri(?:day)?|satur(?:day)?|sun(?:day)?)\b"
        + time_phrase,
        r"\b(?:today|tomorrow|next week)\b" + time_phrase,
        r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
        r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+"
        r"\d{1,2}(?:st|nd|rd|th)?\b" + time_phrase,
        r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b",
    ):
        match = re.search(pattern, user_text, re.IGNORECASE)
        if match:
            appointment = match.group(0).strip()
            break

    return {
        "team_size": detected_size or "Not captured yet",
        "topics": interests,
        "email": email,
        "next_step": next_step,
        "turns": len(user_lines),
        "budget": budget,
        "authority": authority,
        "timeline": timeline,
        "appointment": appointment,
    }


def export_lead_to_webhook(record: dict) -> None:
    """Post a lead record once, if a webhook URL is configured. Never raises."""
    if record.get("exported"):
        return
    url = os.getenv("SALES_WEBHOOK_URL", "").strip()
    if not url:
        return

    record["exported"] = True
    try:
        request = urllib.request.Request(
            url,
            data=json.dumps(record).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            response.read()
    except Exception as exc:
        logger.warning("Lead webhook export failed: %s", exc)


class OutboundCallRequest(BaseModel):
    to_number: str
    lead_name: str = "Alex"
    company_name: str = "Acme"
    team_size: str = ""
    primary_interest: str = ""
    email: str = ""


class BatchCallRequest(BaseModel):
    contacts: List[OutboundCallRequest]


class LeadRegistrationRequest(BaseModel):
    lead_name: str
    company_name: str = "Acme"
    to_number: str = ""
    team_size: str = ""
    primary_interest: str = ""
    email: str = ""
    channel: str = "phone"  # "phone" or "browser"


@app.get("/")
def root_console():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health():
    llm_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("GROQ_API_KEY")
    twilio_ok = all(
        os.getenv(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER")
    )
    public_url = (os.getenv("PUBLIC_BASE_URL") or "").strip()
    public_ok = public_url.startswith("https://")
    llm_base = (os.getenv("LLM_BASE_URL") or "").strip()
    return {
        "status": "ok",
        "active_calls": len(CALL_SESSIONS),
        "active_web_sessions": len(WEB_SESSIONS),
        "recorded_calls": len(CALL_RECORDS),
        "llm_configured": bool(llm_key),
        "llm_model": os.getenv("LLM_MODEL", "gpt-4o-mini") if llm_key else "offline-kb-rules",
        "llm_base_url": llm_base if llm_key else "",
        "twilio_configured": twilio_ok,
        "public_base_url_configured": public_ok,
        "public_base_url": public_url if public_ok else "",
        "ready_for_phone_calls": twilio_ok and public_ok,
    }


def normalize_phone_number(number: str) -> str:
    """Twilio expects E.164, e.g. +15551234567."""
    cleaned = number.strip().replace(" ", "").replace("-", "")
    if not cleaned.startswith("+") or not cleaned[1:].isdigit() or not 8 <= len(cleaned) <= 16:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid number {number!r}; use E.164 format like +15551234567.",
        )
    return cleaned


def place_outbound_call(payload: OutboundCallRequest) -> dict:
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_PHONE_NUMBER")
    public_url = os.getenv("PUBLIC_BASE_URL")

    if not all([account_sid, auth_token, from_number, public_url]):
        raise HTTPException(
            status_code=500,
            detail="Missing Twilio or PUBLIC_BASE_URL environment variables.",
        )

    to_number = normalize_phone_number(payload.to_number)
    twilio_client = TwilioClient(account_sid, auth_token)
    base_url = public_url.rstrip("/")

    try:
        call = twilio_client.calls.create(
            to=to_number,
            from_=from_number,
            url=f"{base_url}/twilio/voice",
            status_callback=f"{base_url}/twilio/status",
            status_callback_event=["initiated", "ringing", "in-progress", "completed"],
        )
    except TwilioRestException as exc:
        logger.error("Twilio call creation failed for %s: %s", to_number, exc.msg)
        raise HTTPException(status_code=400, detail=f"Twilio error ({exc.code}): {exc.msg}") from exc

    lead_meta = {
        "team_size": payload.team_size.strip(),
        "primary_interest": payload.primary_interest.strip(),
        "email": payload.email.strip(),
    }
    CALL_SESSIONS[call.sid] = {
        "lead_name": payload.lead_name,
        "company_name": payload.company_name,
        "lead_meta": lead_meta,
        "silence_count": 0,
        "history": [
            {
                "role": "system",
                "content": build_system_prompt(
                    payload.lead_name,
                    payload.company_name,
                    team_size=lead_meta["team_size"],
                    primary_interest=lead_meta["primary_interest"],
                ),
            }
        ],
    }
    CALL_RECORDS[call.sid] = {
        "call_sid": call.sid,
        "channel": "phone",
        "created_at": _utc_now_iso(),
        "to_number": to_number,
        "lead_name": payload.lead_name,
        "company_name": payload.company_name,
        "lead_meta": lead_meta,
        "status": call.status or "queued",
        "duration": "",
        "transcript": [],
        "qualification": extract_qualification([], lead_meta),
        "exported": False,
    }
    _persist_records()

    return {"status": call.status or "queued", "call_sid": call.sid, "to": to_number}


def record_message(call_sid: str, role: str, text: str) -> None:
    record = CALL_RECORDS.get(call_sid)
    if record is not None:
        record["transcript"].append({"role": role, "text": text})
        record["qualification"] = extract_qualification(record["transcript"], record.get("lead_meta"))
        _persist_records()


def public_base_url() -> str:
    """Absolute URLs are required for TwiML action URLs on trial accounts."""
    return os.getenv("PUBLIC_BASE_URL", "").rstrip("/")


@app.post("/call")
def trigger_outbound_call(payload: OutboundCallRequest):
    return place_outbound_call(payload)


@app.post("/leads")
def register_lead(payload: LeadRegistrationRequest):
    """Lead registration endpoint: saves lead context and either dials via Twilio or starts a browser session."""
    lead_name = payload.lead_name.strip() or "there"
    company_name = payload.company_name.strip() or "your company"

    if payload.channel == "browser":
        return web_start(
            WebStartRequest(
                lead_name=lead_name,
                company_name=company_name,
                team_size=payload.team_size,
                primary_interest=payload.primary_interest,
                email=payload.email,
            )
        )

    if not payload.to_number.strip():
        raise HTTPException(status_code=400, detail="Phone number is required for phone calls.")

    return place_outbound_call(
        OutboundCallRequest(
            to_number=payload.to_number,
            lead_name=lead_name,
            company_name=company_name,
            team_size=payload.team_size,
            primary_interest=payload.primary_interest,
            email=payload.email,
        )
    )


# --- Web dashboard for launching several phone calls ---

@app.get("/web/calls")
def browser_call_dashboard():
    return FileResponse(STATIC_DIR / "calls.html")


@app.post("/web/calls")
def web_launch_calls(payload: BatchCallRequest):
    if not payload.contacts:
        raise HTTPException(status_code=400, detail="Add at least one phone number.")
    if len(payload.contacts) > 20:
        raise HTTPException(status_code=400, detail="Maximum 20 numbers per batch.")

    results = []
    for contact in payload.contacts:
        try:
            results.append(place_outbound_call(contact))
        except HTTPException as exc:
            results.append({"status": "error", "to": contact.to_number, "error": exc.detail})
        except Exception as exc:
            results.append({"status": "error", "to": contact.to_number, "error": str(exc)})
    return {"calls": results}


@app.get("/web/calls/status")
def web_call_status():
    calls = list(reversed(list(CALL_RECORDS.values())))
    return {"calls": calls}


@app.post("/twilio/voice")
async def twilio_voice_entry(
    CallSid: str = Form(...),
    SpeechResult: str = Form(default=""),
):
    logger.info("Twilio webhook: sid=%s speech=%r", CallSid, SpeechResult[:80])
    if SpeechResult.strip():
        return await twilio_respond(CallSid=CallSid, SpeechResult=SpeechResult)

    session = CALL_SESSIONS.setdefault(
        CallSid,
        {
            "lead_name": "there",
            "company_name": "your team",
            "lead_meta": {},
            "silence_count": 0,
            "history": [{"role": "system", "content": build_system_prompt()}],
        },
    )

    lead_meta = session.get("lead_meta", {})
    greeting = build_greeting(
        session["lead_name"],
        session["company_name"],
        team_size=lead_meta.get("team_size", ""),
        primary_interest=lead_meta.get("primary_interest", ""),
    )
    session["history"].append({"role": "assistant", "content": greeting})
    record = CALL_RECORDS.get(CallSid)
    if record is not None:
        record["status"] = "in-progress"
    record_message(CallSid, "assistant", greeting)

    vr = VoiceResponse()
    base = public_base_url()
    action_url = f"{base}/twilio/respond" if base else "/twilio/respond"
    gather = Gather(
        input="speech",
        method="POST",
        action=action_url,
        speech_timeout="auto",
        timeout=6,
        language="en-IN",
    )
    gather.say(greeting, voice="Polly.Aditi")
    vr.append(gather)
    vr.say("I didn't catch that. Feel free to reach out anytime. Goodbye!", voice="Polly.Aditi")
    return Response(content=str(vr), media_type="application/xml")


@app.post("/twilio/respond")
async def twilio_respond(
    CallSid: str = Form(...),
    SpeechResult: str = Form(default=""),
):
    session = CALL_SESSIONS.get(CallSid)
    if not session:
        vr = VoiceResponse()
        vr.say("Thanks for your time today. Goodbye!", voice="Polly.Aditi")
        vr.hangup()
        return Response(content=str(vr), media_type="application/xml")

    vr = VoiceResponse()
    spoken_in = SpeechResult.strip()

    if not spoken_in:
        session["silence_count"] = session.get("silence_count", 0) + 1
        if session["silence_count"] >= 2:
            closing = "It seems we have a quiet line, so I will wrap up for now. Feel free to reach out anytime. Goodbye!"
            record_message(CallSid, "assistant", closing)
            vr.say(closing, voice="Polly.Aditi")
            vr.hangup()
            CALL_SESSIONS.pop(CallSid, None)
            return Response(content=str(vr), media_type="application/xml")

        base = public_base_url()
        action_url = f"{base}/twilio/respond" if base else "/twilio/respond"
        gather = Gather(
            input="speech",
            method="POST",
            action=action_url,
            speech_timeout="auto",
            timeout=6,
            language="en-IN",
        )
        gather.say("Sorry, I didn't quite catch that—could you repeat?", voice="Polly.Aditi")
        vr.append(gather)
        vr.say("I still didn't catch that. Feel free to reach out anytime. Goodbye!", voice="Polly.Aditi")
        return Response(content=str(vr), media_type="application/xml")

    session["silence_count"] = 0
    session["history"].append({"role": "user", "content": spoken_in})
    record_message(CallSid, "user", spoken_in)

    reply = None
    try:
        client, model = get_llm_client()
        reply = generate_llm_reply(client, model, session["history"])
    except Exception as exc:
        logger.warning("Twilio LLM fallback triggered for %s: %s", CallSid, exc)

    if not reply:
        reply = fallback_reply(spoken_in)

    end_call = "[END_CALL]" in reply
    spoken_reply = sanitize_for_tts(reply)
    session["history"].append({"role": "assistant", "content": spoken_reply})
    record_message(CallSid, "assistant", spoken_reply)

    if end_call:
        vr.say(spoken_reply, voice="Polly.Aditi")
        vr.hangup()
        CALL_SESSIONS.pop(CallSid, None)
    else:
        base = public_base_url()
        action_url = f"{base}/twilio/respond" if base else "/twilio/respond"
        gather = Gather(
            input="speech",
            method="POST",
            action=action_url,
            speech_timeout="auto",
            timeout=6,
            language="en-IN",
        )
        gather.say(spoken_reply, voice="Polly.Aditi")
        vr.append(gather)
        vr.say("I didn't catch that. Feel free to reach out anytime. Goodbye!", voice="Polly.Aditi")

    return Response(content=str(vr), media_type="application/xml")


@app.post("/twilio/status")
async def twilio_status(
    CallSid: str = Form(...),
    CallStatus: str = Form(default=""),
    CallDuration: str = Form(default=""),
):
    record = CALL_RECORDS.get(CallSid)
    if record is not None:
        if CallStatus:
            record["status"] = CallStatus
        if CallDuration:
            record["duration"] = CallDuration
        _persist_records()
    if CallStatus in FINAL_CALL_STATUSES:
        CALL_SESSIONS.pop(CallSid, None)
    return {"received": True}


# --- 100% free browser voice demo (no Twilio, no phone number) ---

class WebStartRequest(BaseModel):
    lead_name: str = "Alex"
    company_name: str = "Acme"
    team_size: str = ""
    primary_interest: str = ""
    email: str = ""


class WebTurnRequest(BaseModel):
    session_id: str
    message: str


class WebEndRequest(BaseModel):
    session_id: str


@app.get("/web")
def browser_voice_demo():
    return FileResponse(STATIC_DIR / "voice.html")


@app.post("/web/start")
def web_start(payload: WebStartRequest):
    lead_meta = {
        "team_size": payload.team_size.strip(),
        "primary_interest": payload.primary_interest.strip(),
        "email": payload.email.strip(),
    }
    greeting = build_greeting(
        payload.lead_name,
        payload.company_name,
        team_size=lead_meta["team_size"],
        primary_interest=lead_meta["primary_interest"],
    )
    session_id = str(uuid.uuid4())
    WEB_SESSIONS[session_id] = {
        "lead_name": payload.lead_name,
        "company_name": payload.company_name,
        "lead_meta": lead_meta,
        "history": [
            {
                "role": "system",
                "content": build_system_prompt(
                    payload.lead_name,
                    payload.company_name,
                    team_size=lead_meta["team_size"],
                    primary_interest=lead_meta["primary_interest"],
                ),
            },
            {"role": "assistant", "content": greeting},
        ],
    }
    CALL_RECORDS[session_id] = {
        "call_sid": session_id,
        "channel": "browser",
        "created_at": _utc_now_iso(),
        "to_number": "Browser Mic",
        "lead_name": payload.lead_name,
        "company_name": payload.company_name,
        "lead_meta": lead_meta,
        "status": "in-progress",
        "duration": "",
        "transcript": [{"role": "assistant", "text": greeting}],
        "qualification": extract_qualification([{"role": "assistant", "text": greeting}], lead_meta),
        "exported": False,
    }
    _persist_records()
    return {"session_id": session_id, "greeting": greeting}


@app.post("/web/turn")
def web_turn(payload: WebTurnRequest):
    session = WEB_SESSIONS.get(payload.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Unknown web session. Start a new call.")
    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Empty message.")

    session["history"].append({"role": "user", "content": message})
    record_message(payload.session_id, "user", message)

    reply_text = None
    mode = None
    try:
        client, model = get_llm_client()
    except RuntimeError:
        pass
    else:
        try:
            reply_text = generate_llm_reply(client, model, session["history"])
            if reply_text:
                mode = model
        except Exception as exc:
            logger.warning("Web LLM fallback triggered for %s: %s", payload.session_id, exc)

    if not reply_text:
        reply_text = fallback_reply(message)
        mode = "offline KB rules"

    end_call = "[END_CALL]" in reply_text
    spoken = sanitize_for_tts(reply_text)
    session["history"].append({"role": "assistant", "content": spoken})
    record_message(payload.session_id, "assistant", spoken)

    record = CALL_RECORDS.get(payload.session_id)
    if end_call:
        WEB_SESSIONS.pop(payload.session_id, None)
        if record is not None:
            record["status"] = "completed"
            export_lead_to_webhook(record)
            _persist_records()

    return {
        "reply": spoken,
        "end_call": end_call,
        "mode": mode,
        "qualification": record.get("qualification") if record else None,
    }


@app.post("/web/end")
def web_end(payload: WebEndRequest):
    WEB_SESSIONS.pop(payload.session_id, None)
    record = CALL_RECORDS.get(payload.session_id)
    if record is not None:
        if record.get("status") == "in-progress":
            record["status"] = "completed"
        export_lead_to_webhook(record)
        _persist_records()
    return {"ended": True}
