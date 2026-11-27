"""TwiML builders with absolute callback URLs and bounded speech gathering."""

from __future__ import annotations

from urllib.parse import quote

from fastapi.responses import Response
from twilio.twiml.voice_response import Gather, VoiceResponse


def as_xml(voice: VoiceResponse) -> Response:
    return Response(content=str(voice), media_type="application/xml")


def gather(
    text: str,
    *,
    base_url: str,
    voice: str,
    retry: bool = False,
    token: str = "",
) -> Response:
    response = VoiceResponse()
    suffix = f"?token={quote(token)}" if token else ""
    action_url = f"{base_url}/twilio/respond{suffix}" if base_url else f"/twilio/respond{suffix}"
    node = Gather(
        input="speech",
        method="POST",
        action=action_url,
        speech_timeout="auto",
        timeout=6,
        language="en-IN",
    )
    node.say(text, voice=voice)
    response.append(node)
    fallback = (
        "Sorry, I didn't quite catch that—could you repeat?"
        if retry
        else "I didn't catch that. Feel free to reach out anytime. Goodbye!"
    )
    response.say(fallback, voice=voice)
    return as_xml(response)


def hangup(text: str, *, voice: str) -> Response:
    response = VoiceResponse()
    response.say(text, voice=voice)
    response.hangup()
    return as_xml(response)


def dial(
    target: str,
    *,
    base_url: str,
    voice: str,
    caller_id: str = "",
    token: str = "",
) -> Response:
    response = VoiceResponse()
    response.say("Connecting you with a colleague from Interloop Consulting now.", voice=voice)
    suffix = f"?token={quote(token)}" if token else ""
    action = f"{base_url}/twilio/handoff-status{suffix}" if base_url else f"/twilio/handoff-status{suffix}"
    kwargs = {"method": "POST", "action": action, "timeout": 20}
    if caller_id:
        kwargs["callerId"] = caller_id
    response.dial(target, **kwargs)
    response.say("I could not connect that call right now. The team will call you back. Goodbye!", voice=voice)
    return as_xml(response)
