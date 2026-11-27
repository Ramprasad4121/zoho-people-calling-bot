import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Allow running tests on a host Python that relies on Docker for Twilio SDK
try:
    import twilio  # noqa: F401
except ImportError:
    twilio_mod = types.ModuleType("twilio")
    base_mod = types.ModuleType("twilio.base")
    exc_mod = types.ModuleType("twilio.base.exceptions")
    rest_mod = types.ModuleType("twilio.rest")
    twiml_mod = types.ModuleType("twilio.twiml")
    voice_mod = types.ModuleType("twilio.twiml.voice_response")

    class TwilioRestException(Exception):
        def __init__(self, status=400, uri="", msg="Twilio error", code=21219):
            super().__init__(msg)
            self.status = status
            self.uri = uri
            self.msg = msg
            self.code = code

    class _Client:
        def __init__(self, *args, **kwargs):
            self.calls = MagicMock()

    class _Gather:
        def __init__(self, **attrs):
            self.attrs = attrs
            self.says = []

        def say(self, text, **attrs):
            self.says.append((text, attrs))

    class _VoiceResponse:
        def __init__(self):
            self.children = []

        def append(self, node):
            self.children.append(("gather", node))

        def say(self, text, **attrs):
            self.children.append(("say", text, attrs))

        def hangup(self):
            self.children.append(("hangup",))

        def __str__(self):
            root = ET.Element("Response")
            for item in self.children:
                if item[0] == "gather":
                    node = item[1]
                    g = ET.SubElement(
                        root,
                        "Gather",
                        {k: str(v) for k, v in node.attrs.items()},
                    )
                    for txt, sattrs in node.says:
                        s = ET.SubElement(g, "Say", {k: str(v) for k, v in sattrs.items()})
                        s.text = txt
                elif item[0] == "say":
                    s = ET.SubElement(root, "Say", {k: str(v) for k, v in item[2].items()})
                    s.text = item[1]
                elif item[0] == "hangup":
                    ET.SubElement(root, "Hangup")
            return ET.tostring(root, encoding="unicode")

    exc_mod.TwilioRestException = TwilioRestException
    rest_mod.Client = _Client
    voice_mod.Gather = _Gather
    voice_mod.VoiceResponse = _VoiceResponse
    sys.modules["twilio"] = twilio_mod
    sys.modules["twilio.base"] = base_mod
    sys.modules["twilio.base.exceptions"] = exc_mod
    sys.modules["twilio.rest"] = rest_mod
    sys.modules["twilio.twiml"] = twiml_mod
    sys.modules["twilio.twiml.voice_response"] = voice_mod

try:
    import python_multipart  # noqa: F401
except ImportError:
    try:
        import multipart  # noqa: F401
    except ImportError:
        class _QuerystringParser:
            def __init__(self, callbacks):
                self.callbacks = callbacks
                self.buf = bytearray()

            def write(self, data: bytes):
                self.buf.extend(data)

            def finalize(self):
                raw = bytes(self.buf)
                if raw:
                    for pair in raw.split(b"&"):
                        k, _, v = pair.partition(b"=")
                        self.callbacks["on_field_start"]()
                        self.callbacks["on_field_name"](k, 0, len(k))
                        self.callbacks["on_field_data"](v, 0, len(v))
                        self.callbacks["on_field_end"]()
                self.callbacks["on_end"]()

        mp_mod = types.ModuleType("multipart")
        mp_mod.__version__ = "0.0.17"
        mp_sub = types.ModuleType("multipart.multipart")
        mp_sub.parse_options_header = lambda val: (
            (val or "").split(";")[0].strip().lower().encode(),
            {},
        )
        mp_sub.QuerystringParser = _QuerystringParser
        mp_mod.QuerystringParser = _QuerystringParser
        sys.modules["multipart"] = mp_mod
        sys.modules["multipart.multipart"] = mp_sub

from fastapi.testclient import TestClient  # noqa: E402
import app as bot_app  # noqa: E402
from local_demo import fallback_reply, generate_llm_reply, sanitize_for_tts  # noqa: E402
from prompts import build_greeting, build_system_prompt, load_knowledge_base  # noqa: E402


