import base64
import ctypes
import json
import os
import platform
import re
import shutil
import subprocess
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import cv2
from anthropic import Anthropic, beta_tool

from config import ANTHROPIC_API_KEY, EMAIL_ACCOUNTS, MODEL, ROOT_DIR, TRADING_DIR


# Front-ends (CLI, GUI) can override how confirmation is asked — see set_confirm_handler.
# Defaults to a terminal y/n prompt so the CLI keeps working with no setup.
_confirm_handler = None


def set_confirm_handler(handler) -> None:
    """Register a callable(prompt: str) -> bool used by every confirm-gated tool.
    Call this once at startup from a front-end that isn't the terminal (e.g. the GUI),
    before any tool runs.
    """
    global _confirm_handler
    _confirm_handler = handler


def _confirm(prompt: str) -> bool:
    if _confirm_handler is not None:
        return _confirm_handler(prompt)
    answer = input(f"\n[confirm] {prompt} (y/n): ").strip().lower()
    return answer in ("y", "yes")


def _resolve_path(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT_DIR / p
    return p.resolve()


def _list_directory(path: str) -> str:
    target = _resolve_path(path)
    if not target.exists():
        return f"Error: path does not exist: {target}"
    if not target.is_dir():
        return f"Error: not a directory: {target}"
    entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    lines = []
    for e in entries:
        kind = "DIR " if e.is_dir() else "FILE"
        size = "" if e.is_dir() else f" ({e.stat().st_size} bytes)"
        lines.append(f"{kind} {e.name}{size}")
    return "\n".join(lines) if lines else "(empty directory)"


# ---------------------------------------------------------------------------
# System control
# ---------------------------------------------------------------------------

@beta_tool
def system_info() -> str:
    """Get information about the local machine: OS, CPU architecture, Python version, and disk usage."""
    info = {
        "os": platform.system(),
        "os_version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python_version": platform.python_version(),
    }
    try:
        total, used, free = shutil.disk_usage(ROOT_DIR.anchor or "C:\\")
        info["disk_total_gb"] = round(total / 1e9, 1)
        info["disk_used_gb"] = round(used / 1e9, 1)
        info["disk_free_gb"] = round(free / 1e9, 1)
    except OSError:
        pass
    return json.dumps(info, indent=2)


@beta_tool
def list_directory(path: str = ".") -> str:
    """List files and folders in a directory.

    Args:
        path: Directory path, relative to Jarvis's own folder or absolute. Defaults to Jarvis's own folder.
            To list the Trading folder specifically, use list_trading_files instead.
    """
    return _list_directory(path)


def _read_pdf_text(target: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return "Error: pypdf isn't installed — run 'pip install pypdf' to enable reading PDF files."
    try:
        reader = PdfReader(str(target))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as e:
        return f"Error reading PDF: {e}"
    return "\n\n".join(f"--- Page {i + 1} ---\n{text}" for i, text in enumerate(pages))


@beta_tool
def read_file(path: str, max_chars: int = 8000) -> str:
    """Read the contents of a text file, or extract the text from a PDF.

    Args:
        path: File path, relative to Jarvis's own folder or absolute (e.g. an absolute path into the
            Trading folder to read a specific trading file not covered by the dedicated trading tools,
            or a downloaded syllabus PDF).
        max_chars: Maximum number of characters to return; longer files are truncated.
    """
    target = _resolve_path(path)
    if not target.exists() or not target.is_file():
        return f"Error: file does not exist: {target}"
    if target.suffix.lower() == ".pdf":
        text = _read_pdf_text(target)
    else:
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"Error reading file: {e}"
    if len(text) > max_chars:
        return text[:max_chars] + f"\n...[truncated, {len(text) - max_chars} more characters]"
    return text


@beta_tool
def write_file(path: str, content: str) -> str:
    """Write text content to a file, creating it or overwriting it if it exists.
    Always asks the user to confirm in the terminal before writing.

    Args:
        path: File path, relative to Jarvis's own folder or absolute.
        content: The text content to write.
    """
    target = _resolve_path(path)
    if not _confirm(f"Write {len(content)} characters to {target}?"):
        return "User declined the write."
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} characters to {target}"


@beta_tool
def run_command(command: str) -> str:
    """Run a shell command on the user's machine and return its output.
    Always asks the user to confirm in the terminal before running.

    Args:
        command: The command to execute, e.g. "dir" or "python script.py". Runs from Jarvis's own folder
            (use an absolute path or "cd" within the command to operate elsewhere, e.g. the Trading folder).
    """
    if not _confirm(f"Run this command?\n    {command}"):
        return "User declined to run the command."
    try:
        result = subprocess.run(
            command, shell=True, cwd=ROOT_DIR,
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "Error: command timed out after 120 seconds."
    output = (result.stdout or "") + (result.stderr or "")
    output = output[-6000:]
    return f"Exit code: {result.returncode}\n{output}"


@beta_tool
def open_application(name: str) -> str:
    """Open an application, file, or folder with its default program.
    Always asks the user to confirm in the terminal first.

    Args:
        name: The application name (e.g. "notepad", "chrome") or a file/folder path to open.
    """
    if not _confirm(f"Open '{name}'?"):
        return "User declined."
    try:
        os.startfile(name)  # type: ignore[attr-defined]
        return f"Opened {name}"
    except OSError:
        try:
            subprocess.Popen(["cmd", "/c", "start", "", name], shell=False)
            return f"Opened {name}"
        except Exception as e:
            return f"Error opening {name}: {e}"


# ---------------------------------------------------------------------------
# Media control
# ---------------------------------------------------------------------------

# Standard Windows virtual-key codes for the hardware media keys. Sending one of these is
# indistinguishable to the OS from a physical keypress, so it works with whatever's currently
# playing — Spotify, a browser tab, Windows Media Player, anything — with no per-app integration.
_MEDIA_KEYS = {
    "play_pause": 0xB3,
    "next": 0xB0,
    "previous": 0xB1,
    "stop": 0xB2,
    "volume_up": 0xAF,
    "volume_down": 0xAE,
    "mute": 0xAD,
}
_KEYEVENTF_KEYUP = 0x0002


@beta_tool
def media_control(action: str) -> str:
    """Control whatever media is currently playing on the system, by sending the same OS-level
    media key a keyboard's play/pause button would — works with Spotify, a browser tab, Windows
    Media Player, or anything else, without needing to know what app is actually playing.

    Args:
        action: One of "play_pause", "next", "previous", "stop", "volume_up", "volume_down", "mute".
    """
    vk = _MEDIA_KEYS.get(action.lower())
    if vk is None:
        return f"Error: unknown action '{action}'. Valid actions: {', '.join(_MEDIA_KEYS)}."
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)
    return f"Sent media key: {action}"


def _retry_itunes_com(action, attempts: int = 5):
    """Call the zero-arg `action` (some iTunes COM interaction), retrying on the transient
    "server busy" error (RPC_E_SERVERCALL_RETRYLATER) with increasing backoff — this covers both
    a brief busy state right after a prior automation call, and iTunes cold-starting from fully
    closed, which can take several seconds before it's actually responsive. Never sleeps after
    the final attempt, since nothing would benefit from that wait.

    Returns (action's return value, None) on success, or (None, last_exception) if every attempt
    failed.
    """
    import time

    import pywintypes

    last_error = None
    for attempt in range(attempts):
        try:
            return action(), None
        except pywintypes.com_error as e:
            last_error = e
            if attempt < attempts - 1:
                time.sleep(attempt + 1)
    return None, last_error


@beta_tool
def play_music(query: str = "") -> str:
    """Launch iTunes (this machine's player for Apple Music) and start playing a song — unlike
    media_control, this can start playback from nothing rather than only toggling what's already
    loaded. With a query, searches by title/artist/album and plays the first match — this reaches
    the full Apple Music catalog, not just tracks already downloaded to this computer, so it can
    play songs the user doesn't already have. With no query, just starts the first library track.

    Args:
        query: A song title, artist, or album to search for. Leave blank to just start something playing.
    """
    try:
        import win32com.client
    except ImportError:
        return "Error: pywin32 isn't installed — run 'pip install pywin32' to enable music control."

    def _do_play():
        itunes = win32com.client.Dispatch("iTunes.Application")
        library = itunes.LibraryPlaylist

        track = None
        if query:
            matches = library.Search(query, 0)  # 0 = search all fields (title, artist, album, composer)
            if matches is not None and matches.Count > 0:
                track = matches.Item(1)
        elif library.Tracks.Count > 0:
            track = library.Tracks.Item(1)

        if track is None:
            return None
        track.Play()
        return f"Playing: {track.Name} by {track.Artist}"

    result, error = _retry_itunes_com(_do_play)  # cold-start allowance: up to ~15s across 5 attempts
    if error is not None:
        return f"Error controlling iTunes (still busy after cold-start retries): {error}"
    if result is None:
        return f"No track found matching '{query}'." if query else "The iTunes library appears to be empty."
    return result


@beta_tool
def quit_music() -> str:
    """Close iTunes (this machine's player for Apple Music) entirely, stopping playback.
    Always asks the user to confirm first.
    """
    if not _confirm("Quit iTunes / Apple Music?"):
        return "User declined."
    try:
        import win32com.client
    except ImportError:
        return "Error: pywin32 isn't installed — run 'pip install pywin32' to enable music control."

    def _do_quit():
        itunes = win32com.client.Dispatch("iTunes.Application")
        itunes.Quit()
        return "iTunes closed."

    result, error = _retry_itunes_com(_do_quit, attempts=3)  # no cold-start case here, shorter budget
    if error is not None:
        return f"Error closing iTunes: {error}"
    return result


def _get_volume_interface():
    from pycaw.pycaw import AudioUtilities

    return AudioUtilities.GetSpeakers().EndpointVolume


@beta_tool
def set_volume(percent: int) -> str:
    """Set the system output volume to an exact percentage — unlike media_control's volume_up/
    volume_down, which only nudge the current level up or down a step.

    Args:
        percent: Target volume, 0-100.
    """
    percent = max(0, min(100, percent))
    try:
        volume = _get_volume_interface()
        volume.SetMasterVolumeLevelScalar(percent / 100.0, None)
        return f"Volume set to {percent}%"
    except ImportError:
        return "Error: pycaw isn't installed — run 'pip install pycaw comtypes' to enable volume control."
    except Exception as e:
        return f"Error setting volume: {e}"


@beta_tool
def get_volume() -> str:
    """Get the current system output volume as a percentage."""
    try:
        volume = _get_volume_interface()
        percent = round(volume.GetMasterVolumeLevelScalar() * 100)
        return f"Current volume: {percent}%"
    except ImportError:
        return "Error: pycaw isn't installed — run 'pip install pycaw comtypes' to enable volume control."
    except Exception as e:
        return f"Error reading volume: {e}"


# ---------------------------------------------------------------------------
# Trading data (read-only)
# ---------------------------------------------------------------------------

_KNOWN_STRATEGIES = ("doge", "link", "pepe")


@beta_tool
def list_trading_files() -> str:
    """List the trading strategy files in the Trading folder: strategy logs, state files, and the trade journal."""
    return _list_directory(str(TRADING_DIR))


@beta_tool
def read_strategy_state(strategy: str) -> str:
    """Read the current JSON state (position, entry price, etc.) for a trading strategy.

    Args:
        strategy: The strategy name, e.g. "doge", "link", or "pepe".
    """
    path = TRADING_DIR / f"{strategy.lower()}_strategy_state.json"
    if not path.exists():
        return f"No state file found for strategy '{strategy}'. Known strategies: {', '.join(_KNOWN_STRATEGIES)}."
    return path.read_text(encoding="utf-8")


@beta_tool
def read_strategy_log(strategy: str, tail_lines: int = 50) -> str:
    """Read the most recent lines from a trading strategy's log file.

    Args:
        strategy: The strategy name, e.g. "doge", "link", or "pepe".
        tail_lines: How many lines from the end of the log to return.
    """
    path = TRADING_DIR / f"{strategy.lower()}_strategy_log.txt"
    if not path.exists():
        return f"No log file found for strategy '{strategy}'. Known strategies: {', '.join(_KNOWN_STRATEGIES)}."
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-tail_lines:]) if lines else "(empty log)"


