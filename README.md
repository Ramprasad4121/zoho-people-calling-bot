# Zoho People Outbound AI Calling Bot

Aria is an AI sales assistant that calls on behalf of **Interloop Consulting**, qualifies a lead's HR needs, pitches only relevant **Zoho People** features, and offers a real demo or trial. She discloses that she is an AI at the start of every call.

There are three ways to run it:

1. **Browser voice demo** — no Twilio account or phone charges.
2. **Real phone calls** — Twilio + a public HTTPS tunnel.
3. **Terminal simulator** — conversation-only, no browser or telephony.

No real phone calls were made while repairing this repository. All external integrations are opt-in and are reported as unavailable unless their provider confirms success.

---

## Root cause of "We could not reach your ML server. Please check your URL and try again."

Twilio shows this when its request to your webhook URL fails. In the inspected deployment:

* `PUBLIC_BASE_URL` was `https://artists-mentor-turtle-secondary.trycloudflare.com`, but DNS no longer resolved and no `cloudflared`/`ngrok` process was running. A quick tunnel URL dies when the process exits, and a new URL is issued on restart.
* The app did not check that the configured URL was reachable before spending money on a call.
* `/twilio/respond` called a synchronous LLM client with a 20-second timeout inside an `async` endpoint. Twilio needs a TwiML response in roughly 15 seconds, so a slow LLM could produce the same class of failure even with a healthy tunnel.

The fix adds a preflight DNS+HTTPS probe before dialing, clamps the LLM timeout to 8 seconds by default (hard maximum 12), runs it in a worker thread, and supplies a bounded offline knowledge-base fallback.

---

## Quick start

### 1. Configure

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"  # paste into CALL_API_TOKEN
docker compose up -d --build
```

`docker compose restart` does not reload `.env`; after changing it use:

```bash
docker compose up -d --force-recreate
```

Check configuration without placing a call:

```bash
curl -s http://localhost:8000/health | python3 -m json.tool
```

`/health` reports configuration flags and counts only. It never prints secrets, the tunnel URL, or transcripts.

### 2. Browser voice demo (free)

Open <http://localhost:8000/web> in Chrome or Edge, allow microphone access, press **Start call**, then **Speak** for each turn. Speech recognition and synthesis are provided by the browser. An optional LLM key gives freer replies; without one, Aria uses the offline knowledge-base rules.

For a free LLM key, use [Groq](https://console.groq.com/keys):

```env
LLM_API_KEY=gsk_...
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_MODEL=openai/gpt-oss-20b
```

OpenAI, OpenRouter, or an OpenAI-compatible local endpoint such as Ollama also work. OpenRouter reasoning is explicitly disabled so hidden tokens cannot consume the spoken reply budget.

### 3. Terminal simulator

```bash
python3 local_demo.py
```

---

## Real phone calls via Twilio

### Twilio setup

1. Sign up at <https://www.twilio.com/try-twilio>.
2. In **Communications → Voice → Overview → Try out Voice**, make a sample call and copy the `from` number shown in the sample API request into `TWILIO_PHONE_NUMBER`.
3. Trial accounts can call only **verified recipients** in the sign-up country. Verify extra recipients on that same page.
4. Copy the Account SID and Auth Token from the Console home page.

### Public HTTPS URL

Start a tunnel in a separate terminal and keep it open:

```bash
cloudflared tunnel --url http://localhost:8000
```

Copy the printed `https://...trycloudflare.com` URL into `PUBLIC_BASE_URL`. The app probes `<PUBLIC_BASE_URL>/webhook-check` before every call and returns an actionable error if the hostname is stale. Verify the tunnel manually with:

```bash
curl -sS -X POST 'https://<your-tunnel>/twilio/voice' -d 'CallSid=CAtest123'
```

It must return XML beginning with `<Response>` and containing `<Gather ... action="https://<your-tunnel>/twilio/respond">`.

### Place a call

Every endpoint that can spend money or read transcripts requires the console token:

```bash
curl -X POST http://localhost:8000/call \
  -H 'Content-Type: application/json' \
  -H 'X-API-Token: <CALL_API_TOKEN>' \
  -d '{"to_number":"+15551234567","lead_name":"Alex","company_name":"Acme","team_size":"51-200","primary_interest":"Attendance"}'
```

Use E.164 numbers. The lead console at <http://localhost:8000/> and batch dialer at <http://localhost:8000/web/calls/> both ask for this token and keep it only in the current tab's session storage.

The call flow is:

```text
POST /call
  → Twilio calls the lead
  → POST /twilio/voice         returns greeting + <Gather action=/twilio/respond>
  → POST /twilio/respond       speech → intent/LLM/TTS → next <Gather> or <Hangup>
  → POST /twilio/status        final status, duration, transcript export
```

There is no `SpeechResult` loop back into `/twilio/voice`; spoken turns go to `/twilio/respond`.

### Call lifecycle behavior

* Strict E.164 validation and absolute URLs on every TwiML action.
* Duplicate calls to the same number are blocked for 90 seconds, and active calls are blocked entirely.
* Two empty speech responses end the call instead of looping forever.
* A 30-turn cap prevents runaway conversations.
* `CallStatus`, `CallDuration`, and handoff results are persisted to `data/call_history.json`.
* Lead qualification (team size, topics, budget, authority, timeline, appointment, email, next step) is derived from the transcript.
* Twilio trial restriction errors are translated into instructions for verifying the recipient.

---

## Optional integrations

### Calendly demo booking

Set:

```env
CALENDLY_ACCESS_TOKEN=...               # PAT with event_types:read and scheduled_events:write
CALENDLY_EVENT_TYPE_URI=https://api.calendly.com/event_types/<uuid>
CALENDLY_DISPLAY_TIMEZONE=Asia/Kolkata
CALENDLY_INVITEE_TIMEZONE=Asia/Kolkata
```

