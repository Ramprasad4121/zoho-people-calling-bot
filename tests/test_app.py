"""Comprehensive tests for the calling bot. External services are always mocked."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient

import app as bot_app
import integrations
import security
from local_demo import fallback_reply, generate_llm_reply, sanitize_for_tts
from prompts import build_greeting, build_system_prompt, load_knowledge_base

SID = "CA12345678901234567890123456789012"


def mock_twilio(monkeypatch, sid: str = SID):
    call = SimpleNamespace(sid=sid, status="queued")
    client = MagicMock()
    client.calls.create.return_value = call
    monkeypatch.setattr(bot_app, "TwilioClient", MagicMock(return_value=client))
    return client


def mock_llm(monkeypatch, replies):
    client = MagicMock()
    responses = []
    for text in replies:
        completion = MagicMock()
        completion.choices = [MagicMock(message=MagicMock(content=text))]
        responses.append(completion)
    client.chat.completions.create.side_effect = responses
    monkeypatch.setattr(bot_app, "get_llm_client", lambda: (client, "test-model"))
    return client


def twiml(response: TestClient | object):
    xml = ET.fromstring(response.text)
    assert xml.tag == "Response"
    return xml


# --- Health, prompts and qualification -------------------------------------


def test_health_and_pages(client):
    for route in ("/", "/web", "/web/calls", "/health", "/webhook-check"):
        response = client.get(route)
        assert response.status_code == 200, route
    data = client.get("/health").json()
    assert data["status"] == "ok"
    assert data["console_auth_required"] is True
    assert data["twilio_signature_validation"] is False  # tests disable it explicitly
    assert "public_base_url" not in data  # never leak the tunnel URL
    assert client.get("/webhook-check").text == "ok"


def test_prompt_discloses_ai_and_uses_kb(client):
    kb = load_knowledge_base()
    assert kb["product_name"] == "Zoho People"
    assert "Interloop Consulting" in kb["interloop"]["name"]
    prompt = build_system_prompt("Alex", "Acme", team_size="51-200 employees")
    assert "AI sales assistant" in prompt
    assert "Interloop Consulting" in prompt
    assert "Zoho People" in prompt
    assert "51-200 employees" in prompt
    assert "Essential HR" in prompt
    greeting = build_greeting("Alex", "Acme", primary_interest="Attendance")
    assert "AI sales assistant" in greeting
    assert "Interloop Consulting" in greeting
    assert "Alex" in greeting


def test_sanitize_and_fallback_are_kb_consistent(client):
    assert sanitize_for_tts("**Hello** _world_! [END_CALL]") == "Hello world!"
    assert "48 rupees" in fallback_reply("What is the pricing per user?")
    assert "Zoho Payroll" in fallback_reply("Does it include payroll and PF?")
    assert "Learning Management System" in fallback_reply("Do you have an LMS?")
    assert "HR Help Desk" in fallback_reply("How do employees raise HR tickets?")
    assert "[END_CALL]" in fallback_reply("Thanks, bye!")
    assert "GreytHR" not in fallback_reply("Which integrations do you have?")


def test_qualification_extracts_bant_and_appointment(client):
    qualification = bot_app.extract_qualification(
        [
            {
                "role": "user",
                "text": (
                    "We have 80 employees. Our budget is around Rs 5 lakh. "
                    "I make the final decision. We need this by next quarter. "
                    "Let's meet Thursday at 3 pm. alex@example.com"
                ),
            }
        ]
    )
    assert qualification["team_size"] == "80"
    assert "5 lakh" in qualification["budget"]
    assert qualification["authority"] == "Decision maker"
    assert qualification["timeline"] == "next quarter"
    assert "Thursday" in qualification["appointment"]
    assert qualification["email"] == "alex@example.com"
    assert bot_app.extract_qualification([{"role": "user", "text": "I decide."}])["authority"] == "Decision maker"


def test_llm_timeout_is_below_twilio_deadline(client, env):
    env(LLM_TIMEOUT_SECONDS="99")
    assert bot_app.llm_timeout_seconds() == 12.0
    env(LLM_TIMEOUT_SECONDS="3")
    assert bot_app.llm_timeout_seconds() == 3.0


def test_generate_llm_reply_provider_extras_and_retry(client, monkeypatch):
    completion = MagicMock()
    completion.choices = [MagicMock(message=MagicMock(content="OpenRouter reply"))]
    llm = MagicMock()
    llm.chat.completions.create.return_value = completion
    monkeypatch.setenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
    assert generate_llm_reply(llm, "nvidia/nemotron-3.5-lightning:free", [{"role": "user", "content": "hi"}]) == "OpenRouter reply"
    assert llm.chat.completions.create.call_args.kwargs["extra_body"] == {"reasoning": {"effort": "none"}}

    llm.chat.completions.create.side_effect = [ValueError("unsupported"), completion]
    monkeypatch.setenv("LLM_BASE_URL", "")
    assert generate_llm_reply(llm, "gpt-oss-20b", [{"role": "user", "content": "hi"}]) == "OpenRouter reply"
    assert llm.chat.completions.create.call_count == 3


# --- Outbound calling, auth and preflight -----------------------------------


def test_phone_call_happy_path(client, monkeypatch, auth_headers):
    twilio = mock_twilio(monkeypatch)
    response = client.post(
        "/call",
        json={
            "to_number": "+1 555-765-4321",
            "lead_name": "Alex",
            "company_name": "Acme",
            "primary_interest": "Performance & OKRs",
        },
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["call_sid"] == SID
    assert body["to"] == "+15557654321"
    kwargs = twilio.calls.create.call_args.kwargs
    assert kwargs["to"] == "+15557654321"
    assert kwargs["from_"] == "+15551234567"
    assert kwargs["url"].startswith("https://tunnel.example.test/twilio/voice?token=")
    assert bot_app.CALL_SESSIONS[SID]["webhook_token"] in kwargs["url"]
    assert kwargs["method"] == "POST"
    assert kwargs["status_callback"] == "https://tunnel.example.test/twilio/status"
    assert kwargs["status_callback_event"] == ["initiated", "ringing", "answered", "completed"]
    assert SID in bot_app.CALL_SESSIONS
    assert bot_app.CALL_RECORDS[SID]["status"] == "queued"


def test_call_endpoints_require_token(client, env, monkeypatch, auth_headers):
    mock_twilio(monkeypatch)
    payload = {"to_number": "+15557654321"}
    assert client.post("/call", json=payload).status_code == 401
    assert client.get("/web/calls/status").status_code == 401
    env(CALL_API_TOKEN="")
    response = client.post("/call", json=payload, headers=auth_headers)
    assert response.status_code == 503
    assert "CALL_API_TOKEN" in response.json()["detail"]


def test_invalid_phone_rejected(client, auth_headers):
    response = client.post("/call", json={"to_number": "9876543210"}, headers=auth_headers)
    assert response.status_code == 400
    assert "E.164" in response.json()["detail"]


def test_duplicate_call_prevented(client, monkeypatch, auth_headers):
    mock_twilio(monkeypatch)
    payload = {"to_number": "+15557654321"}
    assert client.post("/call", json=payload, headers=auth_headers).status_code == 200
    second = client.post("/call", json=payload, headers=auth_headers)
    assert second.status_code == 409


def test_batch_call_reports_individual_errors(client, monkeypatch, auth_headers):
    mock_twilio(monkeypatch)
    response = client.post(
        "/web/calls",
        json={"contacts": [{"to_number": "+15557654321"}, {"to_number": "bad"}]},
        headers=auth_headers,
    )
    assert response.status_code == 200
    calls = response.json()["calls"]
    assert calls[0]["call_sid"] == SID
    assert calls[1]["status"] == "error"


def test_preflight_reports_actionable_dns_error(client, env, monkeypatch, auth_headers):
    env(SKIP_WEBHOOK_PREFLIGHT="false", PUBLIC_BASE_URL="https://example.com")
    mock_twilio(monkeypatch)
    monkeypatch.setattr(security, "validate_public_url", lambda url, **kwargs: url)

    def fail(*args, **kwargs):
        raise OSError("Could not resolve host: example.com")

    monkeypatch.setattr(security, "fetch_url", fail)
    response = client.post("/call", json={"to_number": "+15557654321"}, headers=auth_headers)
    assert response.status_code == 503
    assert "Restart the tunnel" in response.json()["detail"]
    assert "example.com" in response.json()["detail"]


def test_trial_restriction_error_is_actionable(client, monkeypatch, auth_headers):
    from twilio.base.exceptions import TwilioRestException

    client_mock = MagicMock()
    client_mock.calls.create.side_effect = TwilioRestException(400, "/Calls", "unverified number", 21219)
    monkeypatch.setattr(bot_app, "TwilioClient", MagicMock(return_value=client_mock))
    response = client.post("/call", json={"to_number": "+15557654321"}, headers=auth_headers)
    assert response.status_code == 400
    assert "verified number" in response.json()["detail"]
    assert not bot_app.CALL_RECORDS


def test_invalid_parameter_error_is_not_misreported_as_trial_verification(client, monkeypatch, auth_headers):
    from twilio.base.exceptions import TwilioRestException

    client_mock = MagicMock()
    client_mock.calls.create.side_effect = TwilioRestException(
        400,
        "/Calls",
        "Invalid or disallowed parameters provided - trial accounts have limited parameter access",
        0,
    )
    monkeypatch.setattr(bot_app, "TwilioClient", MagicMock(return_value=client_mock))
    response = client.post("/call", json={"to_number": "+15557654321"}, headers=auth_headers)
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "verified number" not in detail
    assert "status_callback_event" in detail


def test_rate_limit_blocks_abuse(client, env, monkeypatch, auth_headers):
    env(RATE_LIMIT_ENABLED="true")
    monkeypatch.setattr(bot_app, "place_outbound_call", lambda payload: {"ok": True})
    for _ in range(10):
        assert client.post("/call", json={"to_number": "+15557654321"}, headers=auth_headers).status_code == 200
    assert client.post("/call", json={"to_number": "+15557654321"}, headers=auth_headers).status_code == 429


# --- Twilio webhooks ---------------------------------------------------------


def test_voice_webhook_returns_valid_twiml(client, monkeypatch, auth_headers):
    mock_twilio(monkeypatch)
    client.post("/call", json={"to_number": "+15557654321", "lead_name": "Alex"}, headers=auth_headers)
    response = client.post("/twilio/voice", data={"CallSid": SID, "From": "+15557654321"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    root = twiml(response)
    gather = root.find("Gather")
    assert gather is not None
    assert gather.attrib["action"].startswith("https://tunnel.example.test/twilio/respond?token=")
    assert bot_app.CALL_SESSIONS[SID]["webhook_token"] in gather.attrib["action"]
    assert gather.attrib["input"] == "speech"
    assert "AI sales assistant" in gather.find("Say").text
    assert "Alex" in gather.find("Say").text


def test_multi_turn_conversation_with_mocked_llm(client, monkeypatch):
    mock_llm(monkeypatch, ["Got it, 80 employees. Which area is the priority?", "Thanks, bye! [END_CALL]"])
    client.post("/twilio/voice", data={"CallSid": SID})
    first = client.post("/twilio/respond", data={"CallSid": SID, "SpeechResult": "We have 80 employees"})
    assert "80 employees" in first.text
    assert "<Gather" in first.text
    second = client.post("/twilio/respond", data={"CallSid": SID, "SpeechResult": "Thanks, goodbye"})
    assert "<Hangup" in second.text
    assert SID not in bot_app.CALL_SESSIONS


def test_silence_and_timeout_are_bounded(client):
    client.post("/twilio/voice", data={"CallSid": SID})
    first = client.post("/twilio/respond", data={"CallSid": SID, "SpeechResult": ""})
    assert "repeat" in first.text.lower()
    second = client.post("/twilio/respond", data={"CallSid": SID, "SpeechResult": ""})
    assert "<Hangup" in twiml(second).tag or "<Hangup" in second.text
    assert SID not in bot_app.CALL_SESSIONS


def test_unknown_session_and_turn_cap_hang_up(client, monkeypatch):
    unknown = client.post("/twilio/respond", data={"CallSid": "CAunknown", "SpeechResult": "hello"})
    assert "<Hangup" in unknown.text

    client.post("/twilio/voice", data={"CallSid": SID})
    bot_app.CALL_SESSIONS[SID]["turn_count"] = bot_app.MAX_TURNS
    monkeypatch.setattr(bot_app, "fallback_reply", lambda text: "Wrapping up now. [END_CALL]")
    capped = client.post("/twilio/respond", data={"CallSid": SID, "SpeechResult": "hello"})
    assert "<Hangup" in capped.text


def test_live_llm_failure_falls_back(client, monkeypatch):
    def fail():
        raise TimeoutError("LLM timed out")

    monkeypatch.setattr(bot_app, "get_llm_client", fail)
    client.post("/twilio/voice", data={"CallSid": SID})
    response = client.post("/twilio/respond", data={"CallSid": SID, "SpeechResult": "What does it cost?"})
    assert "Zoho People" in response.text
    assert "<Gather" in response.text


def test_twilio_signature_validation(client, env):
    from twilio.request_validator import RequestValidator

    env(TWILIO_VERIFY_SIGNATURES="true")
    params = {"CallSid": SID, "SpeechResult": "hello"}
    url = "http://testserver/twilio/respond"
    signature = RequestValidator("test-twilio-token").compute_signature(url, params)
    valid = client.post("/twilio/respond", data=params, headers={"X-Twilio-Signature": signature})
    assert valid.status_code == 200
    invalid = client.post("/twilio/respond", data=params, headers={"X-Twilio-Signature": "bad-signature"})
    assert invalid.status_code == 403
    missing = client.post("/twilio/respond", data=params)
    assert missing.status_code == 403


def test_status_callback_finalizes_and_exports(client, monkeypatch, auth_headers):
    mock_twilio(monkeypatch)
    export = MagicMock()
    monkeypatch.setattr(integrations, "post_lead_webhook", export)
    client.post("/call", json={"to_number": "+15557654321"}, headers=auth_headers)
    client.post("/twilio/voice", data={"CallSid": SID})
    response = client.post(
        "/twilio/status",
        data={"CallSid": SID, "CallStatus": "completed", "CallDuration": "42"},
    )
    assert response.json()["received"] is True
    assert bot_app.CALL_RECORDS[SID]["status"] == "completed"
    assert bot_app.CALL_RECORDS[SID]["duration"] == "42"
    assert SID not in bot_app.CALL_SESSIONS
    export.assert_called_once()


def test_busy_status_does_not_export_lead(client, monkeypatch, auth_headers):
    mock_twilio(monkeypatch)
    export = MagicMock()
    monkeypatch.setattr(integrations, "post_lead_webhook", export)
    client.post("/call", json={"to_number": "+15557654321"}, headers=auth_headers)
    client.post("/twilio/status", data={"CallSid": SID, "CallStatus": "busy"})
    assert bot_app.CALL_RECORDS[SID]["status"] == "busy"
    export.assert_not_called()


def test_handoff_requires_explicit_confirmation(client, monkeypatch, env):
    env(HANDOFF_PHONE_NUMBER="+15559998888")
    client.post("/twilio/voice", data={"CallSid": SID})
    request = client.post("/twilio/respond", data={"CallSid": SID, "SpeechResult": "Can I speak to a person?"})
    assert "confirm" in request.text.lower()
    assert "<Dial" not in request.text
    confirmed = client.post("/twilio/respond", data={"CallSid": SID, "SpeechResult": "Yes please"})
    root = twiml(confirmed)
    dial = root.find("Dial")
    assert dial is not None
    assert dial.text == "+15559998888"
    assert bot_app.CALL_RECORDS[SID]["next_step"] == "Transferred to human"


def test_handoff_without_config_offers_callback(client):
    client.post("/twilio/voice", data={"CallSid": SID})
    response = client.post("/twilio/respond", data={"CallSid": SID, "SpeechResult": "Transfer me to a human"})
    assert "call you back" in response.text
    assert "<Dial" not in response.text


# --- Browser demo ------------------------------------------------------------


def test_web_demo_end_to_end_and_persistence(client, monkeypatch):
    monkeypatch.setattr(bot_app, "get_llm_client", lambda: (_ for _ in ()).throw(RuntimeError("no key")))
    start = client.post("/leads", json={"lead_name": "Alex", "channel": "browser"})
    assert start.status_code == 200
    session_id = start.json()["session_id"]
    assert "AI sales assistant" in start.json()["greeting"]

    first = client.post("/web/turn", json={"session_id": session_id, "message": "We have 120 employees and want pricing"})
    assert first.status_code == 200
    assert "Pricing & Trial" in first.json()["qualification"]["topics"]
    assert first.json()["end_call"] is False

    ended = client.post("/web/turn", json={"session_id": session_id, "message": "Thanks, goodbye"})
    assert ended.json()["end_call"] is True
    assert session_id not in bot_app.WEB_SESSIONS
    saved = json.loads(bot_app.CALL_HISTORY_PATH.read_text())
    assert any(rec["call_sid"] == session_id and rec["status"] == "completed" for rec in saved)


def test_web_end_exports_once(client, monkeypatch):
    export = MagicMock()
    monkeypatch.setattr(integrations, "post_lead_webhook", export)
    session_id = client.post("/web/start", json={"lead_name": "Alex"}).json()["session_id"]
    client.post("/web/end", json={"session_id": session_id})
    client.post("/web/end", json={"session_id": session_id})
    export.assert_called_once()
    assert bot_app.CALL_RECORDS[session_id]["status"] == "completed"


def test_web_turn_rejects_unknown_session(client):
    assert client.post("/web/turn", json={"session_id": "missing", "message": "hi"}).status_code == 404


# --- Calendly ----------------------------------------------------------------


def test_calendly_offer_requires_confirmation_before_booking(client, monkeypatch, env):
    env(
        CALENDLY_ACCESS_TOKEN="pat-test",
        CALENDLY_EVENT_TYPE_URI="https://api.calendly.com/event_types/abc123",
    )
    slots = [
        {"start_time": "2030-01-02T10:00:00Z", "label": "Wednesday 03:30 PM IST", "scheduling_url": "https://calendly.com/x"},
        {"start_time": "2030-01-03T10:00:00Z", "label": "Thursday 03:30 PM IST", "scheduling_url": "https://calendly.com/y"},
    ]
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if method == "GET":
            return {
                "collection": [
                    {
                        "status": "available",
                        "invitees_remaining": 1,
                        "start_time": slot["start_time"],
                        "scheduling_url": slot["scheduling_url"],
                    }
                    for slot in slots
                ]
            }
        return {"resource": {"uri": "https://api.calendly.com/invitees/1", "start_time": "2030-01-02T10:00:00Z"}}

    monkeypatch.setattr(integrations, "_calendly_request", fake_request)
    session_id = client.post("/web/start", json={"lead_name": "Alex", "email": "alex@example.com"}).json()["session_id"]
    offer = client.post("/web/turn", json={"session_id": session_id, "message": "I would like to book a demo"}).json()
    assert "Wednesday" in offer["reply"]
    assert "option one" in offer["reply"]

    selection = client.post("/web/turn", json={"session_id": session_id, "message": "First one"}).json()
    assert "confirm" in selection["reply"].lower()
    assert not any(method == "POST" for method, _, _ in calls)

    booked = client.post("/web/turn", json={"session_id": session_id, "message": "Yes, book it"}).json()
    assert "booked" in booked["reply"].lower()
    assert booked["qualification"]["next_step"] == "Demo booked via Calendly"
    assert any(method == "POST" for method, _, _ in calls)


def test_calendly_failure_never_claims_success(client, monkeypatch, env):
    env(CALENDLY_ACCESS_TOKEN="pat-test", CALENDLY_EVENT_TYPE_URI="https://api.calendly.com/event_types/abc123")
    monkeypatch.setattr(
        integrations,
        "_calendly_request",
        MagicMock(side_effect=integrations.IntegrationError("Calendly is unavailable")),
    )
    session_id = client.post("/web/start", json={"lead_name": "Alex", "email": "alex@example.com"}).json()["session_id"]
    result = client.post("/web/turn", json={"session_id": session_id, "message": "Book a demo please"}).json()
    assert "booked" not in result["reply"].lower()


def test_calendly_requires_configuration(client, env):
    env(CALENDLY_ACCESS_TOKEN="", CALENDLY_EVENT_TYPE_URI="")
    assert integrations.calendly_configured() is False
    with pytest.raises(integrations.IntegrationError):
        integrations.available_slots()


# --- Research and SSRF -------------------------------------------------------


def test_research_fetch_cache_and_fallback(monkeypatch, env):
    env(RESEARCH_CACHE_TTL_SECONDS="3600")
    integrations._RESEARCH_CACHE.update({"data": None, "expires_at": 0, "fetched_at": ""})
    pages = {
        integrations.FEATURES_URL: "Zoho People features attendance leave onboarding",
        integrations.PRICING_URL: "30 day free trial plans Essential HR Professional",
        integrations.INTERLOOP_URL: "Interloop Consulting Zoho Premium Partner India UAE",
    }
    monkeypatch.setattr(integrations, "_fetch_text", lambda url: pages[url])
    data = integrations.refresh_research()
    assert data and "Zoho Premium Partner" in data["interloop"]
    assert integrations.get_research()["zoho_features"].startswith("Zoho People features")

    prompt = build_system_prompt("Alex", "Acme", live_context=data)
    assert "Zoho Premium Partner" in prompt

    monkeypatch.setattr(integrations, "_fetch_text", MagicMock(side_effect=OSError("offline")))
    integrations._RESEARCH_CACHE.update({"data": None, "expires_at": 0, "fetched_at": ""})
    assert integrations.refresh_research() is None


def test_ssrf_url_validation(monkeypatch):
    with pytest.raises(security.UnsafeURLError):
        security.validate_public_url("http://example.com/anything")
    with pytest.raises(security.UnsafeURLError):
        security.validate_public_url("https://user:password@example.com/")
    with pytest.raises(security.UnsafeURLError):
        security.validate_public_url("https://127.0.0.1/admin")
    with pytest.raises(security.UnsafeURLError):
        security.validate_public_url("https://192.168.1.10/")


def test_phone_normalization_and_masking():
    assert security.normalize_phone_number("+91 (98765) 43210") == "+919876543210"
    with pytest.raises(ValueError):
        security.normalize_phone_number("+0123456789")
    assert security.mask_phone("+15557654321").endswith("4321")


# --- Additional integration and fallback coverage --------------------------


def test_unsigned_twilio_webhooks_use_per_call_token(client, env, monkeypatch):
    env(TWILIO_VERIFY_SIGNATURES="true")
    mock_llm(monkeypatch, ["Hello Alex, what brings you here today?"])
    session = bot_app._new_session("Alex", "Acme", {})
    token = session["webhook_token"]
    bot_app.CALL_SESSIONS[SID] = session

    voice = client.post(f"/twilio/voice?token={token}", data={"CallSid": SID})
    assert voice.status_code == 200
    assert token in voice.text

    reply = client.post(
        f"/twilio/respond?token={token}",
        data={"CallSid": SID, "SpeechResult": "I am interested"},
    )
    assert reply.status_code == 200
    assert token in reply.text

    denied = client.post("/twilio/voice?token=wrong-token", data={"CallSid": SID})
    assert denied.status_code == 403


def test_calendly_api_payloads_and_auth(monkeypatch, env):
    env(
        CALENDLY_ACCESS_TOKEN="pat-secret",
        CALENDLY_EVENT_TYPE_URI="https://api.calendly.com/event_types/abc123",
    )
    calls = []
    slot_response = json.dumps(
        {
            "collection": [
                {
                    "status": "available",
                    "invitees_remaining": 2,
                    "start_time": "2030-01-02T10:00:00Z",
                    "scheduling_url": "https://calendly.com/demo/1",
                }
            ]
        }
    ).encode()
    booking_response = json.dumps(
        {"resource": {"uri": "https://api.calendly.com/invitees/1", "start_time": "2030-01-02T10:00:00Z"}}
    ).encode()

    def fake_fetch(url, **kwargs):
        calls.append((url, kwargs))
        return slot_response if kwargs["method"] == "GET" else booking_response

    monkeypatch.setattr(integrations, "fetch_url", fake_fetch)
    slots = integrations.available_slots(days=3, limit=1)
    assert slots[0]["start_time"] == "2030-01-02T10:00:00Z"
    assert "event_type=https%3A%2F%2Fapi.calendly.com%2Fevent_types%2Fabc123" in calls[0][0]
    assert calls[0][1]["headers"]["Authorization"] == "Bearer pat-secret"

    booking = integrations.book_slot(
        start_time="2030-01-02T10:00:00Z",
        lead_name="Alex",
        email="alex@example.com",
    )
    assert booking["uri"].endswith("/invitees/1")
    assert calls[1][1]["method"] == "POST"
    payload = json.loads(calls[1][1]["body"].decode())
    assert payload["invitee"]["email"] == "alex@example.com"
    assert all("pat-secret" not in url and b"pat-secret" not in (kwargs.get("body") or b"") for url, kwargs in calls)


def test_lead_export_requires_https_and_never_raises(monkeypatch, env):
    env(SALES_WEBHOOK_URL="http://internal.example/hook", ALLOW_PRIVATE_WEBHOOK="false")
    fetch = MagicMock(side_effect=AssertionError("should not be called"))
    monkeypatch.setattr(integrations, "fetch_url", fetch)
    integrations.post_lead_webhook({"call_sid": SID})
    fetch.assert_not_called()

    env(SALES_WEBHOOK_URL="https://sales.example/hook")
    fetch.side_effect = OSError("offline")
    integrations.post_lead_webhook({"call_sid": SID})  # must swallow errors


def test_fallback_covers_all_core_topics():
    cases = {
        "What does attendance cost?": "48 rupees",
        "Does it handle payroll?": "Zoho Payroll",
        "We need attendance and shifts": "geo-fenced",
        "We hire 20 people": "Zoho Recruit",
        "Our performance reviews are manual": "360-degree appraisals",
        "Do you have an LMS?": "Learning Management System",
        "How do tickets work?": "HR Help Desk",
        "Can I speak to a human?": "Interloop Consulting",
        "I want a demo": "What email address",
        "We are a team of 3": "Free plan covers up to 5 users",
        "We have 700 employees": "Enterprise pricing",
    }
    for question, expected in cases.items():
        assert expected in fallback_reply(question), question


def test_preflight_success_probes_public_webhook(client, env, monkeypatch, auth_headers):
    env(SKIP_WEBHOOK_PREFLIGHT="false")
    mock_twilio(monkeypatch)
    probe = MagicMock(return_value=b"ok")
    monkeypatch.setattr(security, "fetch_url", probe)
    monkeypatch.setattr(security, "validate_public_url", lambda url, **kwargs: url)

    response = client.post("/call", json={"to_number": "+15557654321"}, headers=auth_headers)
    assert response.status_code == 200
    assert any(call.args[0] == "https://tunnel.example.test/webhook-check" for call in probe.call_args_list)