class TestZohoPeopleBot(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._orig_data_dir = bot_app.DATA_DIR
        self._orig_hist_path = bot_app.CALL_HISTORY_PATH
        bot_app.DATA_DIR = Path(self._tmp_dir.name)
        bot_app.CALL_HISTORY_PATH = bot_app.DATA_DIR / "call_history.json"
        bot_app.CALL_SESSIONS.clear()
        bot_app.CALL_RECORDS.clear()
        bot_app.WEB_SESSIONS.clear()
        self.client = TestClient(bot_app.app)

    def tearDown(self):
        bot_app.DATA_DIR = self._orig_data_dir
        bot_app.CALL_HISTORY_PATH = self._orig_hist_path
        self._tmp_dir.cleanup()

    def test_qualification_extracts_bant_and_appointment(self):
        qualification = bot_app.extract_qualification(
            [
                {
                    "role": "user",
                    "text": (
                        "We have 80 employees. Our budget is around Rs 5 lakh. "
                        "I make the final decision. We need this by next quarter. "
                        "Let's meet Thursday at 3 pm."
                    ),
                }
            ]
        )
        for key in ("team_size", "topics", "email", "next_step", "turns",
                    "budget", "authority", "timeline", "appointment"):
            self.assertIn(key, qualification)
        self.assertEqual(qualification["team_size"], "80")
        self.assertIn("5 lakh", qualification["budget"])
        self.assertEqual(qualification["authority"], "Decision maker")
        self.assertEqual(qualification["timeline"], "next quarter")
        self.assertIn("Thursday", qualification["appointment"])
        self.assertEqual(
            bot_app.extract_qualification([{"role": "user", "text": "I decide."}])["authority"],
            "Decision maker",
        )

    def test_webhook_export_posts_once_and_tolerates_failure(self):
        with patch.dict(bot_app.os.environ, {"SALES_WEBHOOK_URL": "https://example.test/hook"}):
            with patch.object(bot_app.urllib.request, "urlopen") as urlopen:
                urlopen.return_value.__enter__.return_value.read.return_value = b"ok"
                record = {"call_sid": "CAexport"}
                bot_app.export_lead_to_webhook(record)
                bot_app.export_lead_to_webhook(record)
                self.assertEqual(urlopen.call_count, 1)
                self.assertTrue(record["exported"])

            with patch.object(
                bot_app.urllib.request,
                "urlopen",
                side_effect=OSError("receiver offline"),
            ):
                bot_app.export_lead_to_webhook({"call_sid": "CAfailed"})

    def test_knowledge_base_and_prompt_include_pricing_and_context(self):
        kb = load_knowledge_base()
        self.assertEqual(kb["product_name"], "Zoho People")
        self.assertGreaterEqual(len(kb["modules"]), 6)
        self.assertIn("pricing_india", kb)
        prompt = build_system_prompt(
            lead_name="Alex",
            company_name="Acme",
            team_size="51-200 employees",
            primary_interest="Attendance & Shift Management",
        )
        self.assertIn("Aria", prompt)
        self.assertIn("Alex", prompt)
        self.assertIn("Acme", prompt)
        self.assertIn("51-200 employees", prompt)
        self.assertIn("Essential HR", prompt)

        greeting = build_greeting("Alex", "Acme", primary_interest="Attendance")
        self.assertIn("Alex", greeting)
        self.assertIn("Attendance", greeting)

    def test_sanitize_and_fallback_coverage(self):
        self.assertEqual(sanitize_for_tts("**Hello** _world_! [END_CALL]"), "Hello world!")
        self.assertIn("48 rupees", fallback_reply("What is the pricing per user?"))
        self.assertIn("Zoho Payroll", fallback_reply("Does it include payroll and PF?"))
        self.assertIn("Learning Management System", fallback_reply("Do you have an LMS for training?"))
        self.assertIn("HR Help Desk", fallback_reply("How do employees raise HR tickets?"))
        self.assertIn("120", fallback_reply("We have 120 employees"))
        self.assertIn("[END_CALL]", fallback_reply("Thanks, bye!"))

    def test_pages_and_health_endpoints(self):
        for route in ("/", "/web", "/web/calls"):
            res = self.client.get(route)
            self.assertEqual(res.status_code, 200, f"GET {route} failed")

        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        data = health.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("llm_configured", data)
        self.assertIn("ready_for_phone_calls", data)

    def test_web_voice_flow_end_to_end_offline_fallback(self):
        with patch.object(bot_app, "get_llm_client", side_effect=RuntimeError("no key")):
            start = self.client.post(
                "/leads",
                json={
                    "lead_name": "Alex",
                    "company_name": "Acme",
                    "team_size": "51-200 employees",
                    "primary_interest": "Attendance & Shift Management",
                    "channel": "browser",
                },
            )
            self.assertEqual(start.status_code, 200)
            session_id = start.json()["session_id"]
            self.assertIn("Attendance", start.json()["greeting"])

            turn1 = self.client.post(
                "/web/turn",
                json={"session_id": session_id, "message": "We have 120 employees and want pricing and a demo"},
            )
            self.assertEqual(turn1.status_code, 200)
            body1 = turn1.json()
            self.assertFalse(body1["end_call"])
            self.assertEqual(body1["qualification"]["team_size"], "51-200 employees")
            self.assertIn("Pricing & Trial", body1["qualification"]["topics"])

            turn2 = self.client.post(
                "/web/turn",
                json={"session_id": session_id, "message": "Thanks, goodbye"},
            )
            self.assertEqual(turn2.status_code, 200)
            self.assertTrue(turn2.json()["end_call"])
            self.assertNotIn(session_id, bot_app.WEB_SESSIONS)

    def test_outbound_twilio_call_and_voice_webhooks(self):
        mock_call = MagicMock()
        mock_call.sid = "CA1234567890"
        mock_call.status = "queued"

        mock_twilio_instance = MagicMock()
        mock_twilio_instance.calls.create.return_value = mock_call

        env = {
            "TWILIO_ACCOUNT_SID": "ACtest",
            "TWILIO_AUTH_TOKEN": "toktest",
            "TWILIO_PHONE_NUMBER": "+15551234567",
            "PUBLIC_BASE_URL": "https://example.trycloudflare.com",
        }
        with patch.dict(os.environ, env, clear=False), patch.object(
            bot_app, "TwilioClient", return_value=mock_twilio_instance
        ):
            res = self.client.post(
                "/call",
                json={
                    "to_number": "+1 555-765-4321",
                    "lead_name": "Alex",
                    "company_name": "Acme",
                    "primary_interest": "Performance & OKRs",
                },
            )
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["call_sid"], "CA1234567890")
            self.assertEqual(res.json()["to"], "+15557654321")

        voice_res = self.client.post("/twilio/voice", data={"CallSid": "CA1234567890"})
        self.assertEqual(voice_res.status_code, 200)
        self.assertIn("<Gather", voice_res.text)
        self.assertIn("Alex", voice_res.text)

        # First silence asks to repeat; second silence hangs up gracefully
        s1 = self.client.post("/twilio/respond", data={"CallSid": "CA1234567890", "SpeechResult": ""})
        self.assertIn("repeat", s1.text.lower())
        s2 = self.client.post("/twilio/respond", data={"CallSid": "CA1234567890", "SpeechResult": ""})
        self.assertIn("<Hangup", s2.text)

    def test_twilio_respond_with_mocked_llm_and_end_call(self):
        bot_app.CALL_SESSIONS["CA_LLM"] = {
            "lead_name": "Alex",
            "company_name": "Acme",
            "lead_meta": {},
            "silence_count": 0,
            "history": [{"role": "system", "content": "test"}],
        }
        bot_app.CALL_RECORDS["CA_LLM"] = {
            "call_sid": "CA_LLM",
            "channel": "phone",
            "to_number": "+15557654321",
            "lead_name": "Alex",
            "company_name": "Acme",
            "status": "in-progress",
            "duration": "",
            "transcript": [],
            "qualification": {},
        }
        mock_completion = MagicMock()
        mock_completion.choices = [
            MagicMock(message=MagicMock(content="**Great**, I'll book your demo for Friday! [END_CALL]"))
        ]
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_completion

        with patch.object(bot_app, "get_llm_client", return_value=(mock_client, "openai/gpt-oss-20b")):
            resp = self.client.post(
                "/twilio/respond",
                data={"CallSid": "CA_LLM", "SpeechResult": "Book the demo for Friday"},
            )
            self.assertEqual(resp.status_code, 200)
            self.assertIn("Great, I'll book your demo for Friday!", resp.text)
            self.assertNotIn("**", resp.text)
            self.assertIn("<Hangup", resp.text)
            self.assertNotIn("CA_LLM", bot_app.CALL_SESSIONS)

    def test_generate_llm_reply_disables_openrouter_reasoning(self):
        mock_completion = MagicMock()
        mock_completion.choices = [MagicMock(message=MagicMock(content="OpenRouter reply"))]
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_completion
        with patch.dict(os.environ, {"LLM_BASE_URL": "https://openrouter.ai/api/v1"}):
            out = generate_llm_reply(
                mock_client,
                "nvidia/nemotron-3.5-lightning:free",
                [{"role": "user", "content": "hi"}],
            )
        self.assertEqual(out, "OpenRouter reply")
        kwargs = mock_client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["extra_body"], {"reasoning": {"effort": "none"}})

    def test_generate_llm_reply_retries_without_reasoning_effort(self):
        mock_completion = MagicMock()
        mock_completion.choices = [MagicMock(message=MagicMock(content="Clean reply"))]
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = [
            ValueError("unsupported extra_body"),
            mock_completion,
        ]
        out = generate_llm_reply(mock_client, "openai/gpt-oss-20b", [{"role": "user", "content": "hi"}])
        self.assertEqual(out, "Clean reply")
        self.assertEqual(mock_client.chat.completions.create.call_count, 2)

    def test_invalid_phone_number_rejected(self):
        env = {
            "TWILIO_ACCOUNT_SID": "ACtest",
            "TWILIO_AUTH_TOKEN": "toktest",
            "TWILIO_PHONE_NUMBER": "+15551234567",
            "PUBLIC_BASE_URL": "https://example.trycloudflare.com",
        }
        with patch.dict(os.environ, env, clear=False):
            res = self.client.post(
                "/call",
                json={"to_number": "9876543210", "lead_name": "Alex"},
            )
            self.assertEqual(res.status_code, 400)
            self.assertIn("E.164", res.json()["detail"])


if __name__ == "__main__":
    unittest.main()