@beta_tool
def read_trade_journal(tail_entries: int = 20) -> str:
    """Read the most recent entries from the trade journal (one JSON object per line).

    Args:
        tail_entries: How many of the most recent journal entries to return.
    """
    path = TRADING_DIR / "trade_journal.jsonl"
    if not path.exists():
        return "No trade journal found."
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return "\n".join(lines[-tail_entries:]) if lines else "(empty journal)"


def _format_price(value: float) -> str:
    # Sub-$1 assets (many cryptos) need more decimal places to be meaningful than a $300 stock does.
    return f"{value:,.4f}" if value < 1 else f"{value:,.2f}"


@beta_tool
def get_market_price(symbols: str) -> str:
    """Look up the current price and today's change for one or more stock or cryptocurrency
    ticker symbols, via Yahoo Finance. Not limited to symbols in the Trading folder.

    Args:
        symbols: One or more ticker symbols, comma-separated (e.g. "AAPL" or "AAPL,TSLA,MSFT").
            For cryptocurrency, use the "-USD" suffix (e.g. "BTC-USD", "DOGE-USD", "ETH-USD",
            "ADA-USD", "LINK-USD").
    """
    try:
        import yfinance as yf
    except ImportError:
        return "Error: yfinance isn't installed — run 'pip install yfinance' to enable price lookups."

    tickers = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not tickers:
        return "Error: no symbols given."

    lines = []
    for symbol in tickers:
        try:
            info = yf.Ticker(symbol).fast_info
            price = info.last_price
            if price is None:
                lines.append(f"{symbol}: no price data found — check the symbol.")
                continue
            prev_close = info.previous_close
            if prev_close:
                change = price - prev_close
                pct = change / prev_close * 100
                sign = "+" if change >= 0 else ""
                lines.append(
                    f"{symbol}: ${_format_price(price)} ({sign}{_format_price(change)}, {sign}{pct:.2f}% today)"
                )
            else:
                lines.append(f"{symbol}: ${_format_price(price)}")
        except Exception as e:
            lines.append(f"{symbol}: error looking up price — {e}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# School / assignment tracking
# ---------------------------------------------------------------------------

_DATA_DIR = ROOT_DIR / "data"
_ASSIGNMENTS_FILE = _DATA_DIR / "assignments.json"


def _load_assignments() -> list[dict]:
    if not _ASSIGNMENTS_FILE.exists():
        return []
    return json.loads(_ASSIGNMENTS_FILE.read_text(encoding="utf-8"))


def _save_assignments(assignments: list[dict]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _ASSIGNMENTS_FILE.write_text(json.dumps(assignments, indent=2), encoding="utf-8")


def _parse_date(value: str) -> Optional[str]:
    """Validate a YYYY-MM-DD string; returns an error message, or None if valid."""
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return f"Error: due_date must be in YYYY-MM-DD format, got '{value}'."
    return None


@beta_tool
def add_assignment(class_name: str, title: str, due_date: str, priority: str = "medium", notes: str = "") -> str:
    """Add a new school assignment to track.

    Args:
        class_name: The class or course name, e.g. "Calculus II".
        title: What the assignment is, e.g. "Problem Set 4" or "Essay draft".
        due_date: Due date in YYYY-MM-DD format.
        priority: "low", "medium", or "high".
        notes: Any extra context, e.g. "group project" or "open book".
    """
    error = _parse_date(due_date)
    if error:
        return error
    assignments = _load_assignments()
    entry = {
        "id": uuid.uuid4().hex[:8],
        "class": class_name,
        "title": title,
        "due_date": due_date,
        "priority": priority.lower(),
        "status": "not_started",
        "notes": notes,
    }
    assignments.append(entry)
    _save_assignments(assignments)
    return f"Added [{entry['id']}] {class_name} — {title}, due {due_date} ({priority})"


@beta_tool
def list_assignments(include_completed: bool = False) -> str:
    """List tracked school assignments, soonest due first.

    Args:
        include_completed: Whether to include assignments already marked done.
    """
    assignments = _load_assignments()
    if not include_completed:
        assignments = [a for a in assignments if a.get("status") != "done"]
    if not assignments:
        return "No assignments tracked." if include_completed else "No pending assignments tracked."
    assignments.sort(key=lambda a: a.get("due_date", ""))
    lines = [
        f"[{a['id']}] {a['due_date']} — {a['class']}: {a['title']} "
        f"(priority: {a.get('priority', 'medium')}, status: {a.get('status', 'not_started')})"
        + (f" — {a['notes']}" if a.get("notes") else "")
        for a in assignments
    ]
    return "\n".join(lines)


@beta_tool
def update_assignment(
    assignment_id: str,
    status: Optional[str] = None,
    due_date: Optional[str] = None,
    priority: Optional[str] = None,
    notes: Optional[str] = None,
) -> str:
    """Update fields on an existing assignment — e.g. mark it done or reschedule it.
    Only pass the fields that should change; everything else stays as-is.

    Args:
        assignment_id: The short id shown by list_assignments, e.g. "a1b2c3d4".
        status: New status: "not_started", "in_progress", or "done".
        due_date: New due date in YYYY-MM-DD format.
        priority: New priority: "low", "medium", or "high".
        notes: New notes text.
    """
    assignments = _load_assignments()
    for a in assignments:
        if a["id"] == assignment_id:
            if status is not None:
                a["status"] = status.lower()
            if due_date is not None:
                error = _parse_date(due_date)
                if error:
                    return error
                a["due_date"] = due_date
            if priority is not None:
                a["priority"] = priority.lower()
            if notes is not None:
                a["notes"] = notes
            _save_assignments(assignments)
            return f"Updated [{assignment_id}]: {a['class']} — {a['title']}"
    return f"No assignment found with id '{assignment_id}'."


@beta_tool
def delete_assignment(assignment_id: str) -> str:
    """Remove a tracked assignment entirely. Always asks the user to confirm first.

    Args:
        assignment_id: The short id shown by list_assignments.
    """
    assignments = _load_assignments()
    match = next((a for a in assignments if a["id"] == assignment_id), None)
    if match is None:
        return f"No assignment found with id '{assignment_id}'."
    if not _confirm(f"Delete assignment [{assignment_id}] {match['class']} — {match['title']}?"):
        return "User declined the delete."
    assignments = [a for a in assignments if a["id"] != assignment_id]
    _save_assignments(assignments)
    return f"Deleted [{assignment_id}] {match['class']} — {match['title']}"


@beta_tool
def upcoming_workload(days_ahead: int = 7) -> str:
    """Get a day-by-day breakdown of assignments due in the next N days, plus any overdue ones.
    Flags any day with 3 or more things due as a heavy day, for spotting overload before it hits.

    Args:
        days_ahead: How many days ahead to look, starting today.
    """
    assignments = [a for a in _load_assignments() if a.get("status") != "done"]
    today = date.today()
    cutoff = today.toordinal() + days_ahead

    by_day: dict[str, list[dict]] = {}
    overdue = []
    for a in assignments:
        try:
            d = datetime.strptime(a["due_date"], "%Y-%m-%d").date()
        except (ValueError, KeyError):
            continue
        if d.toordinal() < today.toordinal():
            overdue.append(a)
        elif d.toordinal() <= cutoff:
            by_day.setdefault(a["due_date"], []).append(a)

    lines = []
    if overdue:
        lines.append(f"OVERDUE ({len(overdue)}):")
        for a in overdue:
            lines.append(f"  {a['due_date']} — {a['class']}: {a['title']}")
        lines.append("")

    if not by_day:
        lines.append(f"Nothing due in the next {days_ahead} days.")
    else:
        for day in sorted(by_day):
            items = by_day[day]
            flag = "  <- heavy day" if len(items) >= 3 else ""
            lines.append(f"{day} ({len(items)} due){flag}:")
            for a in items:
                lines.append(f"  {a['class']}: {a['title']} (priority: {a.get('priority', 'medium')})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Memory (persists across sessions — Jarvis has no memory of past runs otherwise)
# ---------------------------------------------------------------------------

_MEMORY_FILE = _DATA_DIR / "memory.json"


def _load_memories() -> list[dict]:
    if not _MEMORY_FILE.exists():
        return []
    return json.loads(_MEMORY_FILE.read_text(encoding="utf-8"))


def _save_memories(memories: list[dict]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _MEMORY_FILE.write_text(json.dumps(memories, indent=2), encoding="utf-8")


@beta_tool
def remember(note: str) -> str:
    """Save a fact, preference, or piece of context about the user worth keeping beyond this
    conversation — a stated preference, an ongoing project, a correction they gave you. Don't
    save what's already tracked elsewhere (assignments, trading data) or anything sensitive like
    passwords, API keys, or account numbers.

    Args:
        note: The fact or preference to remember, written plainly, e.g. "Prefers concise replies"
            or "Runs a life insurance agency and is exploring outreach tools".
    """
    memories = _load_memories()
    entry = {
        "id": uuid.uuid4().hex[:8],
        "note": note,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    memories.append(entry)
    _save_memories(memories)
    return f"Remembered [{entry['id']}]: {note}"


@beta_tool
def recall(query: str = "") -> str:
    """Look through what's been remembered about the user so far. With a query, filters by
    substring match; with no query, returns everything.

    Args:
        query: Text to search for within saved memories. Leave blank to list everything.
    """
    memories = _load_memories()
    if query:
        q = query.lower()
        memories = [m for m in memories if q in m.get("note", "").lower()]
    if not memories:
        return "Nothing remembered yet." if not query else f"No memories matching '{query}'."
    return "\n".join(f"[{m['id']}] ({m['created_at']}) {m['note']}" for m in memories)


@beta_tool
def forget(memory_id: str) -> str:
    """Delete a saved memory. Always asks the user to confirm first.

    Args:
        memory_id: The short id shown by recall, e.g. "a1b2c3d4".
    """
    memories = _load_memories()
    match = next((m for m in memories if m["id"] == memory_id), None)
    if match is None:
        return f"No memory found with id '{memory_id}'."
    if not _confirm(f"Forget memory [{memory_id}]: \"{match['note']}\"?"):
        return "User declined."
    memories = [m for m in memories if m["id"] != memory_id]
    _save_memories(memories)
    return f"Forgot [{memory_id}]: {match['note']}"


# ---------------------------------------------------------------------------
# Camera & screen / vision
# ---------------------------------------------------------------------------

_CAMERA_DIR = ROOT_DIR / "data" / "camera"
_SCREEN_DIR = ROOT_DIR / "data" / "screenshots"
_vision_client: Optional[Anthropic] = None


def _get_vision_client() -> Anthropic:
    global _vision_client
    if _vision_client is None:
        _vision_client = Anthropic(api_key=ANTHROPIC_API_KEY)
    return _vision_client


def _capture_frame():
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    if not cap.isOpened():
        cap.release()
        raise RuntimeError("Could not access the webcam — check it's connected and not in use by another app.")
    try:
        # A couple of throwaway reads let auto-exposure/focus settle before the real shot.
        frame = None
        for _ in range(3):
            ok, frame = cap.read()
        if not ok or frame is None:
            raise RuntimeError("Failed to capture an image from the webcam.")
        return frame
    finally:
        cap.release()


@beta_tool
def look_at_camera(question: str = "Describe what you see in detail.") -> str:
    """Take a photo with the webcam right now and look at it to answer a question about what's
    visible — e.g. who or what is in frame, what's on the desk, or reading text held up to the
    camera. Always takes a fresh photo; never assume what the camera currently sees.
    Always asks the user to confirm before taking the photo.

    Args:
        question: What to look for, describe, or answer about the captured image.
    """
    if not _confirm("Take a photo with the webcam right now?"):
        return "User declined to use the camera."

    try:
        frame = _capture_frame()
    except RuntimeError as e:
        return f"Error: {e}"

    ok, buf = cv2.imencode(".jpg", frame)
    if not ok:
        return "Error: failed to encode the captured image."
    image_bytes = buf.tobytes()

    _CAMERA_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_path = _CAMERA_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
    snapshot_path.write_bytes(image_bytes)

    response = _get_vision_client().messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.standard_b64encode(image_bytes).decode("utf-8"),
                    },
                },
                {"type": "text", "text": question},
            ],
        }],
    )
    description = next((b.text for b in response.content if b.type == "text"), "")
    return description or "(no description returned)"


