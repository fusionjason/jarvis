"""Speech in and out. Listening (mic capture + faster-whisper transcription), wake-word detection
(openWakeWord), and speaking's local fallback tier all run entirely on-device. Talking back tries
three tiers in order: ElevenLabs (most natural/expressive, needs ELEVENLABS_API_KEY) -> Microsoft
Edge's free neural voices (natural, no key needed) -> fully local Windows SAPI5 (via pyttsx3,
works with no internet at all). Each tier falls through to the next if it fails.

Two other network dependencies, both one-time: the first transcription downloads the Whisper
model weights from Hugging Face, and the first wake-word use downloads openWakeWord's "Hey
Jarvis" model from GitHub. Both are cached locally after that and work fully offline.
"""

import asyncio
import os
import re
import tempfile
import threading
import time
from typing import Optional

import numpy as np
import pyttsx3
import sounddevice as sd
from faster_whisper import WhisperModel

from config import ELEVENLABS_API_KEY

SAMPLE_RATE = 16000

_CHUNK_SECONDS = 0.25
_SILENCE_RMS = 0.010            # below this = "quiet"; raise if it stops too early in a noisy room
_SILENCE_HANG_SECONDS = 1.2     # how much continuous quiet ends the recording, once speech has started
_MAX_SECONDS = 15               # hard cap so a stuck mic can never hang forever

# Barge-in: how many consecutive loud chunks (at the same _SILENCE_RMS threshold used for normal
# listening) are required before we treat it as the user actually talking over a reply, rather
# than a single noisy mic pop — see the mic-quality troubleshooting this was built alongside.
_BARGE_IN_CONSECUTIVE_CHUNKS = 2

# openWakeWord expects 16kHz mono int16 audio in fixed 1280-sample (80ms) frames — a different
# format from the float32 used for transcription above, so wake listening uses its own stream.
_WAKE_MODEL_NAME = "hey_jarvis"
_WAKE_CHUNK_SAMPLES = 1280
_WAKE_THRESHOLD = 0.5  # openWakeWord's own recommended starting point; tune after real-world testing

# This mic's input level swings wildly between recordings (observed anywhere from near-silent to
# clipping at full-scale on the same setup, seconds apart) — openWakeWord is trained on
# consistently-leveled audio, so raw input made it miss real speech either way. These normalize
# each chunk toward a target peak using a slowly-adapting running estimate (not a per-chunk gain,
# which would cause jarring jumps between 80ms frames).
_WAKE_TARGET_PEAK = 12000.0   # target peak amplitude, out of a possible 32767
_WAKE_MIN_PEAK = 300.0        # below this, treat the chunk as silence/noise — don't amplify it
_WAKE_GAIN_SMOOTHING = 0.3    # 0-1; how fast the running peak estimate adapts to level changes
_WAKE_MAX_GAIN = 8.0          # cap so a quiet->loud transition can't spike/clip the normalized signal

# ElevenLabs (primary, if ELEVENLABS_API_KEY is set) — by far the most natural/expressive tier.
# "Daniel" is a formal British broadcaster voice; eleven_v3 is the model that understands inline
# delivery tags like "[sarcastic] ..." in the text for expressive readings. Browse other voices
# via client.voices.get_all() — see the chat history for how this one was picked.
_ELEVENLABS_VOICE_ID = "onwK4e9ZLuTAKqWW03F9"  # Daniel — Steady Broadcaster
_ELEVENLABS_MODEL = "eleven_v3"

# Edge TTS neural voice (second tier) — natural-sounding, free, no API key. Used only if
# ElevenLabs isn't configured or its call fails. List alternatives with `edge-tts --list-voices`.
_EDGE_VOICE = "en-GB-RyanNeural"
_EDGE_RATE = "+0%"  # e.g. "+15%" faster, "-10%" slower

# Local SAPI5 fallback (via pyttsx3, third tier) — used only if both cloud tiers fail. Substring-
# matched against installed Windows voice names (case-insensitive) — "david" or "zira" are the
# two built into Windows.
_TTS_VOICE_HINT = "david"
_TTS_RATE = 175  # words per minute; Windows' own default is ~200

_model = None


def _get_model() -> WhisperModel:
    global _model
    if _model is None:
        # "base" on CPU with int8: no GPU required, first call downloads ~145MB once and caches it.
        # Swap to "small"/"medium" for better accuracy at the cost of a bigger download and more CPU time.
        _model = WhisperModel("base", device="cpu", compute_type="int8")
    return _model


