import sys
import time
from typing import Callable, Optional

from anthropic import Anthropic

from config import ANTHROPIC_API_KEY, ASSISTANT_NAME, MODEL
from tools import ALL_TOOLS, recall

SYSTEM_PROMPT = f"""You are {ASSISTANT_NAME}, a personal AI assistant running on the user's Windows machine, \
in the spirit of Iron Man's J.A.R.V.I.S. — capable, direct, and a little dry, never gushing.

You have tools to:
- Inspect and control the local system: system info, list/read/write files, run shell commands, open or close applications.
- Control whatever media is currently playing: play/pause, next/previous track, volume, mute.
- Search the iTunes library and start playing a specific song, artist, or album, or close iTunes entirely.
- Read or set the system volume to an exact percentage.
- Read the user's trading strategy logs, state files, and trade journal in their Trading folder (read-only).
- Look up current stock or cryptocurrency prices (via Yahoo Finance) for any ticker, not just ones the user is actively trading.
- Track school assignments: add, list, update, and delete them, and check the upcoming workload.
- Look through the webcam, or take a screenshot of the desktop, to answer questions about what's \
currently in view or on screen.
- Remember facts and preferences about the user across sessions, recall them, or forget one.
- Read, search (using Gmail's own search syntax), or download attachments from the user's Gmail (personal or \
business account), or send an email — optionally with an attachment — on their behalf. Default to the personal \
account unless the user says otherwise or context makes it obvious.
- Create Word documents — e.g. for printing or emailing as an attachment.

Rules:
- Writing files, running commands, opening or closing applications, quitting iTunes, forgetting a memory, downloading email \
attachments, sending an email, creating a Word document, and using the camera or screen capture each ask the \
user for confirmation directly in the terminal first — the tool handles that itself, don't ask again yourself.
- Before sending an email, make sure the recipient, subject, and body are actually what the user intends — the \
confirmation prompt shows them the full text, but don't send something vague or guessed at.
- When the user states a lasting preference, corrects you, or shares context worth keeping beyond this \
conversation (not a one-off fact), save it with remember — don't wait to be asked. Don't remember passwords, \
API keys, account numbers, or anything already tracked elsewhere (assignments, trading data).
- Never guess at file contents, trading numbers, assignment due dates, system state, or what the camera/screen \
shows — read/look with your tools instead. Only use the camera or screen capture when actually relevant to what's \
being asked, not as a running background check — a screenshot can expose whatever else the user has open.
- If a request needs system access you don't have a tool for, say so plainly rather than pretending to do it.
- Proactively mention it when upcoming_workload shows overdue items or a heavy day, even if not asked directly.
- On trading and financial questions, help analyze and summarize — never present yourself as a licensed financial \
advisor, and don't imply you've placed or would place a real trade.
- Keep replies concise and conversational; skip preamble like "Certainly!" or "I'd be happy to help."
"""


class Jarvis:
    def __init__(self):
        if not ANTHROPIC_API_KEY:
            raise SystemExit(
                "No ANTHROPIC_API_KEY found. Add it to the .env file next to jarvis.py:\n"
                "    ANTHROPIC_API_KEY=sk-ant-...\n"
            )
        self.client = Anthropic(api_key=ANTHROPIC_API_KEY)
        self.messages: list[dict] = []
        self.system_prompt = self._build_system_prompt()

    @staticmethod
    def _build_system_prompt() -> str:
        # Each run otherwise starts from zero — this is what gives Jarvis continuity between
        # separate sessions instead of only within one conversation.
        memories = recall.func("")
        if memories.startswith("Nothing remembered"):
            return SYSTEM_PROMPT
        return f"{SYSTEM_PROMPT}\nWhat you already know about the user, from past sessions:\n{memories}\n"

    def send(self, user_input: str, on_tool_use: Optional[Callable[[str, dict], None]] = None) -> str:
        """Send a user message and return Jarvis's final reply.

        on_tool_use, if given, is called as on_tool_use(tool_name, tool_input) each time a tool
        runs — lets a front-end (GUI) show tool activity instead of it going to stdout.
        """
        self.messages.append({"role": "user", "content": user_input})

        kwargs = {}
        if not MODEL.startswith("claude-haiku"):
            # Haiku 4.5 doesn't support adaptive thinking or the effort parameter (400s if sent).
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": "medium"}

        runner = self.client.beta.messages.tool_runner(
            model=MODEL,
            max_tokens=4096,
            system=self.system_prompt,
            tools=ALL_TOOLS,
            messages=self.messages,
            **kwargs,
        )

        last_message = None
        for message in runner:
            last_message = message
            for block in message.content:
                if block.type == "tool_use":
                    if on_tool_use is not None:
                        on_tool_use(block.name, block.input)
                    else:
                        print(f"  -> {block.name}({block.input})")

        final_text = ""
        if last_message is not None:
            final_text = next((b.text for b in last_message.content if b.type == "text"), "")

        # Keep history as plain text turns rather than mirroring every tool_use/tool_result
        # block — simpler bookkeeping, at the cost of Jarvis not "remembering" exactly which
        # tools it called in earlier turns (only what it concluded from them).
        self.messages.append({"role": "assistant", "content": final_text or "(no response)"})
        return final_text