def _grab_screen_png() -> bytes:
    from PIL import ImageGrab
    image = ImageGrab.grab(all_screens=True)
    import io
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _ask_vision(image_bytes: bytes, media_type: str, question: str) -> str:
    response = _get_vision_client().messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64.standard_b64encode(image_bytes).decode("utf-8"),
                    },
                },
                {"type": "text", "text": question},
            ],
        }],
    )
    description = next((b.text for b in response.content if b.type == "text"), "")
    return description or "(no description returned)"


@beta_tool
def look_at_screen(question: str = "Describe what you see in detail.") -> str:
    """Take a screenshot of the desktop (all monitors) right now and look at it to answer a
    question — e.g. what's on screen, reading text in a window, checking what app is open.
    Always takes a fresh screenshot; never assume what's currently displayed. Always asks the
    user to confirm first, since a screenshot can reveal whatever else is open on screen.

    Args:
        question: What to look for, describe, or answer about the captured screenshot.
    """
    if not _confirm("Take a screenshot of the screen right now?"):
        return "User declined to use screen capture."

    try:
        image_bytes = _grab_screen_png()
    except ImportError:
        return "Error: Pillow isn't installed — run 'pip install Pillow' to enable screen capture."
    except Exception as e:
        return f"Error capturing the screen: {e}"

    _SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_path = _SCREEN_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    snapshot_path.write_bytes(image_bytes)

    return _ask_vision(image_bytes, "image/png", question)