def record_until_silence(max_seconds: float = _MAX_SECONDS) -> np.ndarray:
    """Record from the default microphone until the speaker goes quiet, or max_seconds elapses.
    Returns a mono float32 array at SAMPLE_RATE; empty if nothing was captured.
    """
    chunk_frames = int(_CHUNK_SECONDS * SAMPLE_RATE)
    hang_chunks = max(1, int(_SILENCE_HANG_SECONDS / _CHUNK_SECONDS))
    max_chunks = int(max_seconds / _CHUNK_SECONDS)

    chunks: list[np.ndarray] = []
    quiet_streak = 0
    heard_speech = False

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32") as stream:
        for _ in range(max_chunks):
            block, _overflowed = stream.read(chunk_frames)
            block = block[:, 0]
            chunks.append(block)
            rms = float(np.sqrt(np.mean(np.square(block))))
            if rms >= _SILENCE_RMS:
                heard_speech = True
                quiet_streak = 0
            else:
                quiet_streak += 1
            if heard_speech and quiet_streak >= hang_chunks:
                break

    # If real speech was never detected, treat it the same as "nothing captured" — Whisper tends
    # to hallucinate plausible-sounding text from pure background noise/silence otherwise.
    if not heard_speech or not chunks:
        return np.zeros(0, dtype="float32")
    return np.concatenate(chunks)


def _listen_for_barge_in(
    interrupt_flag: threading.Event, playback_done: threading.Event, max_seconds: float = _MAX_SECONDS
) -> np.ndarray:
    """Runs on a background thread for as long as Jarvis is speaking. Watches the mic the whole
    time; the moment it's sure someone's actually talking (not just a stray pop), it sets
    interrupt_flag so the caller can cut playback short — then keeps recording that same audio
    (nothing said gets clipped, since chunks were being buffered from the start) until they go
    quiet, exactly like record_until_silence.

    Returns the captured audio once real speech both started and trailed off. Returns empty if
    playback_done gets set first (the reply finished normally, with no interruption).
    """
    chunk_frames = int(_CHUNK_SECONDS * SAMPLE_RATE)
    hang_chunks = max(1, int(_SILENCE_HANG_SECONDS / _CHUNK_SECONDS))
    max_chunks = int(max_seconds / _CHUNK_SECONDS)

    chunks: list[np.ndarray] = []
    quiet_streak = 0
    loud_streak = 0
    heard_speech = False

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32") as stream:
        for _ in range(max_chunks):
            if playback_done.is_set() and not heard_speech:
                return np.zeros(0, dtype="float32")
            block, _overflowed = stream.read(chunk_frames)
            block = block[:, 0]
            chunks.append(block)
            rms = float(np.sqrt(np.mean(np.square(block))))
            if rms >= _SILENCE_RMS:
                loud_streak += 1
                quiet_streak = 0
                if not heard_speech and loud_streak >= _BARGE_IN_CONSECUTIVE_CHUNKS:
                    heard_speech = True
                    interrupt_flag.set()
            else:
                loud_streak = 0
                quiet_streak += 1
            if heard_speech and quiet_streak >= hang_chunks:
                break

    if not heard_speech:
        return np.zeros(0, dtype="float32")
    return np.concatenate(chunks)


def transcribe(audio: np.ndarray) -> str:
    """Transcribe a mono float32 16kHz audio array to text."""
    if audio.size == 0:
        return ""
    segments, _info = _get_model().transcribe(audio, language="en")
    return " ".join(segment.text.strip() for segment in segments).strip()


def listen_and_transcribe(max_seconds: float = _MAX_SECONDS) -> str:
    """Record from the mic until silence (or max_seconds) and return the transcribed text.
    Returns an empty string if nothing but silence was captured.
    """
    audio = record_until_silence(max_seconds)
    return transcribe(audio)


_wake_model = None


def _get_wake_model():
    global _wake_model
    if _wake_model is None:
        from openwakeword.model import Model
        _wake_model = Model(wakeword_models=[_WAKE_MODEL_NAME], inference_framework="onnx")
    return _wake_model


