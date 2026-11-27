"""Deterministic BANT and appointment extraction from call transcripts."""

from __future__ import annotations

import re

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

_TOPIC_MAP = {
    "Attendance & Shifts": ["attendance", "shift", "roster", "biometric", "geo", "timesheet"],
    "Leave Management": ["leave", "time-off", "time off", "holiday"],
    "Onboarding & Recruitment": ["onboard", "recruit", "hire", "document", "offboard"],
    "Performance & OKRs": ["performance", "okr", "appraisal", "kpi", "kra", "review"],
    "Payroll & Expense": ["payroll", "salary", "expense", "reimburse", "payslip"],
    "LMS & Training": ["lms", "learning", "training", "course"],
    "HR Help Desk": ["help desk", "helpdesk", "ticket", "grievance", "sla"],
    "Pricing & Trial": ["price", "pricing", "cost", "plan", "trial", "quote"],
}


def extract_qualification(transcript: list[dict], lead_meta: dict | None = None) -> dict:
    """Derive a structured sales qualification summary from the conversation."""
    lead_meta = lead_meta or {}
    user_lines = [m["text"] for m in transcript if m.get("role") == "user"]
    user_text = " ".join(user_lines)
    full_user_text = user_text.lower()
    all_text = " ".join(m.get("text", "") for m in transcript).lower()

    detected_size = lead_meta.get("team_size") or ""
    if not detected_size:
        size_match = re.search(r"\b(\d{1,5})\s*(?:employees?|people|staff|users?|members?|folks)\b", full_user_text)
        if size_match:
            detected_size = size_match.group(1)

    interests = [label for label, keywords in _TOPIC_MAP.items() if any(kw in full_user_text for kw in keywords)]
    if lead_meta.get("primary_interest") and lead_meta["primary_interest"] not in interests:
        interests.insert(0, lead_meta["primary_interest"])

    email_match = EMAIL_RE.search(user_text) or EMAIL_RE.search(full_user_text)
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
        r"\b(?:mon(?:day)?|tues(?:day)?|wednes(?:day)?|thurs(?:day)?|fri(?:day)?|satur(?:day)?|sun(?:day)?)\b" + time_phrase,
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

    result = {
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
    if lead_meta.get("booking"):
        result["next_step"] = "Demo booked via Calendly"
    return result
