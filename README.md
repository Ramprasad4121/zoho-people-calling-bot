# Zoho People Outbound AI Calling Bot

An outbound AI sales calling agent built for your Friday 1:00 PM demo. It calls a registered lead, pitches **Zoho People** using product knowledge extracted from [Zoho People Features](https://www.zoho.com/people/features.html?src=people-header), qualifies their HR needs, and books a demo or free trial.

## Files Included
- `app.py`: FastAPI server + Twilio Voice webhooks (`/call`, `/twilio/voice`, `/twilio/respond`) + OpenAI/Groq LLM integration.
- `prompts.py`: Outbound sales system prompt and knowledge base loader.
- `zoho_people_kb.json`: Structured product knowledge from Zoho People's official features page.
- `local_demo.py`: Local terminal simulator so you can test the conversation immediately without Twilio.
- `requirements.txt` & `.env.example`: Dependencies and configuration template.

---

## Option 0: 100% Free Browser Voice Demo (no Twilio, no phone number)

A real spoken conversation with Aria through your browser's mic and speakers. The browser provides speech recognition and speech synthesis for free; replies come from a free LLM tier, offline rules, or a local model.

1. Start the container (the blank `.env` is fine):
   ```bash
   docker compose up -d --build
   ```
2. Open **http://localhost:8000/web** in Chrome or Edge, allow microphone access, press **Start call**, then **Speak** for each turn.

### Full AI replies, still free
- **Free Groq key (recommended; no card):** create one at https://console.groq.com/keys, then put this in `.env` and run `docker compose up -d --force-recreate` (a plain `restart` does not reload `.env`):
  ```env
  LLM_API_KEY=gsk_...
  LLM_BASE_URL=https://api.groq.com/openai/v1
  LLM_MODEL=openai/gpt-oss-20b
  ```
  The free tier is rate-limited but ample for a demo. If a call fails, the demo automatically falls back to offline replies.
- **Zero accounts, fully offline:** install Ollama on the host and `ollama pull qwen2.5:3b`, then set `LLM_API_KEY=ollama`, `LLM_BASE_URL=http://host.docker.internal:11434/v1`, and `LLM_MODEL=qwen2.5:3b` in `.env`.
- **No key at all:** Aria answers from the built-in Zoho People knowledge-base rules in `local_demo.py`.

New endpoints: `GET /web` (the demo page), `POST /web/start`, `POST /web/turn`.

> Real calls to phone numbers cannot be 100% free: a carrier always charges for PSTN minutes (Twilio's $15 trial credit is one-time). Docker's MCP catalog does include a `dialer` server for outbound calls, but its free tier is a single call and is marked "coming soon"; after that it is £4.99/month. The browser demo avoids telephony costs completely.

---

## Free voice sales agent

Aria runs the full sales motion in the browser demo. She discovers needs, qualifies budget, authority, need, and timeline, handles objections with the Zoho People knowledge base, proposes a 15-minute demo or 30-day trial, and captures a date and time. The qualification panel shows Team size, Topics, Budget, Authority, Timeline, Appointment, and Next step. **Copy share link** builds a link to `/web` with the current lead fields and `autostart=1` so the recipient lands in a call.

The setup costs no money. The browser mic provides speech recognition, the browser provides speech synthesis, an optional free Groq key provides full replies, and `local_demo.py` provides offline knowledge-base answers when no key is set or the free tier is throttled. Set `SALES_WEBHOOK_URL` in `.env` to POST each finished lead as JSON. The webhook works with any free webhook receiver. Export is best effort, runs at most once per session, and never fails the call.

### Use OpenRouter instead of Groq

OpenRouter works through the same OpenAI-compatible client. The app disables reasoning for OpenRouter requests so a free reasoning model cannot spend the whole token budget on hidden thinking. Create a free key at https://openrouter.ai/keys, then set these three lines in `.env`:

```env
LLM_API_KEY=sk-or-v1-your-key
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_MODEL=nvidia/nemotron-3.5-lightning:free
```

Recreate the container with `docker compose up -d --force-recreate`. The current free model list is at https://openrouter.ai/api/v1/models; any model id ending in `:free` has zero token price. OpenRouter allows 50 free model requests per day without credits, or 1000 per day after buying at least 10 credits. Groq's free tier currently allows 1000 requests per day, so Groq stays the default for demos. OpenRouter free models can be slower, so the offline knowledge fallback still handles timeouts.

These alternatives are not free at zero cost:

- Google Speech-to-Text requires billing. The v1 API includes 60 free minutes per month, then charges per minute. See https://cloud.google.com/speech-to-text/pricing.
- Google Text-to-Speech requires billing. It includes a limited number of free characters per month, then charges per character. See https://cloud.google.com/text-to-speech/pricing.
- The Claude API has no recurring free tier. API use is billed per token. Anthropic's consumer Claude Free plan is a chat product, not API credit. See https://www.anthropic.com/pricing.
- Make and similar automation platforms charge after a limited free allowance.
- Phone calls need a paid carrier. Twilio's trial credit is one-time, and PSTN minutes are billed after it.

---

## Option 1: Real Phone Calls via Twilio (Docker)

Twilio charges per minute after free units. The current free trial includes **75 voice minutes for 30 days** (no card needed). Option 0 above stays completely free.

### 1. Twilio setup (one time)
1. Sign up at https://www.twilio.com/try-twilio and verify your personal number.
2. New trials do **not** buy a number at signup. In the Console go to **Communications → Voice → Overview → Try out Voice**, make a sample call, and copy the `from` number shown in the sample API request — that is your **trial phone number** for `TWILIO_PHONE_NUMBER`. No upgrade needed.
3. Trial calls work only to **verified recipients** (up to 5; your signup number is pre-verified) and only **within your sign-up country**. Verify extra recipients from the Try out Voice page.
4. Copy the **Account SID** and **Auth Token** from the Console home page.
5. To use a purchased number instead (multiple numbers, any destination), click **Upgrade** — that requires payment details. Trial numbers are shared and disappear after upgrade.

### 2. Free LLM key for spoken replies
Create a free key at https://console.groq.com/keys (no card). Without a key, `/twilio/respond` cannot generate replies.

### 3. Public HTTPS URL (no signup needed)
`cloudflared` and `ngrok` are both installed. Start a quick tunnel in a separate terminal:

```bash
cloudflared tunnel --url http://localhost:8000
```

It prints a URL like `https://abcd1234.trycloudflare.com` — keep that terminal open. The URL changes on restart, but you do **not** need to configure it in Twilio: the app sends it with each outbound call.

### 4. Fill in `.env` and recreate the container
```env
TWILIO_ACCOUNT_SID=ACxxxxxxxx...
TWILIO_AUTH_TOKEN=your-token
TWILIO_PHONE_NUMBER=+1xxxxxxxxxx
PUBLIC_BASE_URL=https://abcd1234.trycloudflare.com
LLM_API_KEY=gsk_your-key
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_MODEL=openai/gpt-oss-20b
```

```bash
docker compose up -d --force-recreate
```

Important: `docker compose restart` does **not** reload `.env`; use `--force-recreate` after editing it.

### 5. Place the call
```bash
curl -X POST http://localhost:8000/call \
  -H "Content-Type: application/json" \
  -d '{"to_number": "+91XXXXXXXXXX", "lead_name": "Alex", "company_name": "Acme"}'
```

Use E.164 format (`+91...`). Watch the conversation with `docker compose logs -f`. Keep the tunnel URL private — anyone with it can trigger `/call`.

### 6. Call several numbers from a web page
Open **http://localhost:8000/web/calls**. Add a name, company, and `+91...` number per row, then press **Call all numbers**. The page shows each call's live status (queued → ringing → in-progress → completed) and a transcript of what was said. Up to 20 numbers can be queued in one batch; if the free AI tier is rate-limited, replies fall back to the offline knowledge base.

Endpoints used by the page:
- `GET /web/calls` — dashboard
- `POST /web/calls` — body `{"contacts":[{"to_number":"+91...","lead_name":"...","company_name":"..."}]}`
- `GET /web/calls/status` — per-call status, duration, and transcript

### Troubleshooting: `No Twilio trial phone number is assigned for voice calls to this destination number`
Twilio trial accounts can call only **verified recipient numbers**, at most 5 per account, and only **within your sign-up country**. Your signup number is already verified.

1. Twilio Console → **Communications → Voice → Overview → Try out Voice**.
2. Enter the exact `to` number from the failed call (E.164, e.g. `+15551234567`) and complete the verification code challenge (SMS or voice call).
3. In that same page, make a sample call to the number and copy the `from` number shown in the sample API request. Trial numbers are assigned per destination — it may differ from the one currently in `.env`.
4. If it differs, set `TWILIO_PHONE_NUMBER=<that from number>` in `.env`, then run `docker compose up -d --force-recreate`.

If the recipient is outside your sign-up country, verification alone will not work — that requires upgrading the account with payment details.

### Troubleshooting: "We could not reach your TwiML server"
Twilio could not fetch `https://<your-tunnel>/twilio/voice`. Quick tunnel URLs are temporary — when the tunnel stops, the hostname disappears. Restart it and update `.env`:

```bash
cloudflared tunnel --url http://localhost:8000
```

Copy the new `https://...trycloudflare.com` URL into `PUBLIC_BASE_URL`, then run `docker compose up -d --force-recreate`. Verify with:

```bash
curl -X POST https://<new-url>/twilio/voice -d 'CallSid=CAtest123'
```

It should return XML starting with `<?xml version="1.0" ...><Response>`.

The app avoids a separate `<Gather action=...>` callback: every spoken turn returns to `/twilio/voice` with `SpeechResult`, so there is only one public webhook URL for Twilio to fetch.

---

## Option 2: Fastest No-Code Demo Backup (10 Minutes)
If you want ultra-low-latency voice (~500ms) without managing Twilio webhooks yourself:
1. Sign up on **Bolna** (`bolna.dev` — built for Indian +91 numbers), **Vapi** (`vapi.ai`), or **Retell AI** (`retellai.com`).
2. Create an Outbound Assistant and paste the system prompt generated by `prompts.py` plus `zoho_people_kb.json` as the knowledge base.
3. Enter the interviewer's phone number and click **Call**.
