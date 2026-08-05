# Life Insurance Outreach Assistant

An AI assistant that texts and calls life insurance leads, qualifies them, and books a callback
with a licensed agent — with compliance guardrails in front of every outbound contact.

- **SMS assistant** — drafts and sends texts, replies in-thread, handles STOP/HELP deterministically
- **Realtime voice agent** — Twilio Media Streams bridged to the OpenAI Realtime API, with barge-in,
  live transcription, and an automatic post-call summary + disposition
- **Campaign cadence** — multi-step SMS/voice sequences with per-lead scheduling and retries
- **Compliance layer** — consent tracking, internal DNC, local calling windows (incl. stricter state
  cutoffs), attempt caps, and an append-only audit log of every contact decision
- **Dashboard** — lead list, conversation timeline, call outcomes, and the audit trail

The assistant always identifies itself as automated, never quotes a premium, never binds coverage,
and never asks for SSN or payment details. Those limits are enforced in the shared persona prompt
(`app/services/llm.py`).

> This is an engineering control, not legal advice. You still own TCPA/state-law review, a national
> DNC scrubbing subscription, carrier 10DLC registration, and agent licensing.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # works empty: Twilio and OpenAI both degrade to safe fallbacks
python -m scripts.seed_demo   # demo leads + a 3-touch cadence
uvicorn app.main:app --reload
```

Open http://localhost:8000 for the dashboard and http://localhost:8000/docs for the API.

Without credentials the app runs fully: Twilio calls become dry-run sends recorded in the database,
and SMS drafting falls back to deterministic copy. Nothing is dialed or texted.

## Going live

1. Set `TWILIO_*`, `OPENAI_API_KEY` and `PUBLIC_BASE_URL` (a public HTTPS URL — use `ngrok http 8000`
   in development).
2. Point your Twilio number's **A message comes in** webhook at `POST {PUBLIC_BASE_URL}/webhooks/sms/inbound`.
3. Outbound voice needs no console config: `place_call` passes the answer/status/recording callbacks.
4. Run the cadence on a schedule: `curl -X POST {PUBLIC_BASE_URL}/api/campaigns/run` from cron every
   15 minutes (or hit "Run due campaign steps" in the dashboard).
5. Register your 10DLC campaign with Twilio and enable Advanced Opt-Out before texting real numbers.

## How a call works

```
place_call() -> Twilio dials the lead
  -> POST /webhooks/voice/answer      TwiML: recording disclosure, then <Connect><Stream>
  -> WS   /webhooks/voice/stream      MediaStreamBridge relays g711_ulaw both ways:
                                        Twilio audio -> input_audio_buffer.append
                                        response.audio.delta -> Twilio media frames
                                        speech_started -> "clear" (barge-in)
  -> on hangup                        transcript -> analyze_call() -> summary + disposition
                                        disposition drives lead status (e.g. verbal opt-out -> DNC)
```

Answering machines are detected by Twilio (`machine_detection`) and get a short spoken message
instead of a conversation.

## Compliance model

Every outbound send funnels through `compliance.check_contact_allowed`, which blocks when the lead
is on the internal DNC list, has no active consent for that channel, has exhausted the attempt cap,
or is outside their local contact window — and writes an `audit_log` row either way. Replies to an
inbound text skip only the time-window check.

Consent is append-only (`consents` table): granting and revoking both add rows, so the history of
who agreed to what, and when, is reconstructable. `STOP` revokes every channel, adds the number to
the DNC list, and sets the lead to `do_not_contact`.

## Layout

```
app/config.py            settings (env-driven)
app/models.py            leads, consents, DNC, messages, calls, campaigns, audit log
app/services/compliance.py  contact gating, consent, DNC, opt-out keywords
app/services/llm.py         persona, SMS drafting, call analysis
app/services/realtime.py    Twilio <-> OpenAI Realtime audio bridge
app/services/telephony.py   Twilio client (dry-run when unconfigured)
app/services/conversation.py orchestration used by API, webhooks and scheduler
app/services/scheduler.py   campaign cadence engine
app/routers/                dashboard, REST API, Twilio webhooks
```

## Development

```bash
pytest        # 30+ tests, no network or credentials required
ruff check .
mypy app
```

The schema is created with `Base.metadata.create_all` on startup; add Alembic before the first
production deploy.