def wait_for_wake_word(stop_check=None) -> bool:
    """Block, continuously listening on the default microphone, until the wake word ("Hey
    Jarvis") is heard. Returns True once detected.

    stop_check, if given, is a zero-arg callable polled between audio chunks (roughly every
    80ms); if it returns True, listening stops early and this returns False. Lets a caller (e.g.
    a GUI toggle) cancel an in-progress wait without killing the whole process.
    """
    model = _get_wake_model()
    model.reset()  # clear prediction state left over from any previous listen

    running_peak = _WAKE_TARGET_PEAK  # start assuming a "normal" level; adapts from there

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16") as stream:
        while True:
            if stop_check is not None and stop_check():
                return False
            block, _overflowed = stream.read(_WAKE_CHUNK_SAMPLES)
            audio = block[:, 0]

            chunk_peak = float(np.max(np.abs(audio)))
            if chunk_peak > _WAKE_MIN_PEAK:
                running_peak = (1 - _WAKE_GAIN_SMOOTHING) * running_peak + _WAKE_GAIN_SMOOTHING * chunk_peak
                gain = min(_WAKE_TARGET_PEAK / max(running_peak, _WAKE_MIN_PEAK), _WAKE_MAX_GAIN)
                audio = np.clip(audio.astype(np.float64) * gain, -32768, 32767).astype(np.int16)

            scores = model.predict(audio)
            if scores[_WAKE_MODEL_NAME] >= _WAKE_THRESHOLD:
                return True


_MD_BULLET = re.compile(r"^[ \t]*[-*+][ \t]+", re.MULTILINE)
_MD_HR = re.compile(r"^[ \t]*[-*_]{3,}[ \t]*$", re.MULTILINE)
_MD_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_MD_ITALIC = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_MD_CODE = re.compile(r"`([^`]+)`")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_HEADER = re.compile(r"^#{1,6}[ \t]+", re.MULTILINE)
_MD_BLANK_RUN = re.compile(r"\n{3,}")


