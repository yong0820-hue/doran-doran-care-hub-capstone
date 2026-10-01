"""Thread-safe voice state and bounded coordination with browser radio playback."""

from contextlib import contextmanager
import threading
import time


class DisplayState:
    """Scope state to actual work, restoring other workers after every exit.

    Microphone capture and playback outrank network work. If physical phases
    overlap, the most recently entered one wins. Inactivity care has display
    priority over medication; the other card returns when its scope remains.
    """

    _PHASE_PRIORITY = {"thinking": 1, "listening": 2, "speaking": 2}
    RADIO_PAUSE_TIMEOUT_SECONDS = 0.75
    _ACTIVITIES = {"conversation", "inactivity", "medication", "game"}
    _ACTIVITY_PRIORITY = ("inactivity", "medication", "conversation", "game")
    _ALERTS = {
        "inactivity": {
            "kind": "inactivity",
            "title": "안부를 확인하고 있어요",
            "message": "한동안 움직임이 없으신데 괜찮으세요?",
        },
        "medication": {
            "kind": "medication",
            "title": "약 드실 시간이에요",
            "message": "복약 안내를 확인해 주세요.",
        },
    }

    def __init__(self):
        self._lock = threading.RLock()
        self._activities = {}
        self._phases = {}
        self._sequence = 0
        self._radio_condition = threading.Condition(self._lock)
        self._radio_playing = False
        self._focus_revision = 0
        self._non_game_revision = 0

    def request_radio(self, playing):
        """Reserve browser playback while idle, or acknowledge a completed pause.

        The same lock makes a play request atomic with a voice scope starting.
        This is browser intent, never a claim about a physical audio device.
        """
        if type(playing) is not bool:
            raise ValueError("Radio playing must be boolean")
        with self._radio_condition:
            if playing and (self._activities or self._phases):
                return False
            self._radio_playing = playing
            if not playing:
                self._radio_condition.notify_all()
            return True

    def _wait_for_radio_pause(self):
        # Publish busy before waiting so the existing state poll can pause the
        # browser and acknowledge it. No browser intent means no extra delay.
        # A crashed/disconnected kiosk must never prevent voice care indefinitely.
        deadline = time.monotonic() + self.RADIO_PAUSE_TIMEOUT_SECONDS
        with self._radio_condition:
            while self._radio_playing:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._radio_playing = False
                    self._radio_condition.notify_all()
                    break
                self._radio_condition.wait(remaining)

    @contextmanager
    def activity(self, kind, *, message=None):
        if kind not in self._ACTIVITIES:
            raise ValueError("Unknown display activity")
        if message is not None and not isinstance(message, str):
            raise ValueError("Display message must be text")
        token = object()
        with self._lock:
            if not (self._activities or self._phases):
                self._focus_revision += 1
            if kind != "game":
                # Game narration may ignore its own focus transitions, but it
                # must never miss a short care/conversation scope between polls.
                self._non_game_revision += 1
            self._activities[token] = (kind, message)
        try:
            self._wait_for_radio_pause()
            yield
        finally:
            with self._lock:
                self._activities.pop(token, None)
                if not (self._activities or self._phases):
                    self._focus_revision += 1

    @contextmanager
    def phase(self, state):
        if state not in self._PHASE_PRIORITY:
            raise ValueError("Unknown display phase")
        token = object()
        with self._lock:
            if not (self._activities or self._phases):
                self._focus_revision += 1
            self._sequence += 1
            self._phases[token] = (state, self._sequence)
        try:
            self._wait_for_radio_pause()
            yield
        finally:
            with self._lock:
                self._phases.pop(token, None)
                if not (self._activities or self._phases):
                    self._focus_revision += 1

    def snapshot(self):
        """Return a detached, JSON-safe snapshot without speech or secrets."""
        with self._lock:
            state = "idle"
            if self._phases:
                state = max(
                    self._phases.values(),
                    key=lambda item: (self._PHASE_PRIORITY[item[0]], item[1]),
                )[0]
            activities = {kind for kind, _message in self._activities.values()}
            activity = next(
                (kind for kind in self._ACTIVITY_PRIORITY if kind in activities),
                None,
            )
            alert = next(
                (dict(self._ALERTS[kind])
                 for kind in ("inactivity", "medication") if kind in activities),
                None,
            )
            if alert is not None:
                # Most recent scope of the selected card supplies its hint.
                for kind, message in reversed(list(self._activities.values())):
                    if kind == alert["kind"]:
                        if message is not None:
                            alert["message"] = message
                        break
            return {
                "state": state,
                "busy": bool(self._activities or self._phases),
                "activity": activity,
                "alert": alert,
                "audio_focus": {
                    "blocked": bool(self._activities or self._phases),
                    "revision": self._focus_revision,
                    "non_game_revision": self._non_game_revision,
                },
            }


DISPLAY = DisplayState()