def describe_current_screen(question: str) -> str:
    """Screenshot the desktop and describe it, with no confirm prompt and nothing saved to disk.

    Not an LLM tool (no @beta_tool) — the model can never call this on its own. It exists only
    for app-level features (like the CLI/GUI "watch" mode) that already got one-time upfront
    consent for a whole session of repeated captures, rather than re-confirming every frame.
    """
    try:
        image_bytes = _grab_screen_png()
    except ImportError:
        return "Error: Pillow isn't installed — run 'pip install Pillow' to enable screen capture."
    except Exception as e:
        return f"Error capturing the screen: {e}"
    return _ask_vision(image_bytes, "image/png", question)


# ---------------------------------------------------------------------------
# Email (Gmail)
# ---------------------------------------------------------------------------

_IMAP_HOST = "imap.gmail.com"
_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 465
_ATTACHMENTS_DIR = ROOT_DIR / "data" / "email_attachments"


def _resolve_account(account: str):
    """Returns (address, app_password) for a named account, or (None, None) if unknown/unconfigured."""
    acct = EMAIL_ACCOUNTS.get(account.lower())
    if not acct:
        return None, None
    return acct.get("address"), acct.get("app_password")


def _decode_words(value: str) -> str:
    """Decode an RFC 2047-encoded header value (subject, attachment filename, etc.) to plain text."""
    from email.header import decode_header
    decoded = ""
    for part, encoding in decode_header(value or ""):
        decoded += part.decode(encoding or "utf-8", errors="replace") if isinstance(part, bytes) else part
    return decoded