The agent then offers real available slots. Booking happens only after the prospect gives an email and explicitly confirms a chosen slot. No booking is claimed unless Calendly returns a resource.

### Human handoff

```env
HANDOFF_PHONE_NUMBER=+1555...
```

With this set, a request to speak to a person triggers a confirmation question. Only an explicit "yes" produces a TwiML `<Dial>`. Without it, Aria offers a callback rather than pretending to transfer.

### Official-site research and lead export

* Facts are sourced from `zoho_people_kb.json`. Official pages at `zoho.com/people/features.html`, `zoho.com/people/zohopeople-pricing.html`, and `interloopconsulting.com` are fetched at most hourly when needed, with a 5-second timeout and local-KB fallback.
* Zoho renders pricing amounts in JavaScript, so the verified local plan amounts are used and should be confirmed on the official pricing page before quoting a custom enterprise deal.
* `SALES_WEBHOOK_URL` posts completed leads once per session. It is best-effort and never fails a call. Private IP targets are rejected unless `ALLOW_PRIVATE_WEBHOOK=true` is explicitly set.

---

## Security

* `CALL_API_TOKEN` protects `/call`, `/leads` for phone calls, `/web/calls`, and `/web/calls/status`. Missing token means 503; wrong token means 401.
* Twilio signatures are verified by default on `/twilio/voice`, `/twilio/respond`, `/twilio/status`, and `/twilio/handoff-status`. Disable only for local development with `TWILIO_VERIFY_SIGNATURES=false`.
* Outbound calls use explicit `POST` callbacks and carry a fresh per-call token in `/twilio/voice` and every follow-up URL. Some trial accounts omit the signature header; those requests are accepted only when the token matches that exact active call. An unknown or wrong token is still rejected with 403.
* Per-IP in-process rate limits cover calls, batch calls, status reads, lead registration, and browser turns.
* All outbound fetches are HTTPS-only, timeout-bounded, size-capped, redirect-refusing, and reject private/loopback/link-local/reserved addresses to prevent SSRF.
* Secrets are redacted from logs and error responses. Phone numbers are masked in call logs.
* CORS is restrictive: same-origin works; cross-origin is allowed only via `ALLOWED_ORIGINS`.
* The container runs as a non-root user.

In-process rate limits and session storage are per process. For multi-replica deployment, move them to shared storage such as Redis.

---

## Testing and verification

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt

python3 -m compileall -q app.py prompts.py local_demo.py security.py integrations.py qualification.py state.py twiml.py tests
python3 -m pytest -q
python3 -m pytest --cov=app --cov=security --cov=integrations --cov=qualification --cov=state --cov=twiml --cov=prompts --cov=local_demo --cov-report=term-missing
ruff check app.py prompts.py local_demo.py security.py integrations.py qualification.py state.py twiml.py tests
docker compose build
```

The suite uses a Twilio SDK stub when the SDK is not installed and mocks every LLM, Calendly, research, and webhook request. It covers auth, signatures, E.164, duplicates, batch errors, preflight errors, TwiML, silence, timeouts, turn caps, status callbacks, handoff confirmation, Calendly confirmation, browser turns, persistence, SSRF, prompt disclosure, and qualification extraction.

Live Twilio delivery, live Calendly credentials, and live handoff destination were **not** exercised because credentials and explicit approval to place paid calls were not available.

---

## Troubleshooting

| Symptom | Action |
| --- | --- |
| "We could not reach your ML server" | Check `curl https://<PUBLIC_BASE_URL>/webhook-check`. If DNS is fine, check container logs for `twilio_signature_missing` on `/twilio/voice`; the app now handles unsigned trial callbacks with a per-call token. |
| 503 from `/call` naming `PUBLIC_BASE_URL` | Preflight failed. The exact reason is returned without secrets; fix the tunnel or set `SKIP_WEBHOOK_PREFLIGHT=true` only for local tests. |
| 401 from `/call` | Send the `X-API-Token` header with the server's `CALL_API_TOKEN`. |
| 403 on a Twilio callback | Signature mismatch or unknown per-call token. Confirm `TWILIO_AUTH_TOKEN`, the exact public URL, and that the call was started by this app so its fresh token is present. |
| "unverified number" trial error | Verify the recipient in Twilio Console → Voice → Try out Voice, use the shown `from` number, and keep the destination in the sign-up country. |
| Slow or silent replies | Keep `LLM_TIMEOUT_SECONDS` at 8; the offline KB fallback engages on timeout. |

---

## Files

| File | Purpose |
| --- | --- |
| `app.py` | FastAPI routes, Twilio webhooks, state machine, qualification, persistence. |
| `security.py` | Auth, rate limits, Twilio signatures, SSRF-safe fetches, phone validation. |
| `integrations.py` | Calendly, research cache, lead export. |
| `qualification.py` | BANT, timeline and appointment extraction. |
| `state.py` | In-memory sessions and atomic transcript snapshots. |
| `twiml.py` | Valid TwiML builders with absolute callback URLs. |
| `prompts.py` | Disclosure, system prompt, greeting, live-context injection. |
| `local_demo.py` | Terminal simulator, LLM timeout/retry logic, offline KB replies, TTS sanitizer. |
| `zoho_people_kb.json` | Verified product/pricing facts and Interloop credentials. |
| `static/` | Lead console, browser voice demo, batch dialer. |
| `tests/` | Comprehensive mocked test suite. |
| `Dockerfile`, `docker-compose.yml` | Non-root container and environment wiring. |
