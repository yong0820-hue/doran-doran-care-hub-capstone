"""At-most-once proactive care on existing Firebase inactivity events.

A conditional source-event claim precedes audio. A durable voiceCheck marker,
including an unfinished claim, is never reclaimed by a restarted process. A
crash can therefore skip a check, but cannot cause the same event to speak again.
"""

from copy import deepcopy
from dataclasses import dataclass
import math
from urllib.parse import quote
from uuid import uuid4


SERVER_TIMESTAMP = {".sv": "timestamp"}
_READ_FAILED = object()
_MISSING = object()
OUTCOMES = {"okay", "help", "unclear", "no_speech", "cancelled"}
HELP_TITLE = "장시간 미활동 · 음성 도움 요청"


@dataclass(frozen=True)
class PendingCheck:
    event_id: str
    claim_token: str
    identity: tuple
    outcome: str = ""
    error_type: str = ""


class FirebaseInactivityClient:
    """Consume one open source event or retry its completion per polling pass."""

    REQUEST_TIMEOUT = 10
    IDENTITY_FIELDS = ("type", "createdAt", "elderId", "deviceId")

    def __init__(self, base_url, elder_id, device_id, http=None, poll_seconds=5):
        self.base_url = base_url.rstrip("/")
        self.elder_id = elder_id
        self.device_id = device_id
        self.path = f"events/{elder_id}"
        self.poll_seconds = float(poll_seconds)
        if not self.base_url:
            raise ValueError("FIREBASE_DATABASE_URL is required for inactivity care")
        if not self._valid_key(elder_id) or not self._valid_key(device_id):
            raise ValueError("ELDER_ID and DEVICE_ID must be valid Firebase keys")
        if not math.isfinite(self.poll_seconds) or not 1 <= self.poll_seconds <= 60:
            raise ValueError("Inactivity poll_seconds must be between 1 and 60")
        if http is None:
            import requests

            http = requests
        self.http = http
        self._pending_claim = None
        self._pending_completion = None
        self._started_events = set()
        self._active_check = None

    @staticmethod
    def _valid_key(value):
        return (
            isinstance(value, str) and bool(value)
            and not any(char in value for char in ".#$[]/")
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)
        )

    @staticmethod
    def _valid_timestamp(value):
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    def _collection_url(self):
        return f"{self.base_url}/events/{quote(self.elder_id, safe='')}.json"

    def _event_url(self, event_id):
        return f"{self._collection_url()[:-5]}/{quote(event_id, safe='')}.json"

    @classmethod
    def _identity(cls, event):
        return tuple((key in event, event.get(key)) for key in cls.IDENTITY_FIELDS)

    @staticmethod
    def _marker(event):
        meta = event.get("meta", {})
        return meta.get("voiceCheck", _MISSING) if isinstance(meta, dict) else _MISSING

    def _eligible(self, event_id, event):
        if not self._valid_key(event_id) or not isinstance(event, dict):
            return False
        return (
            event.get("type") == "long_inactivity"
            and event.get("status", "open") == "open"
            and self._valid_timestamp(event.get("createdAt"))
            and ("elderId" not in event or event["elderId"] == self.elder_id)
            and isinstance(event.get("meta", {}), dict)
            and self._marker(event) is _MISSING
            and event_id not in self._started_events
        )

    def _read_event(self, event_id, *, with_etag):
        headers = {"X-Firebase-ETag": "true"} if with_etag else None
        try:
            response = self.http.get(
                self._event_url(event_id), headers=headers, timeout=self.REQUEST_TIMEOUT
            )
            if response.status_code != 200:
                return _READ_FAILED, None
            event = response.json()
            etag = response.headers.get("ETag") if with_etag else None
            if with_etag and not etag:
                print("[Inactivity] Source ETag unavailable; will retry without audio.")
                return _READ_FAILED, None
            return event, etag
        except Exception:
            print("[Inactivity] Source read pending; will retry without audio.")
            return _READ_FAILED, None

    def _find_next(self):
        try:
            response = self.http.get(self._collection_url(), timeout=self.REQUEST_TIMEOUT)
            if response.status_code != 200:
                print("[Inactivity] Event poll unavailable; will retry.")
                return None
            events = response.json()
        except Exception:
            print("[Inactivity] Event poll unavailable; will retry.")
            return None
        if events is None:
            return None
        if not isinstance(events, dict):
            print("[Inactivity] Ignored malformed event collection.")
            return None

        # Existing announcement-only records also prove the source was handled.
        legacy_sources = {
            event["sourceEventId"] for event in events.values()
            if isinstance(event, dict)
            and event.get("type") == "inactivity_voice_check_sent"
            and self._valid_key(event.get("sourceEventId"))
            and ("elderId" not in event or event["elderId"] == self.elder_id)
        }
        eligible = [
            (event_id, event) for event_id, event in events.items()
            if event_id not in legacy_sources and self._eligible(event_id, event)
        ]
        if not eligible:
            return None
        event_id, event = min(eligible, key=lambda item: (item[1]["createdAt"], item[0]))
        return PendingCheck(event_id, uuid4().hex, self._identity(event))

    def _owns(self, event, check, status):
        if (not isinstance(event, dict)
                or not self._valid_timestamp(event.get("createdAt"))
                or self._identity(event) != check.identity):
            return False
        marker = self._marker(event)
        return (
            isinstance(marker, dict) and marker.get("status") == status
            and marker.get("claimToken") == check.claim_token
            and marker.get("attemptedBy") == self.device_id
        )

    def _claim(self, check):
        event, etag = self._read_event(check.event_id, with_etag=True)
        if event is _READ_FAILED:
            return False
        if self._owns(event, check, "processing"):
            self._pending_claim = None
            # A guardian may have resolved the event while a response was lost.
            return event.get("status", "open") == "open"
        if not self._eligible(check.event_id, event) or self._identity(event) != check.identity:
            self._pending_claim = None
            return False

        claimed = deepcopy(event)
        # Legacy sources omit status; persist the same open state we accept.
        claimed.setdefault("status", "open")
        claimed.setdefault("meta", {})["voiceCheck"] = {
            "status": "processing",
            "claimToken": check.claim_token,
            "attemptedAt": SERVER_TIMESTAMP,
            "attemptedBy": self.device_id,
        }
        try:
            response = self.http.put(
                self._event_url(check.event_id), json=claimed,
                headers={"If-Match": etag}, timeout=self.REQUEST_TIMEOUT,
            )
            if 200 <= response.status_code < 300:
                self._pending_claim = None
                return True
            if response.status_code == 412:
                self._pending_claim = None
                return False
        except Exception:
            pass
        delivered, _etag = self._read_event(check.event_id, with_etag=False)
        if self._owns(delivered, check, "processing"):
            self._pending_claim = None
            return delivered.get("status", "open") == "open"
        print("[Inactivity] Claim unconfirmed; will retry without audio.")
        return False

    def _completed_matches(self, event, check):
        if not self._owns(event, check, "completed"):
            return False
        marker = self._marker(event)
        return (
            marker.get("outcome") == check.outcome and "completedAt" in marker
            and marker.get("errorType", "") == check.error_type
        )

    def validate_before_audio(self, event_id):
        """Revalidate a claim after waiting for another voice session to finish.

        The callback calls this while holding the interaction lock, immediately
        before its first audio side effect. Read/identity failures fail closed;
        an existing guardian resolution cancels the check without speaking.
        """
        check = self._active_check
        if check is None or check.event_id != event_id:
            raise RuntimeError("No matching active inactivity check")
        event, _etag = self._read_event(event_id, with_etag=False)
        if event is _READ_FAILED:
            raise RuntimeError("Inactivity source validation unavailable")
        if not self._owns(event, check, "processing"):
            raise RuntimeError("Inactivity source claim no longer matches")
        status = event.get("status", "open")
        if status == "resolved":
            print(f"[Inactivity] source={event_id} resolved while waiting; voice check cancelled.")
            return False
        if status != "open":
            raise RuntimeError("Inactivity source is no longer open")
        return True

    def _complete(self, check):
        event, etag = self._read_event(check.event_id, with_etag=True)
        if event is _READ_FAILED:
            return False
        if self._completed_matches(event, check):
            return True
        if not self._owns(event, check, "processing"):
            print(f"[Inactivity] source={check.event_id} completion conflict; preserved external update.")
            return True

        completed = deepcopy(event)
        marker = completed["meta"]["voiceCheck"]
        marker.update(status="completed", completedAt=SERVER_TIMESTAMP, outcome=check.outcome)
        if check.error_type:
            marker["errorType"] = check.error_type
        else:
            marker.pop("errorType", None)

        if check.outcome == "okay":
            # A voice reassurance cannot clear an independently critical alert.
            if event.get("status", "open") == "open" and event.get("severity") != "critical":
                completed.update(status="resolved", resolvedAt=SERVER_TIMESTAMP)
            elif event.get("severity") == "critical":
                print(f"[Inactivity] source={check.event_id} okay reply; critical alert retained.")
        elif check.outcome == "help":
            # A guardian resolution always wins; never reopen it after the reply.
            completed.update(severity="critical", title=HELP_TITLE)
            if "label" in completed:
                completed["label"] = HELP_TITLE

        try:
            response = self.http.put(
                self._event_url(check.event_id), json=completed,
                headers={"If-Match": etag}, timeout=self.REQUEST_TIMEOUT,
            )
            if 200 <= response.status_code < 300:
                print(
                    f"[Inactivity] source={check.event_id} outcome={check.outcome} "
                    f"status={completed.get('status', 'open')} "
                    f"severity={completed.get('severity', 'unknown')} recorded."
                )
                return True
        except Exception:
            pass
        delivered, _etag = self._read_event(check.event_id, with_etag=False)
        if self._completed_matches(delivered, check):
            return True
        print(f"[Inactivity] source={check.event_id} completion pending; audio will not repeat.")
        return False

    def run_once(self, run_check):
        """Run at most one check, or retry only an already produced outcome."""
        if self._pending_completion is not None:
            if self._complete(self._pending_completion):
                self._pending_completion = None
                return True
            return False
        check = self._pending_claim or self._find_next()
        if check is None:
            return False
        self._pending_claim = check
        if not self._claim(check) or check.event_id in self._started_events:
            return False
        self._started_events.add(check.event_id)
        print(f"[Inactivity] source={check.event_id} claimed; starting voice check.")
        error_type = ""
        self._active_check = check
        try:
            outcome = run_check(check.event_id)
            if not isinstance(outcome, str) or outcome not in OUTCOMES:
                raise ValueError("Unsupported inactivity voice outcome")
        except Exception as exc:
            outcome, error_type = "error", type(exc).__name__
            print(f"[Inactivity] source={check.event_id} voice check failed ({error_type}).")
        finally:
            self._active_check = None
        completion = PendingCheck(
            check.event_id, check.claim_token, check.identity, outcome, error_type
        )
        self._pending_completion = completion
        if self._complete(completion):
            self._pending_completion = None
        return True
