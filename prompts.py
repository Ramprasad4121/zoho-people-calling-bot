"""System prompt and greetings for the Zoho People voice agent."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

KB_PATH = Path(__file__).parent / "zoho_people_kb.json"


def load_knowledge_base() -> dict:
    with KB_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def _live_context_block(live_context: Optional[Dict[str, str]]) -> str:
    if not live_context:
        return ""
    parts = []
    for key in ("zoho_features", "zoho_pricing", "interloop"):
        snippet = (live_context.get(key) or "").strip()
        if snippet:
            parts.append(f"- {key.replace('_', ' ')}: {snippet[:400]}")
    if not parts:
        return ""
    return (
        "\nLIVE OFFICIAL-SITE NOTES (fetched from zoho.com and interloopconsulting.com; "
        "use only facts that are actually stated here, otherwise fall back to the knowledge base):\n"
        + "\n".join(parts)
        + "\n"
    )


def build_system_prompt(
    lead_name: str = "there",
    company_name: str = "your company",
    team_size: str = "",
    primary_interest: str = "",
    live_context: Optional[Dict[str, str]] = None,
) -> str:
    kb = load_knowledge_base()
    kb_summary_lines = []
    for module in kb["modules"]:
        features_str = "; ".join(f"{feat['name']}: {feat['summary']}" for feat in module["features"])
        kb_summary_lines.append(f"- {module['category']}: {features_str}")
    kb_text = "\n".join(kb_summary_lines)

    pricing = kb.get("pricing_india", {})
    plan_lines = [
        f"- {p['name']}: Rs {p['annual_per_user_month']}/user/month billed annually "
        f"(Rs {p['monthly_per_user_month']} monthly). {p['highlights']}"
        for p in pricing.get("plans", [])
    ]
    pricing_text = "\n".join(plan_lines)

    interloop = kb.get("interloop", {})
    interloop_text = (
        f"{interloop.get('description', '')} {interloop.get('credential', '')}".strip()
        or "Interloop Consulting is a Zoho implementation partner."
    )

    lead_context_parts = []
    if team_size:
        lead_context_parts.append(f"Reported team size: {team_size}.")
    if primary_interest:
        lead_context_parts.append(f"Primary HR interest noted on registration: {primary_interest}.")
    lead_context = " ".join(lead_context_parts)
    if lead_context:
        lead_context = (
            f"\nLEAD REGISTRATION CONTEXT: {lead_context} "
            "Acknowledge this naturally without asking them to repeat it."
        )

    return f"""You are Aria, an AI sales assistant calling on behalf of {interloop_text}
You are speaking with {lead_name} from {company_name}, who registered interest in Zoho People.{lead_context}

STRICT VOICE CALL RULES:
1. You are an AI assistant. If asked, say so clearly and immediately. Never imply you are a human.
2. Speak naturally in complete sentences. Keep every reply under 45 words and never use bullets, markdown, asterisks, emojis, or numbered lists; your words are spoken aloud.
3. Ask only one question at a time and wait for the answer. Listen before pitching. Reflect the prospect's own words back to them.
4. Qualify with BANT: budget, decision maker and approvers, need and top HR pain point, and timeline. Do not interrogate; let it feel conversational.
5. Pitch only the one or two Zoho People modules that solve the need they described. Use the knowledge base below as your source of truth.
6. Do not invent features, integrations, discounts, pricing, availability, or promises. For pricing, use only the India plan data below, including the 30-day free trial, the Free plan for up to 5 users, separate Zoho Payroll integration, and custom quotes for 500+ users. If you do not know, say you will confirm and follow up.
7. Never claim that a demo was booked, a link was sent, or a colleague was connected unless the system has actually done it. The system handles real Calendly bookings and live transfers; you only explain them.
8. If the prospect wants a human, say you can connect them now and keep your reply short. The transfer is handled after they confirm.
9. Once you understand the need, propose a 15-minute personalized live demo or a 30-day free trial. Ask for an email address when one is needed. The system will offer real available times.
10. If the prospect says goodbye, asks to end the call, or clearly wraps up, thank them and put [END_CALL] at the very end of your reply.

PRODUCT KNOWLEDGE ({kb['product_name']}, {kb['source_url']}):
Trusted by {kb['social_proof']['users']}, {kb['social_proof']['organizations']} across {kb['social_proof']['countries']}.
{kb_text}

INDIA PRICING ({pricing.get('free_trial', '')}, {pricing.get('tax_note', '')}, {pricing.get('payroll_note', '')}):
{pricing_text}
Source: {pricing.get('source_url', kb['source_url'])}
{_live_context_block(live_context)}"""


def build_greeting(
    lead_name: str = "there",
    company_name: str = "",
    team_size: str = "",
    primary_interest: str = "",
) -> str:
    disclosure = (
        f"Hi {lead_name}, this is Aria, an AI sales assistant calling from Interloop Consulting "
        "about Zoho People."
    )
    if primary_interest:
        return (
            f"{disclosure} Thanks for registering your interest in {primary_interest}. "
            "Before I point you to the right setup, what is the biggest bottleneck your HR team is facing with that right now?"
        )
    if company_name and company_name not in ("your team", "your company"):
        return (
            f"{disclosure} I saw that {company_name} registered interest with us. "
            "I would love to learn a bit about your HR setup. What is the biggest challenge your team is trying to solve right now?"
        )
    return (
        f"{disclosure} Thanks for registering your interest with us. "
        "I would love to learn a bit about your HR setup. What is the biggest challenge your team is trying to solve right now?"
    )
