"""Local terminal simulator and offline fallback rules for the Zoho People AI calling agent."""

import logging
import os
import re
from dotenv import load_dotenv
from prompts import build_greeting, build_system_prompt, load_knowledge_base

load_dotenv()
logger = logging.getLogger("zoho_calling_bot")


def sanitize_for_tts(text: str) -> str:
    """Remove markdown/formatting artifacts so TTS speaks cleanly."""
    cleaned = text.replace("[END_CALL]", "")
    cleaned = re.sub(r"[*_`#]+", "", cleaned)
    cleaned = re.sub(r"^\s*[-•]\s+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def generate_llm_reply(client, model: str, history: list[dict]) -> str | None:
    """Call OpenAI-compatible LLM with voice-safe timeouts and reasoning-model handling."""
    kwargs = {
        "model": model,
        "messages": history,
        "temperature": 0.6,
        "max_tokens": 160,
    }
    base_url = os.environ.get("LLM_BASE_URL", "").lower()
    extra_body = {}
    if "openrouter.ai" in base_url:
        # OpenRouter reasoning models can spend the whole token budget thinking.
        # Disable reasoning so the reply is spoken text, not hidden tokens.
        extra_body["reasoning"] = {"effort": "none"}
    elif "gpt-oss" in model.lower():
        extra_body["reasoning_effort"] = "low"
    if extra_body:
        kwargs["extra_body"] = extra_body

    try:
        completion = client.chat.completions.create(**kwargs)
    except Exception as exc:
        if extra_body:
            logger.warning("Retrying LLM call without provider extras after error: %s", exc)
            kwargs.pop("extra_body", None)
            completion = client.chat.completions.create(**kwargs)
        else:
            raise

    content = completion.choices[0].message.content if completion.choices else None
    return content.strip() if content else None


def fallback_reply(user_text: str) -> str:
    """Deterministic Zoho People KB responses when no LLM key is set or free tier is throttled."""
    text = user_text.lower()
    kb = load_knowledge_base()

    if any(w in text for w in ["bye", "goodbye", "thank you", "thanks", "that's all", "stop", "hang up", "wrap up"]):
        return (
            "Thanks so much for your time today! I will send over the 30-day free trial details "
            "and a calendar link right away. Have a great day! [END_CALL]"
        )

    if any(w in text for w in ["price", "cost", "pricing", "plan", "tier", "how much", "budget", "rupee", "inr"]):
        return (
            "Zoho People starts free for up to 5 users, with paid plans from 48 rupees per user "
            "per month on Essential HR up to 192 rupees for Enterprise billed annually, plus a 30-day free trial. "
            "Roughly how many employees are in your team?"
        )

    if any(w in text for w in ["payroll", "salary", "payslip", "expense", "reimburse", "pf", "tds"]):
        return (
            "Core Zoho People handles attendance and leave, and integrates seamlessly with Zoho Payroll "
            "for India-compliant pay runs and Zoho Expense for reimbursements, or you can bundle them in Zoho People Plus. "
            "Would you like a 15-minute live demo of how that sync works?"
        )

    if any(w in text for w in ["attendance", "shift", "roster", "leave", "time-off", "time off", "biometric", "geo", "timesheet", "clock"]):
        return (
            "Zoho People automates attendance with geo-fenced mobile check-ins, facial recognition, and biometric sync, "
            "alongside shift rosters, custom leave policies, and project timesheets. "
            "How are you tracking attendance and leave right now?"
        )

    if any(w in text for w in ["onboard", "hire", "recruit", "document", "e-sign", "offer", "offboard", "exit"]):
        return (
            "With Zoho Recruit integration, paperless onboarding workflows, e-signatures, and a central document vault, "
            "new hires complete formalities before day one, and offboarding stays equally structured. "
            "How many hires do you typically onboard in a quarter?"
        )

    if any(w in text for w in ["performance", "okr", "appraisal", "kpi", "kra", "review", "goal", "compensation"]):
        return (
            "Zoho People includes goal and OKR tracking, 360-degree appraisals, and compensation reviews "
            "so managers and HR get full visibility into team outcomes. "
            "Are you currently running reviews on spreadsheets or another tool?"
        )

    if any(w in text for w in ["lms", "learning", "training", "course", "upskill"]):
        return (
            "Our built-in Learning Management System lets you assign role-based courses, track certifications, "
            "and automate compliance training inside the same HR portal. "
            "Would you like to see how the LMS and core HR work together in a quick demo?"
        )

    if any(w in text for w in ["help desk", "helpdesk", "ticket", "grievance", "sla", "query"]):
        return (
            "Zoho People's HR Help Desk gives employees a dedicated portal to raise HR tickets with built-in SLAs "
            "and a self-service knowledge base so your HR team handles far fewer repeat emails. "
            "What is your current HR team size handling employee queries?"
        )

    if any(w in text for w in ["zia", "ai", "automat", "workflow", "integrat", "crm", "greythr", "teams", "slack", "mobile", "app"]):
        return (
            "Zoho People includes Zia AI for conversational HR queries, no-code workflow automation, iOS and Android apps, "
            "and native integrations with Zoho CRM, Microsoft Teams, Slack, and GreytHR. "
            "Which tools do you want Zoho People to connect with first?"
        )

    if any(w in text for w in ["demo", "trial", "book", "schedule", "yes", "sure", "interested", "sounds good", "send link"]):
        return (
            "Wonderful, I can set up a 15-minute personalized demo or share your 30-day free trial link right after this call. "
            "What email address should I send the invite to?"
        )

    size_match = re.search(r"\b(\d{1,5})\b", text)
    if size_match and any(w in text for w in ["employee", "people", "staff", "user", "member", "team", "headcount", "we are", "around"]):
        count = int(size_match.group(1))
        if count <= 5:
            rec = "our Free plan covers up to 5 users at zero cost"
        elif count >= 500:
            rec = "we offer tailored Enterprise pricing and dedicated onboarding for teams of 500 plus"
        else:
            rec = "our Professional or Premium plans fit teams that size really well, starting at 96 rupees per user monthly billed annually"
        return (
            f"Got it, for a team of {count}, {rec}. "
            "Which workflow matters most to you first: attendance and leave, onboarding, or performance tracking?"
        )

    return (
        f"Zoho People is trusted by {kb['social_proof']['organizations']} to manage everything from "
        "onboarding, attendance, and leave to performance OKRs, LMS, and payroll sync. "
        "Which of those areas is your biggest priority right now?"
    )


def run_simulator(lead_name: str = "Alex", company_name: str = "Acme"):
    api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("GROQ_API_KEY")
    use_llm = bool(api_key)
    client = None
    model = os.getenv("LLM_MODEL", "gpt-4o-mini")

    if use_llm:
        from openai import OpenAI
        base_url = os.getenv("LLM_BASE_URL")
        client = (
            OpenAI(api_key=api_key, base_url=base_url, timeout=10.0, max_retries=0)
            if base_url
            else OpenAI(api_key=api_key, timeout=10.0, max_retries=0)
        )
        print(f"[Simulator] Using live LLM ({model})")
    else:
        print("[Simulator] No LLM API key found—running in offline KB-rule mode. Set LLM_API_KEY / OPENAI_API_KEY / GROQ_API_KEY in .env for full AI replies.")

    history = [{"role": "system", "content": build_system_prompt(lead_name, company_name)}]
    greeting = build_greeting(lead_name, company_name)
    print(f"\nBot: {greeting}\n")
    history.append({"role": "assistant", "content": greeting})

    while True:
        user_input = input(f"{lead_name}: ").strip()
        if not user_input:
            continue
        history.append({"role": "user", "content": user_input})

        reply = None
        if use_llm and client is not None:
            try:
                reply = generate_llm_reply(client, model, history)
            except Exception as exc:
                print(f"[Simulator LLM fallback: {exc}]")
        if not reply:
            reply = fallback_reply(user_input)

        end_call = "[END_CALL]" in reply
        spoken = sanitize_for_tts(reply)
        print(f"\nBot: {spoken}\n")
        history.append({"role": "assistant", "content": spoken})

        if end_call:
            print("[Call ended]")
            break


if __name__ == "__main__":
    run_simulator()
