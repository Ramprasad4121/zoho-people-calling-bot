"""Test bootstrap: deterministic config, no outbound calls, no live LLM calls."""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# These are set before app.py calls load_dotenv(); dotenv does not override them.
SAFE_ENV = {
    "TWILIO_ACCOUNT_SID": "ACtestaccount",
    "TWILIO_AUTH_TOKEN": "test-twilio-token",
    "TWILIO_PHONE_NUMBER": "+15551234567",
    "PUBLIC_BASE_URL": "https://tunnel.example.test",
    "CALL_API_TOKEN": "test-console-token",
    "TWILIO_VERIFY_SIGNATURES": "false",
    "SKIP_WEBHOOK_PREFLIGHT": "true",
    "RATE_LIMIT_ENABLED": "false",
    "LLM_API_KEY": "",
    "OPENAI_API_KEY": "",
    "GROQ_API_KEY": "",
    "LLM_BASE_URL": "",
    "SALES_WEBHOOK_URL": "",
    "CALENDLY_ACCESS_TOKEN": "",
    "CALENDLY_EVENT_TYPE_URI": "",
    "HANDOFF_PHONE_NUMBER": "",
}
for key, value in SAFE_ENV.items():
    os.environ[key] = value


def _install_twilio_stub() -> None:
    """Host machines without the Twilio SDK can still run the unit suite."""
    try:
        import twilio  # noqa: F401

        return
    except ImportError:
        pass

    class TwilioRestException(Exception):
        def __init__(self, status=400, uri="", msg="Twilio error", code=21211):
            super().__init__(msg)
            self.status, self.uri, self.msg, self.code = status, uri, msg, code

    class Client:
        def __init__(self, *args, **kwargs):
            self.calls = types.SimpleNamespace(create=lambda **kw: None)

    class Gather:
        def __init__(self, **attrs):
            self.attrs, self.content = attrs, ""

        def say(self, text, **attrs):
            self.content += text

    class VoiceResponse:
        def __init__(self):
            self.parts = []

        def append(self, node):
            self.parts.append(node)

        def say(self, text, **attrs):
            self.parts.append(text)

        def hangup(self):
            self.parts.append("<Hangup/>")

        def dial(self, number, **attrs):
            self.parts.append(f"<Dial>{number}</Dial>")

        def __str__(self):
            from xml.etree import ElementTree as ET

            root = ET.Element("Response")
            for part in self.parts:
                if isinstance(part, Gather):
                    child = ET.SubElement(root, "Gather", {k: str(v) for k, v in part.attrs.items()})
                    child.text = part.content
                elif str(part).startswith("<"):
                    ET.fromstring(str(part))
                    ET.SubElement(root, "Hangup" if "Hangup" in str(part) else "Dial")
                else:
                    child = ET.SubElement(root, "Say")
                    child.text = str(part)
            return ET.tostring(root, encoding="unicode")

    twilio_mod = types.ModuleType("twilio")
    base_mod = types.ModuleType("twilio.base")
    exc_mod = types.ModuleType("twilio.base.exceptions")
    rest_mod = types.ModuleType("twilio.rest")
    twiml_mod = types.ModuleType("twilio.twiml")
    voice_mod = types.ModuleType("twilio.twiml.voice_response")
    validator_mod = types.ModuleType("twilio.request_validator")

    exc_mod.TwilioRestException = TwilioRestException
    rest_mod.Client = Client
    voice_mod.Gather = Gather
    voice_mod.VoiceResponse = VoiceResponse
    validator_mod.RequestValidator = object
    for name, module in (
        ("twilio", twilio_mod),
        ("twilio.base", base_mod),
        ("twilio.base.exceptions", exc_mod),
        ("twilio.rest", rest_mod),
        ("twilio.twiml", twiml_mod),
        ("twilio.twiml.voice_response", voice_mod),
        ("twilio.request_validator", validator_mod),
    ):
        sys.modules[name] = module


_install_twilio_stub()

import app as bot_app  # noqa: E402
import state as bot_state  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(autouse=True)
def no_live_research(monkeypatch):
    """Unit tests must never perform outbound HTTP fetches."""
    monkeypatch.setattr(bot_app, "_live_context", lambda: None)


@pytest.fixture
def env(monkeypatch):
    """Set environment values for one test without leaking them to the next."""
    def setenv(**kwargs):
        for key, value in kwargs.items():
            monkeypatch.setenv(key, value)

    return setenv


@pytest.fixture
def client(tmp_path, monkeypatch):
    history_path = tmp_path / "call_history.json"
    monkeypatch.setattr(bot_state, "DATA_DIR", tmp_path)
    monkeypatch.setattr(bot_state, "CALL_HISTORY_PATH", history_path)
    bot_app.DATA_DIR = tmp_path
    bot_app.CALL_HISTORY_PATH = history_path
    bot_app.CALL_SESSIONS.clear()
    bot_app.CALL_RECORDS.clear()
    bot_app.WEB_SESSIONS.clear()
    bot_app.LAST_CALL_REQUEST.clear()
    bot_app.PREFLIGHT_CACHE.clear()
    bot_app.LLM_CLIENT_CACHE.clear()
    with TestClient(bot_app.app) as test_client:
        yield test_client


@pytest.fixture
def auth_headers():
    return {"X-API-Token": "test-console-token"}