def _get_body_snippet(msg, max_chars: int = 400) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if content_type == "text/plain" and "attachment" not in disposition:
                try:
                    body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace")
                    return body.strip()[:max_chars]
                except Exception:
                    continue
        return ""
    try:
        body = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", errors="replace")
        return body.strip()[:max_chars]
    except Exception:
        return ""


def _list_attachment_names(msg) -> list:
    names = []
    if msg.is_multipart():
        for part in msg.walk():
            disposition = str(part.get("Content-Disposition", ""))
            filename = part.get_filename()
            if filename and "attachment" in disposition:
                names.append(_decode_words(filename))
    return names


def _format_message(msg) -> str:
    subject = _decode_words(msg.get("Subject", "")) or "(no subject)"
    from_ = msg.get("From", "(unknown sender)")
    date_ = msg.get("Date", "")
    attachments = _list_attachment_names(msg)
    snippet = _get_body_snippet(msg)

    entry = f"From: {from_}\nSubject: {subject}\nDate: {date_}"
    if attachments:
        entry += f"\nAttachments: {', '.join(attachments)}"
    if snippet:
        entry += f"\n{snippet}"
    return entry


def _fetch_messages(conn, ids: list, count: int) -> list:
    """Fetch and format up to `count` messages (most recent first) for the given IMAP ids."""
    import email
    ids = ids[-count:]
    ids.reverse()
    entries = []
    for eid in ids:
        status, msg_data = conn.fetch(eid, "(RFC822)")
        if status != "OK" or not msg_data or not msg_data[0]:
            continue
        entries.append(_format_message(email.message_from_bytes(msg_data[0][1])))
    return entries


