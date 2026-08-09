---
name: testing-outreach-assistant
description: How to run and end-to-end test the life insurance outreach assistant (FastAPI + Jinja dashboard + SQLite) locally in credential-free dry-run mode, including how to force compliance windows to be open or closed.
---

# Testing the life insurance outreach assistant

## Run it locally (no credentials needed)

```bash
cd <repo>            # e.g. /home/ubuntu/repos/jarvis
python -m venv .venv && .venv/bin/pip install -e ".[dev]"   # if no venv yet
rm -f assistant.db                                          # start from a clean DB
.venv/bin/python -m scripts.seed_demo                       # 4 leads + "New lead cadence"
.venv/bin/uvicorn app.main:app --port 8000
```

Dashboard: http://localhost:8000 · API docs: /docs

Gotchas learned the hard way:

- **Check port 8000 first.** A stale uvicorn from a *different* clone of this project may already
  own the port; your new server then exits with `[Errno 98] address already in use` while the
  browser silently talks to the old process against a different `assistant.db`. Symptom: the page
  contradicts what a direct `python -c "...compliance.check_contact_allowed(...)"` call returns.
  Fix: `ps aux | grep uvicorn`, kill the stale pid, restart, and confirm the surviving process's
  path is your repo.
- **Webhook signature checking is skipped only while no `TWILIO_AUTH_TOKEN` is set.** Once a token
  is configured, unsigned `POST /webhooks/*` returns 403 — set `TWILIO_VALIDATE_SIGNATURES=false`
  to test webhooks by hand in that setup.
- Dry-run mode is confirmed by log lines `Twilio not configured; dry-run ...` /
  `SMTP not configured; dry-run email ...` in the uvicorn log, and by `status = dry-run` on
  messages/calls. Header badges read "Twilio dry-run", "Email dry-run", "OpenAI fallback".

## Controlling compliance windows without mocking the clock

SMS/voice are gated to the lead's LOCAL 08:00–21:00 (`app/services/compliance.py`), with a
stricter 08:00–20:00 for FL/MS/AL. Email ignores the window entirely. So instead of freezing time,
**pick a state whose local time is inside/outside the window right now** and create a lead there.
Fastest way to create one (also exercises the importer):

```bash
printf 'first_name,last_name,phone,email,state,consent_evidence\nKai,N,5551230012,kai@example.com,HI,web form opt-in\n' > /tmp/l.csv
curl -s -X POST -F "file=@/tmp/l.csv" localhost:8000/api/leads/import
```

Late-UTC-evening runs: HI (UTC-10) is usually still in-window long after CA/TX/NY/FL have closed —
useful because it gives you a lead you can send to for hours. A FL lead is the cheapest way to show
the stricter 20:00 cutoff on screen.

## Useful shortcuts

- Unsubscribe token: `.venv/bin/python -c "from app.db import SessionLocal; from app.models import Lead; print(SessionLocal().get(Lead,5).unsubscribe_token)"`
  (only populated after an email has been sent). Then `GET/POST /unsubscribe/{token}`;
  a bogus token renders "Link not recognized" with HTTP 404.
- Inbound SMS: `curl -X POST localhost:8000/webhooks/sms/inbound -d From=+1555... -d Body=STOP -d MessageSid=SM1`
  (`STOP` → empty TwiML, `HELP` → help `<Message>`, anything else → drafted `<Message>`;
  unknown numbers auto-create a lead with SMS consent only).
- Attempt cap is 6 (`max_contact_attempts`); each send/call increments `leads.attempts`.
- Evidence for blocked attempts lives in the `audit_log` table (`event=contact_check, allowed=0`)
  and in the lead page's "Compliance audit trail".
- Long typed URLs in the address bar can drop characters; paste/verify tokens before concluding a
  404 is a bug.

## Blocked sends

A blocked dashboard send redirects to `/leads/{id}?error=<reason>` and renders a red banner above
the eligibility card. If a send appears to do nothing and no banner shows, that is a bug — don't
assume the send worked; check the audit trail.

## Devin Secrets Needed

None for dry-run testing. Real delivery and the Twilio Media Streams ↔ OpenAI Realtime voice
bridge would need `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`,
`OPENAI_API_KEY`, SMTP creds, and a publicly reachable `PUBLIC_BASE_URL` (tunnel).