def _clean_for_speech(text: str) -> str:
    """Strips markdown formatting Claude's replies come back in (**bold**, bullets, headers,
    links, code spans) so TTS speaks natural words instead of literal asterisks and hashes.
    """
    text = _MD_BULLET.sub("", text)
    text = _MD_HR.sub("", text)
    text = _MD_BOLD.sub(r"\1", text)
    text = _MD_ITALIC.sub(r"\1", text)
    text = _MD_CODE.sub(r"\1", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_HEADER.sub("", text)
    text = _MD_BLANK_RUN.sub("\n\n", text)
    return text.strip()


def _select_voice(engine: "pyttsx3.Engine", hint: str) -> None:
    if not hint:
        return
    for v in engine.getProperty("voices"):
        if hint.lower() in v.name.lower():
            engine.setProperty("voice", v.id)
            return


def speak(text: str) -> None:
    """Speak text aloud. Tries ElevenLabs first (if configured), then Edge TTS, then the fully
    local Windows voice as a last resort. Blocks until finished.
    """
    text = _clean_for_speech(text)
    if not text:
        return
    if ELEVENLABS_API_KEY:
        try:
            _speak_elevenlabs(text)
            return
        except Exception:
            pass  # fall through to the next tier
    try:
        _speak_edge(text)
    except Exception:
        speak_local(text)


def _synthesize_elevenlabs(text: str) -> str:
    """Renders text to a temp mp3 via ElevenLabs and returns its path — caller owns cleanup."""
    from elevenlabs.client import ElevenLabs

    client = ElevenLabs(api_key=ELEVENLABS_API_KEY)
    audio = client.text_to_speech.convert(
        voice_id=_ELEVENLABS_VOICE_ID,
        model_id=_ELEVENLABS_MODEL,
        text=text,
    )
    audio_bytes = b"".join(audio)

    fd, path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    with open(path, "wb") as f:
        f.write(audio_bytes)
    return path


def _synthesize_edge(text: str) -> str:
    """Renders text to a temp mp3 via Edge TTS and returns its path — caller owns cleanup."""
    import edge_tts

    async def _synthesize(path: str) -> None:
        communicate = edge_tts.Communicate(text, _EDGE_VOICE, rate=_EDGE_RATE)
        await communicate.save(path)

    fd, path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    asyncio.run(_synthesize(path))
    return path


def _speak_elevenlabs(text: str) -> None:
    from playsound import playsound

    path = _synthesize_elevenlabs(text)
    try:
        playsound(path)
    finally:
        os.remove(path)


def _speak_edge(text: str) -> None:
    from playsound import playsound

    path = _synthesize_edge(text)
    try:
        playsound(path)
    finally:
        os.remove(path)


def speak_local(text: str) -> None:
    """Speak text aloud using the local Windows TTS voice (SAPI5, via pyttsx3). Fully offline.
    Blocks until finished speaking.

    Builds a fresh engine per call rather than reusing one — pyttsx3's SAPI5 driver is a known
    source of hangs on a second runAndWait() from a reused engine instance, and a new engine is
    cheap enough on SAPI5 that it isn't worth the risk.
    """
    text = text.strip()
    if not text:
        return
    engine = pyttsx3.init()
    _select_voice(engine, _TTS_VOICE_HINT)
    engine.setProperty("rate", _TTS_RATE)
    engine.say(text)
    engine.runAndWait()
    engine.stop()


def _play_file_stoppable(path: str, interrupt_flag: threading.Event) -> None:
    """Plays an audio file, polling interrupt_flag a few times a second and cutting playback
    short the instant it's set. Needs pygame's mixer rather than playsound — playsound has no
    way to stop a file once started.
    """
    import pygame

    pygame.mixer.init()
    try:
        pygame.mixer.music.load(path)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            if interrupt_flag.is_set():
                pygame.mixer.music.stop()
                break
            time.sleep(0.05)
    finally:
        pygame.mixer.music.unload()
        pygame.mixer.quit()


def _speak_local_stoppable(text: str, interrupt_flag: threading.Event) -> None:
    engine = pyttsx3.init()
    _select_voice(engine, _TTS_VOICE_HINT)
    engine.setProperty("rate", _TTS_RATE)
    engine.say(text)

    # pyttsx3 has no built-in polling hook, but engine.stop() is safe to call from another
    # thread while runAndWait() is blocking — this watcher does exactly that the moment someone
    # talks over the reply. done_flag ensures it exits promptly either way, not just on interrupt.
    done_flag = threading.Event()

    def _watch() -> None:
        while not done_flag.is_set():
            if interrupt_flag.is_set():
                engine.stop()
                return
            time.sleep(0.05)

    watcher = threading.Thread(target=_watch, daemon=True)
    watcher.start()
    try:
        engine.runAndWait()
    finally:
        done_flag.set()
        engine.stop()


def _speak_stoppable(text: str, interrupt_flag: threading.Event) -> None:
    if ELEVENLABS_API_KEY:
        try:
            path = _synthesize_elevenlabs(text)
            try:
                _play_file_stoppable(path, interrupt_flag)
                return
            finally:
                os.remove(path)
        except Exception:
            pass  # fall through to the next tier
    try:
        path = _synthesize_edge(text)
        try:
            _play_file_stoppable(path, interrupt_flag)
        finally:
            os.remove(path)
    except Exception:
        _speak_local_stoppable(text, interrupt_flag)


def speak_with_barge_in(text: str, max_interrupt_seconds: float = _MAX_SECONDS) -> Optional[str]:
    """Speaks text aloud with the same 3-tier fallback as speak(), but keeps the mic live the
    whole time. If the user starts talking over it, playback is cut short immediately and
    whatever they said is captured and transcribed — lets a mishear get corrected, or a new
    request take over, without waiting for the reply to finish.

    Returns the transcribed interruption if the user talked over the reply, or None if it played
    out normally (or an attempted interruption didn't yield any usable speech).
    """
    text = _clean_for_speech(text)
    if not text:
        return None

    interrupt_flag = threading.Event()   # set the moment real speech is heard, tells playback to stop
    playback_done = threading.Event()    # set once playback ends, one way or another
    result: dict[str, np.ndarray] = {}

    def _listen() -> None:
        result["audio"] = _listen_for_barge_in(interrupt_flag, playback_done, max_interrupt_seconds)

    listener = threading.Thread(target=_listen, daemon=True)
    listener.start()
    try:
        _speak_stoppable(text, interrupt_flag)
    finally:
        playback_done.set()
        listener.join(timeout=max_interrupt_seconds + 1)

    audio = result.get("audio", np.zeros(0, dtype="float32"))
    if audio.size == 0:
        return None
    return transcribe(audio)