@beta_tool
def read_emails(account: str = "personal", count: int = 10, unread_only: bool = False) -> str:
    """Read recent emails from one of the user's Gmail inboxes — sender, subject, date, any
    attachment filenames, and a short preview of each. Read-only; cannot mark emails as read or
    delete them. Use search_emails instead to find specific emails by sender, subject, etc.

    Args:
        account: Which Gmail account to read — "personal" or "business".
        count: How many of the most recent emails to fetch.
        unread_only: If True, only fetch unread emails.
    """
    address, app_password = _resolve_account(account)
    if not address or not app_password:
        return f"Error: the '{account}' email account isn't configured in .env. Known accounts: {', '.join(EMAIL_ACCOUNTS)}."

    import imaplib

    try:
        conn = imaplib.IMAP4_SSL(_IMAP_HOST)
        conn.login(address, app_password)
        conn.select("INBOX")

        status, data = conn.search(None, "UNSEEN" if unread_only else "ALL")
        if status != "OK":
            return "Error: could not search the inbox."

        entries = _fetch_messages(conn, data[0].split(), count)
        conn.logout()

        if not entries:
            return "No unread emails." if unread_only else "No emails found."
        return "\n\n".join(entries)
    except imaplib.IMAP4.error as e:
        return f"Error connecting to Gmail: {e}"
    except Exception as e:
        return f"Error reading emails: {e}"


