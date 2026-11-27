import json
from pathlib import Path

KB_PATH = Path(__file__).parent / "zoho_people_kb.json"


def load_knowledge_base() -> dict:
    with KB_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def build_system_prompt(
    lead_name: str = "there",
    company_name: str = "your company",
    team_size: str = "",
    primary_interest: str = "",
) -> str:
    kb = load_knowledge_base()
    kb_summary_lines = []
    for module in kb["modules"]:
        features_str = "; ".join(
            f"{feat['name']}: {feat['summary']}" for feat in module["features"]
        )
        kb_summary_lines.append(f"- {module['category']}: {features_str}")
    kb_text = "\n".join(kb_summary_lines)

    pricing = kb.get("pricing_india", {})
    plan_lines = [
        f"- {p['name']}: Rs {p['annual_per_user_month']}/user/month billed annually "
        f"(Rs {p['monthly_per_user_month']} monthly). {p['highlights']}"
        for p in pricing.get("plans", [])
    ]
    pricing_text = "\n".join(plan_lines)

    lead_context_parts = []
    if team_size:
        lead_context_parts.append(f"Reported team size: {team_size}.")
    if primary_interest:
        lead_context_parts.append(f"Primary HR interest noted on registration: {primary_interest}.")
    lead_context = " ".join(lead_context_parts)
    if lead_context:
        lead_context = f"\nLEAD REGISTRATION CONTEXT: {lead_context} Acknowledge this naturally without asking them to repeat it."

    return f"""You are Aria, a friendly, sharp sales specialist calling on behalf of Zoho People.
You are speaking with {lead_name} from {company_name}, who recently registered interest in Zoho People.{lead_context}

STRICT VOICE CALL RULES:
1. Speak naturally. Keep every reply under 45 words and use complete sentences only.
2. Never use bullet points, markdown, asterisks, emojis, or numbered lists. Your text is spoken aloud by Text-to-Speech.
3. Ask only one question at a time. Wait for the answer before you ask another.
4. Qualify with BANT. Find their budget, who makes the decision and who must approve it, their need and top HR pain point, and their timeline. Repeat their own words back when they answer.
5. Pitch only the one or two Zoho People modules that solve the need they described. The modules are listed below.
6. Answer pricing objections only from the India pricing data below. Quote rupee amounts and the 30-day free trial. Mention the free plan for up to 5 users, the separate Zoho Payroll integration, and custom quotes for 500 plus users. Never invent discounts.
7. Once you know the need, close by proposing a 15-minute personalized live demo or a 30-day free trial. If they pick the demo, ask for a date and time and repeat it back.
8. If the prospect says goodbye, asks to end the call, or confirms the next step and wraps up, thank them and put [END_CALL] at the very end of your reply.

PRODUCT KNOWLEDGE ({kb['product_name']}, {kb['source_url']}):
Trusted by {kb['social_proof']['users']}, {kb['social_proof']['organizations']} across {kb['social_proof']['countries']}.
{kb_text}

INDIA PRICING ({pricing.get('free_trial', '')}, {pricing.get('tax_note', '')}, {pricing.get('payroll_note', '')}):
{pricing_text}
"""


def build_greeting(
    lead_name: str = "there",
    company_name: str = "",
    team_size: str = "",
    primary_interest: str = "",
) -> str:
    if primary_interest:
        return (
            f"Hi {lead_name}, this is Aria calling from Zoho People! "
            f"Thanks for registering your interest in {primary_interest}. "
            "To make sure I point you to the right setup, what's the biggest bottleneck your HR team is running into with that right now?"
        )
    if company_name and company_name not in ("your team", "your company"):
        return (
            f"Hi {lead_name}, this is Aria calling from Zoho People! "
            f"Thanks for registering your interest for {company_name}. "
            "I'd love to learn a bit about your HR setup—what's the biggest challenge your team is trying to solve right now?"
        )
    return (
        f"Hi {lead_name}, this is Aria calling from Zoho People! "
        "Thanks for registering your interest with us. "
        "I'd love to learn a bit about your HR setup—what's the biggest challenge your team is trying to solve right now?"
    )