_WATCH_INTERVAL_SECONDS = 20  # cadence for "watch" mode's repeated screenshots
_WATCH_PROMPT = (
    "The user is working on something on their screen and wants ongoing help, checked in on "
    "every few seconds. Briefly describe what they appear to be doing right now, and offer any "
    "relevant help, corrections, or next steps if something stands out. If nothing meaningfully "
    "new or actionable is visible since a moment ago, just say so briefly rather than repeating "
    "a full description each time."
)


def _handle_turn(jarvis: Jarvis, voice_module, user_input: str) -> None:
    """Sends user_input to Jarvis, prints and speaks the reply, and — since speak_with_barge_in
    keeps the mic live the whole time — if the user talks over the reply, immediately handles
    what they said as the next turn instead of waiting for the reply to finish or requiring the
    wake word again.
    """
    print(f"You (voice): {user_input}")
    try:
        reply = jarvis.send(user_input)
    except Exception as e:
        print(f"[error] {e}", file=sys.stderr)
        return
    print(f"\n{ASSISTANT_NAME}: {reply}\n")
    if not reply:
        return

    try:
        interruption = voice_module.speak_with_barge_in(reply)
    except Exception as e:
        print(f"[voice error] {e}", file=sys.stderr)
        return
    if interruption:
        print("\n🎤 (you cut in — stopping to listen)")
        _handle_turn(jarvis, voice_module, interruption)


def _voice_turn(jarvis: Jarvis, voice_module, max_seconds: Optional[float] = None) -> bool:
    """One full voice interaction: listen, then hand off to _handle_turn. Shared by both the
    'mic' (single listen) and 'wake' (continuous) CLI commands.

    Returns True if anything was actually heard and handled, False on silence — 'wake' mode uses
    this to know whether to keep listening for a follow-up or fall back to requiring the wake
    word again.
    """
    try:
        kwargs = {} if max_seconds is None else {"max_seconds": max_seconds}
        user_input = voice_module.listen_and_transcribe(**kwargs)
    except Exception as e:
        print(f"[voice error] {e}", file=sys.stderr)
        return False
    if not user_input:
        print("(didn't catch anything)\n")
        return False

    _handle_turn(jarvis, voice_module, user_input)
    return True


def main():
    # Windows consoles often default to a codepage (e.g. cp1252) that can't represent arbitrary
    # Unicode — real content Jarvis prints (email bodies, tool output, etc.) can contain emoji or
    # other characters that codepage has no mapping for, crashing every print() that hits one.
    # Forcing UTF-8 here, with lossy replacement as a last resort, makes the whole CLI resilient
    # instead of patching individual print calls as each one happens to get hit.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    print(f"{ASSISTANT_NAME} online. Type 'exit' to quit, 'mic' to speak once, 'wake' for "
          f"hands-free \"Hey Jarvis\" mode, or 'watch' to have Jarvis periodically screenshot "
          f"your screen and comment while you work.\n")
    jarvis = Jarvis()
    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break
        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            print("Goodbye.")
            break

        if user_input.lower() in ("mic", "voice", "listen"):
            import voice  # deferred: skip loading Whisper/audio deps for text-only sessions
            print("🎤 Listening... (speak now, stops once you go quiet)")
            _voice_turn(jarvis, voice)
            continue

        if user_input.lower() in ("wake", "wakeword", "wake word"):
            import voice  # deferred: skip loading Whisper/wake-word deps for text-only sessions
            print('🎧 Listening for "Hey Jarvis"... (Ctrl+C to stop and return to typing)')
            try:
                voice.wait_for_wake_word()
                print('\n🎧 Wake word heard! Listening from here on — no need to say it again.')
                while True:
                    print("🎤 Listening... (speak now, stops once you go quiet)")
                    _voice_turn(jarvis, voice)
            except KeyboardInterrupt:
                print("\n(stopped listening)\n")
            continue

        if user_input.lower() in ("watch", "watch screen"):
            import tools
            print(
                "This repeatedly screenshots your whole screen (every "
                f"{_WATCH_INTERVAL_SECONDS}s) so Jarvis can comment and help while you work — "
                "unlike a one-off look, it keeps capturing until you stop it."
            )
            if not tools._confirm("Start watching your screen periodically until you stop it?"):
                print("Okay, not starting.\n")
                continue
            print(f'👀 Watching your screen every {_WATCH_INTERVAL_SECONDS}s — Ctrl+C to stop.\n')
            try:
                while True:
                    commentary = tools.describe_current_screen(_WATCH_PROMPT)
                    print(f"{ASSISTANT_NAME}: {commentary}\n")
                    time.sleep(_WATCH_INTERVAL_SECONDS)
            except KeyboardInterrupt:
                print("\n(stopped watching the screen)\n")
            continue

        try:
            reply = jarvis.send(user_input)
        except Exception as e:
            print(f"[error] {e}", file=sys.stderr)
            continue
        print(f"\n{ASSISTANT_NAME}: {reply}\n")


if __name__ == "__main__":
    main()