@beta_tool
def search_emails(query: str, account: str = "personal", count: int = 10) -> str:
    """Search one of the user's Gmail inboxes using Gmail's own search syntax — the same as
    typing into Gmail's search box, e.g. "from:jane subject:invoice", "after:2026/08/01",
    "has:attachment". Read-only.

    Args:
        query: A Gmail search query.
        account: Which Gmail account to search — "personal" or "business".
        count: Maximum number of matching emails to return, most recent first.
    """
    address, app_password = _resolve_account(account)
    if not address or not app_password:
        return f"Error: the '{account}' email account isn't configured in .env. Known accounts: {', '.join(EMAIL_ACCOUNTS)}."

    import imaplib

    try:
        conn = imaplib.IMAP4_SSL(_IMAP_HOST)
        conn.login(address, app_password)
        conn.select("INBOX")

        escaped = query.replace('"', '\\"')
        status, data = conn.search(None, "X-GM-RAW", f'"{escaped}"')
        if status != "OK":
            return f"Error: search failed for '{query}'."

        entries = _fetch_messages(conn, data[0].split(), count)
        conn.logout()

        if not entries:
            return f"No emails matching '{query}'."
        return "\n\n".join(entries)
    except imaplib.IMAP4.error as e:
        return f"Error connecting to Gmail: {e}"
    except Exception as e:
        return f"Error searching emails: {e}"


