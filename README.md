# Jarvis — Personal AI Assistant

A Windows desktop AI assistant (CLI + PyQt6 GUI), built on the Anthropic Claude API, in the spirit of Iron Man's J.A.R.V.I.S. Runs locally, calls out to Claude for reasoning, and gives the model a real set of tools to act on the user's own machine.

## What it does

- **Voice interaction** — wake-word activation ("Hey Jarvis," via openWakeWord), local speech-to-text (faster-whisper), and 3-tier text-to-speech (ElevenLabs → Edge TTS → offline Windows SAPI5), with real-time barge-in support: the mic stays live while Jarvis talks, so you can interrupt it mid-reply and it responds to whatever you just said instead of finishing its sentence.
- **Vision** — takes a photo with the webcam or a screenshot of the desktop and answers questions about what's in frame, using Claude's vision capability. Includes a "watch mode" that periodically screenshots the screen and offers live commentary/help while you work.
- **System control** — file read/write, running shell commands, launching and closing apps, media playback control (OS-level media keys plus direct iTunes COM automation for library search/playback), system volume.
- **Email** — reads, searches (Gmail search syntax), downloads attachments from, and sends email across multiple Gmail accounts via IMAP/SMTP, including multi-file attachments.
- **Live market data** — stock and cryptocurrency price lookups via Yahoo Finance (`yfinance`), plus read-only access to a separate personal trading-bot project's logs and trade journal for portfolio Q&A.
- **Document generation** — creates Word documents (bulleted/numbered lists, headings) for anything from recipes to assignment checklists.
- **PDF reading** — extracts text from PDFs (e.g. course syllabi) so the assistant can answer questions about them directly.
- **Personal task tracking** — a JSON-backed assignment tracker (add/list/update/delete, "what's due this week") and a persistent cross-session memory system so the assistant remembers facts and preferences between separate runs.

## Architecture

- `jarvis.py` — core `Jarvis` class wrapping `client.beta.messages.tool_runner()`, plus the CLI entry point
- `gui.py` — PyQt6 desktop GUI with a custom-painted HUD dashboard, QThread workers for chat/mic/wake/speak so nothing blocks the UI thread
- `tools.py` — ~30 tools registered with the model, each a plain Python function decorated with `@beta_tool`
- `voice.py` — audio I/O: mic capture with silence detection, wake-word detection with adaptive gain normalization (the mic's input level swings wildly between recordings, so this normalizes it), and the barge-in listener that runs concurrently with TTS playback
- `config.py` — settings, loaded from a gitignored `.env`
- `assignment_tool.py` — small fixed CLI for marking a tracker item done/not-done by title text
- `build_checklist.py` — regenerates a Word checklist doc from `data/assignments.json`

## Stack

Python, Anthropic SDK (tool use / agentic tool-calling loop), PyQt6, faster-whisper, openWakeWord + ONNX Runtime, sounddevice, pygame (for interruptible audio playback), pywin32 (COM automation), pycaw (Windows Core Audio), yfinance, pypdf, python-docx, Pillow, opencv-python.

## Notes

This is a personal project, actively developed feature-by-feature — not a polished public library. `.env`, cached data, and personal files are gitignored.
