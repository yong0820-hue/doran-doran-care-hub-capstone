"""Optional offline Korean wake-phrase detection for the Raspberry Pi hub.

Wake audio is kept in memory, decoded locally with Vosk, and never persisted.
The detector deliberately uses bounded ``arecord`` chunks so it can share the
existing Jabra audio lock with reminders and AI conversations.
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import time
import unicodedata


SAMPLE_RATE = 16_000
CAPTURE_SECONDS = 1
AUDIO_YIELD_SECONDS = 0.1
POST_TURN_COOLDOWN_SECONDS = 1.0
MAX_RECOGNIZER_GAP_SECONDS = 2.5
_TRUE_VALUES = {"1", "true", "yes", "on"}


def _normalized_tokens(text):
    if not isinstance(text, str):
        return ()
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return tuple(re.findall(r"[^\W_]+", normalized, flags=re.UNICODE))


def matches_wake_phrase(text, phrases):
    """Match a configured phrase on token boundaries without fuzzy matching."""
    tokens = _normalized_tokens(text)
    if not tokens:
        return False

    for phrase in phrases:
        target = "".join(_normalized_tokens(phrase))
        if not target:
            continue
        for start in range(len(tokens)):
            candidate = ""
            for token in tokens[start:]:
                candidate += token
                if candidate == target:
                    return True
                if len(candidate) >= len(target):
                    break
    return False


def _parse_bool(value):
    return str(value).strip().lower() in _TRUE_VALUES


@dataclass(frozen=True)
class WakeWordSettings:
    model_path: Path
    phrases: tuple
    audio_device: str
    log_transcripts: bool = False
    sample_rate: int = SAMPLE_RATE
    capture_seconds: int = CAPTURE_SECONDS
    audio_yield_seconds: float = AUDIO_YIELD_SECONDS
    cooldown_seconds: float = POST_TURN_COOLDOWN_SECONDS

    @classmethod
    def from_env(cls, audio_device):
        raw_path = os.getenv(
            "AI_WAKE_WORD_MODEL_PATH",
            "models/vosk-model-small-ko-0.22",
        ).strip()
        if not raw_path:
            raise ValueError("AI_WAKE_WORD_MODEL_PATH is required")
        model_path = Path(raw_path).expanduser()
        if not model_path.is_absolute():
            model_path = Path.cwd() / model_path
        model_path = model_path.resolve()
        if not model_path.is_dir():
            raise FileNotFoundError("Configured wake-word model directory is missing")

        raw_phrases = os.getenv("AI_WAKE_WORD_PHRASES", "도란아")
        phrases = tuple(item.strip() for item in raw_phrases.split(",") if item.strip())
        if not phrases or len(phrases) > 10:
            raise ValueError("AI_WAKE_WORD_PHRASES must contain 1 to 10 phrases")
        for phrase in phrases:
            normalized = "".join(_normalized_tokens(phrase))
            if not 2 <= len(normalized) <= 20:
                raise ValueError("Each wake phrase must contain 2 to 20 characters")

        if not isinstance(audio_device, str) or not audio_device.strip():
            raise ValueError("AI_AUDIO_DEVICE is required for wake-word capture")

        return cls(
            model_path=model_path,
            phrases=phrases,
            audio_device=audio_device.strip(),
            log_transcripts=_parse_bool(
                os.getenv("AI_WAKE_WORD_LOG_TRANSCRIPTS", "false")
            ),
        )


class _WakeWordStopped(Exception):
    pass


class LocalWakeWordDetector:
    """Capture one bounded PCM chunk and inspect local Vosk output."""

    def __init__(
        self,
        recognizer,
        settings,
        audio_lock,
        stop_event,
        popen_factory=subprocess.Popen,
        monotonic=time.monotonic,
    ):
        self.recognizer = recognizer
        self.settings = settings
        self.audio_lock = audio_lock
        self.stop_event = stop_event
        self.popen_factory = popen_factory
        self.monotonic = monotonic
        self.audio_yield_seconds = settings.audio_yield_seconds
        self.cooldown_seconds = settings.cooldown_seconds
        self._last_pcm_at = None

    def reset(self):
        """Drop partial speech so audio separated by another task is not joined."""
        self._last_pcm_at = None
        reset = getattr(self.recognizer, "Reset", None)
        if reset is not None:
            reset()

    @staticmethod
    def _terminate(process):
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)

    def _capture_pcm(self):
        while not self.audio_lock.acquire(timeout=0.1):
            if self.stop_event.is_set():
                raise _WakeWordStopped()

        process = None
        try:
            if self.stop_event.is_set():
                raise _WakeWordStopped()
            command = [
                "arecord",
                "-q",
                "-D",
                self.settings.audio_device,
                "-d",
                str(self.settings.capture_seconds),
                "-t",
                "raw",
                "-f",
                "S16_LE",
                "-r",
                str(self.settings.sample_rate),
                "-c",
                "1",
            ]
            process = self.popen_factory(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            timeout = self.settings.capture_seconds + 3
            deadline = self.monotonic() + timeout
            while True:
                if self.stop_event.is_set():
                    raise _WakeWordStopped()
                remaining = deadline - self.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    pcm, _stderr = process.communicate(timeout=min(0.1, remaining))
                except subprocess.TimeoutExpired:
                    continue
                if process.returncode:
                    raise subprocess.CalledProcessError(process.returncode, command)
                return pcm
        finally:
            try:
                if process is not None:
                    self._terminate(process)
            finally:
                self.audio_lock.release()

    @staticmethod
    def _text_from_result(payload, key):
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            return ""
        text = data.get(key, "") if isinstance(data, dict) else ""
        return text if isinstance(text, str) else ""

    def listen_once(self):
        try:
            pcm = self._capture_pcm()
        except _WakeWordStopped:
            return False
        if not pcm or self.stop_event.is_set():
            return False

        captured_at = self.monotonic()
        if (
            self._last_pcm_at is not None
            and captured_at - self._last_pcm_at > MAX_RECOGNIZER_GAP_SECONDS
        ):
            self.reset()
        self._last_pcm_at = captured_at
        is_final = bool(self.recognizer.AcceptWaveform(pcm))
        if is_final:
            text = self._text_from_result(self.recognizer.Result(), "text")
        else:
            text = self._text_from_result(self.recognizer.PartialResult(), "partial")

        if self.settings.log_transcripts and text:
            print(f"[WakeWord Debug] local transcript: {text}")

        # A partial Vosk hypothesis can change on the next audio frame. Requiring
        # an endpointed result avoids opening the cloud-backed turn on a transient
        # ambient-speech guess.
        if not is_final or not matches_wake_phrase(text, self.settings.phrases):
            return False

        self.reset()
        print("[WakeWord] 설정된 로컬 호출어를 감지했습니다.")
        return True


def create_wake_word_detector(audio_lock, stop_event, audio_device):
    """Build the optional detector without downloading a model at runtime."""
    settings = WakeWordSettings.from_env(audio_device)
    from vosk import KaldiRecognizer, Model, SetLogLevel

    SetLogLevel(-1)
    model = Model(str(settings.model_path))
    recognizer = KaldiRecognizer(model, settings.sample_rate)
    detector = LocalWakeWordDetector(
        recognizer,
        settings,
        audio_lock,
        stop_event,
    )
    # Keep an explicit reference for bindings that do not retain the model.
    detector.model = model
    return detector