@beta_tool
def download_email_attachments(query: str, account: str = "personal", max_emails: int = 5) -> str:
    """Find emails matching a Gmail search query and download any attachments from the most
    recent matches, saving them locally so they can be read or examined further (e.g. with
    read_file, or shown to look_at_camera-style vision analysis for images). Always asks the
    user to confirm, listing exactly what will be saved, before writing anything to disk.

    Args:
        query: A Gmail search query to locate the email(s) with the attachment(s) you want,
            e.g. "subject:invoice has:attachment" or "from:jane has:attachment".
        account: Which Gmail account to search — "personal" or "business".
        max_emails: Maximum number of matching emails to check for attachments.
    """
    address, app_password = _resolve_account(account)
    if not address or not app_password:
        return f"Error: the '{account}' email account isn't configured in .env. Known accounts: {', '.join(EMAIL_ACCOUNTS)}."

    import email
    import imaplib

    try:
        conn = imaplib.IMAP4_SSL(_IMAP_HOST)
        conn.login(address, app_password)
        conn.select("INBOX")

        escaped = query.replace('"', '\\"')
        status, data = conn.search(None, "X-GM-RAW", f'"{escaped}"')
        if status != "OK":
            return f"Error: search failed for '{query}'."

        ids = data[0].split()[-max_emails:]
        ids.reverse()

        found = []  # (filename, payload_bytes)
        for eid in ids:
            status, msg_data = conn.fetch(eid, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            if not msg.is_multipart():
                continue
            for part in msg.walk():
                disposition = str(part.get("Content-Disposition", ""))
                filename = part.get_filename()
                if not filename or "attachment" not in disposition:
                    continue
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                found.append((_decode_words(filename), payload))
        conn.logout()

        if not found:
            return f"No attachments found in emails matching '{query}'."

        listing = "\n".join(f"  {name} ({len(payload)} bytes)" for name, payload in found)
        if not _confirm(f"Download {len(found)} attachment(s) to {_ATTACHMENTS_DIR}?\n{listing}"):
            return "User declined the download."

        _ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        saved = []
        for name, payload in found:
            safe_name = Path(name).name or "attachment"
            dest = _ATTACHMENTS_DIR / f"{stamp}_{safe_name}"
            dest.write_bytes(payload)
            saved.append(str(dest))
        return "Saved:\n" + "\n".join(saved)
    except imaplib.IMAP4.error as e:
        return f"Error connecting to Gmail: {e}"
    except Exception as e:
        return f"Error downloading attachments: {e}"


@beta_tool
def send_email(to: str, subject: str, body: str, account: str = "personal", attachment_paths: str = "") -> str:
    """Send an email from one of the user's Gmail accounts, optionally with one or more files
    attached. Always asks the user to confirm — showing exactly who it's going to, from which
    account, what it says, and any attachments — before sending.

    Args:
        to: Recipient email address.
        subject: Email subject line.
        body: The email's plain-text body.
        account: Which Gmail account to send from — "personal" or "business".
        attachment_paths: Optional local file path(s) to attach — relative to Jarvis's own
            folder, or absolute (e.g. a resume and cover letter saved earlier). Comma-separate
            multiple paths, e.g. "resume.docx,cover_letter.docx".
    """
    address, app_password = _resolve_account(account)
    if not address or not app_password:
        return f"Error: the '{account}' email account isn't configured in .env. Known accounts: {', '.join(EMAIL_ACCOUNTS)}."

    attach_files = []
    for raw_path in attachment_paths.split(","):
        raw_path = raw_path.strip()
        if not raw_path:
            continue
        attach_file = _resolve_path(raw_path)
        if not attach_file.exists() or not attach_file.is_file():
            return f"Error: attachment file not found: {attach_file}"
        attach_files.append(attach_file)

    confirm_msg = f"Send this email from {address}?\n    To: {to}\n    Subject: {subject}"
    for attach_file in attach_files:
        confirm_msg += f"\n    Attachment: {attach_file.name} ({attach_file.stat().st_size} bytes)"
    confirm_msg += f"\n\n{body}"
    if not _confirm(confirm_msg):
        return "User declined to send the email."

    import smtplib
    from email import encoders
    from email.mime.base import MIMEBase
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    try:
        if attach_files:
            msg = MIMEMultipart()
            msg.attach(MIMEText(body))
            for attach_file in attach_files:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(attach_file.read_bytes())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", f'attachment; filename="{attach_file.name}"')
                msg.attach(part)
        else:
            msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = address
        msg["To"] = to

        with smtplib.SMTP_SSL(_SMTP_HOST, _SMTP_PORT) as server:
            server.login(address, app_password)
            server.send_message(msg)
        suffix = f" (with attachments: {', '.join(f.name for f in attach_files)})" if attach_files else ""
        return f"Email sent to {to} from {address}.{suffix}"
    except smtplib.SMTPException as e:
        return f"Error sending email: {e}"
    except Exception as e:
        return f"Error sending email: {e}"


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

@beta_tool
def create_word_document(filename: str, title: str, content: str) -> str:
    """Create a Word (.docx) document — e.g. for printing or emailing. Always asks the user to
    confirm first. Content supports light formatting: lines starting with "- " become bullet
    points, lines starting with "1. " (any number) become a numbered list, and everything else
    becomes a plain paragraph.

    Args:
        filename: Name for the file, e.g. "recipe.docx" (".docx" is added automatically if missing).
        title: A heading shown at the top of the document.
        content: The document body.
    """
    try:
        import docx
    except ImportError:
        return "Error: python-docx isn't installed — run 'pip install python-docx' to enable document creation."

    if not filename.lower().endswith(".docx"):
        filename += ".docx"
    path = _resolve_path(filename)

    if not _confirm(f"Create Word document '{path}'?"):
        return "User declined."

    doc = docx.Document()
    doc.add_heading(title, level=1)

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            doc.add_paragraph(stripped[2:], style="List Bullet")
        elif re.match(r"^\d+\.\s", stripped):
            doc.add_paragraph(re.sub(r"^\d+\.\s", "", stripped), style="List Number")
        else:
            doc.add_paragraph(stripped)

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return f"Saved Word document: {path}"


ALL_TOOLS = [
    system_info,
    list_directory,
    read_file,
    write_file,
    run_command,
    open_application,
    media_control,
    play_music,
    quit_music,
    set_volume,
    get_volume,
    list_trading_files,
    read_strategy_state,
    read_strategy_log,
    read_trade_journal,
    get_market_price,
    add_assignment,
    list_assignments,
    update_assignment,
    delete_assignment,
    upcoming_workload,
    remember,
    recall,
    forget,
    look_at_camera,
    look_at_screen,
    read_emails,
    search_emails,
    download_email_attachments,
    send_email,
    create_word_document,
]
