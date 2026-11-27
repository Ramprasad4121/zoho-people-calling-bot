"""In-process call state and append-only transcript snapshots."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

import integrations
import security
from qualification import extract_qualification

logger = logging.getLogger("zoho_calling_bot.state")

DATA_DIR = Path(__file__).parent / "data"
CALL_HISTORY_PATH = DATA_DIR / "call_history.json"

CALL_SESSIONS: Dict[str, dict] = {}
CALL_RECORDS: Dict[str, dict] = {}
WEB_SESSIONS: Dict[str, dict] = {}
LAST_CALL_REQUEST: Dict[str, float] = {}

STATE_LOCK = threading.RLock()
MAX_RECORDS = 100
MAX_TRANSCRIPT_MESSAGES = 120
SESSION_TTL_SECONDS = 1800


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_saved_records() -> None:
    if not CALL_HISTORY_PATH.exists():
        return
    try:
        data = json.loads(CALL_HISTORY_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            for rec in data[-MAX_RECORDS:]:
                if isinstance(rec, dict) and rec.get("call_sid"):
                    CALL_RECORDS[str(rec["call_sid"])] = rec
    except Exception as exc:
        logger.warning("event=history_load_failed error=%s", security.redact(exc))


def persist_records() -> None:
    """Atomic-ish snapshot write under a lock; never blocks on external I/O."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        records = list(CALL_RECORDS.values())[-MAX_RECORDS:]
        tmp_path = CALL_HISTORY_PATH.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(records, indent=2), encoding="utf-8")
        tmp_path.replace(CALL_HISTORY_PATH)
    except Exception as exc:
        logger.warning("event=history_write_failed error=%s", security.redact(exc))


def export_lead_to_webhook(record: dict) -> None:
    """Post a lead record once. Never raises, never blocks on retries."""
    if record.get("exported"):
        return
    record["exported"] = True
    integrations.post_lead_webhook(record)


def record_message(call_sid: str, role: str, text: str) -> None:
    with STATE_LOCK:
        record = CALL_RECORDS.get(call_sid)
        if record is None:
            return
        transcript = record.setdefault("transcript", [])
        transcript.append({"role": role, "text": text[:2000]})
        if len(transcript) > MAX_TRANSCRIPT_MESSAGES:
            del transcript[:-MAX_TRANSCRIPT_MESSAGES]
        record["qualification"] = extract_qualification(transcript, record.get("lead_meta"))
        persist_records()


def purge_expired_sessions() -> None:
    now = time.monotonic()
    with STATE_LOCK:
        for store in (CALL_SESSIONS, WEB_SESSIONS):
            for sid in [sid for sid, session in store.items() if now - session.get("touch", now) > SESSION_TTL_SECONDS]:
                store.pop(sid, None)
        for number, stamp in list(LAST_CALL_REQUEST.items()):
            if now - stamp > SESSION_TTL_SECONDS:
                LAST_CALL_REQUEST.pop(number, None)


def finalize_call(call_sid: str, status: str = "completed") -> None:
    with STATE_LOCK:
        CALL_SESSIONS.pop(call_sid, None)
        record = CALL_RECORDS.get(call_sid)
        if record is not None:
            record["status"] = status
            record["completed_at"] = utc_now_iso()
            persist_records()


load_saved_records()
