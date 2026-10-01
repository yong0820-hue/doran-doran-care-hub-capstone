"""One durable voice attempt per existing scheduled medication reminder.

The scheduled slot and device select a deterministic event ID. Conditional
creation reserves the attempt before any audio; even an unfinished record is
never reclaimed after restart. Failed creation is not retried within this
process. Completion retries only persist an already observed outcome, never
repeat audio. In-memory completion retries do not survive a process restart.
"""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import re
from urllib.parse import quote
from uuid import uuid4


SERVER_TIMESTAMP = {".sv": "timestamp"}
_READ_FAILED = object()
OUTCOMES = {"confirmed", "defer", "no_speech", "unclear", "cancelled"}
ANNOUNCEMENT_OUTCOMES = {"announced", "cancelled"}


@dataclass(frozen=True)
class PendingConfirmation:
    event_id: str
    claim_token: str
    identity: tuple
    outcome: str = ""
    error_type: str = ""
    marker_field: str = "voiceCheck"


class FirebaseMedicationClient:
    """Use the current events collection, status fields and voiceCheck marker."""

    REQUEST_TIMEOUT = 10
    IDENTITY_FIELDS = ("type", "createdAt", "elderId", "deviceId", "runId")

    def __init__(self, base_url, elder_id, device_id, http=None):
        self.base_url = base_url.rstrip("/")
        self.elder_id = elder_id
        self.device_id = device_id
        self.path = f"events/{elder_id}"
        if not self.base_url:
            raise ValueError("FIREBASE_DATABASE_URL is required for medication confirmation")
        if not self._valid_key(elder_id) or not self._valid_key(device_id):
            raise ValueError("ELDER_ID and DEVICE_ID must be valid Firebase keys")
        if http is None:
            import requests

            http = requests
        self.http = http
        self._scheduled_slots = set()
        self._pending_completions = {}
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

    @staticmethod
    def _validate_slot(slot_key):
        if not isinstance(slot_key, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}_\d{2}:\d{2}", slot_key, flags=re.ASCII
        ):
            raise ValueError("Medication slot must be YYYY-MM-DD_HH:MM")
        datetime.strptime(slot_key, "%Y-%m-%d_%H:%M")

    def _event_id(self, slot_key, event=None):
        meta = event.get("meta", {}) if isinstance(event, dict) else {}
        schedule_id = meta.get("scheduleId") if isinstance(meta, dict) else None
        if schedule_id is not None:
            if not self._valid_key(schedule_id):
                raise ValueError("Medication scheduleId must be a valid Firebase key")
            return (
                f"pi_medication_reminder_sent_{self.device_id}_"
                f"{slot_key[:10]}_{schedule_id}"
            )
        return f"pi_medication_reminder_sent_{self.device_id}_{slot_key}"

    def _event_url(self, event_id):
        elder = quote(self.elder_id, safe="")
        return f"{self.base_url}/events/{elder}/{quote(event_id, safe='')}.json"

    @classmethod
    def _identity(cls, event):
        fields = tuple((key in event, event.get(key)) for key in cls.IDENTITY_FIELDS)
        meta = event.get("meta", {})
        return fields + (
            meta.get("scheduledFor") if isinstance(meta, dict) else None,
            meta.get("scheduleId") if isinstance(meta, dict) else None,
        )

    @staticmethod
    def _marker(event, field="voiceCheck"):
        meta = event.get("meta", {})
        return meta.get(field) if isinstance(meta, dict) else None

    def _validate_event(self, slot_key, event):
        if not isinstance(event, dict):
            raise ValueError("Medication event must be a dictionary")
        if (
            event.get("type") != "medication_reminder_sent"
            or event.get("status") != "open"
            or event.get("elderId") != self.elder_id
            or event.get("deviceId") != self.device_id
            or not self._valid_timestamp(event.get("createdAt"))
            or not isinstance(event.get("runId"), str)
            or not event["runId"]
            or not isinstance(event.get("meta", {}), dict)
        ):
            raise ValueError("Invalid scheduled medication event")
        meta = event.get("meta", {})
        if ("voiceCheck" in meta or "reminderDelivery" in meta
                or meta.get("scheduledFor", slot_key) != slot_key):
            raise ValueError("Medication event already contains an attempt or another slot")

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
                return _READ_FAILED, None
            return event, etag
        except Exception:
            return _READ_FAILED, None

    def _owns(self, event, check, status):
        if not isinstance(event, dict) or self._identity(event) != check.identity:
            return False
        marker = self._marker(event, check.marker_field)
        return (
            isinstance(marker, dict) and marker.get("status") == status
            and marker.get("claimToken") == check.claim_token
            and marker.get("attemptedBy") == self.device_id
        )

    def _create(self, check, claimed):
        existing, etag = self._read_event(check.event_id, with_etag=True)
        if existing is _READ_FAILED:
            print(f"[Medication] event={check.event_id} creation unavailable; no audio.", flush=True)
            return False
        if existing is not None:
            print(f"[Medication] event={check.event_id} already exists; attempt skipped.", flush=True)
            return False
        try:
            response = self.http.put(
                self._event_url(check.event_id), json=claimed,
                headers={"If-Match": etag}, timeout=self.REQUEST_TIMEOUT,
            )
            if 200 <= response.status_code < 300:
                return True
            if response.status_code == 412:
                print(f"[Medication] event={check.event_id} claimed elsewhere; no audio.", flush=True)
                return False
        except Exception:
            pass
        # A lost successful response is safe only after reading our exact claim.
        delivered, _etag = self._read_event(check.event_id, with_etag=False)
        if self._owns(delivered, check, "processing"):
            return True
        print(f"[Medication] event={check.event_id} claim unconfirmed; no audio or retry.", flush=True)
        return False

    def _heartbeat(self, timestamp):
        payload = {
            f"devices/{self.device_id}/lastSeen": timestamp,
            f"devices/{self.device_id}/lastSignal": timestamp,
        }
        try:
            response = self.http.patch(
                f"{self.base_url}/.json", json=payload, timeout=self.REQUEST_TIMEOUT,
            )
            if 200 <= response.status_code < 300:
                return
        except Exception:
            pass
        print("[Medication] device heartbeat unavailable; reminder claim retained.", flush=True)

    def validate_before_audio(self, event_id):
        """Revalidate ownership after waiting for the shared interaction lock."""
        check = self._active_check
        if check is None or check.event_id != event_id:
            raise RuntimeError("No matching active medication confirmation")
        event, _etag = self._read_event(event_id, with_etag=False)
        if event is _READ_FAILED:
            raise RuntimeError("Medication source validation unavailable")
        if not self._owns(event, check, "processing"):
            raise RuntimeError("Medication source claim no longer matches")
        status = event.get("status")
        if status == "resolved":
            print(f"[Medication] event={event_id} resolved while queued; confirmation cancelled.", flush=True)
            return False
        if status != "open":
            raise RuntimeError("Medication source is no longer open")
        return True

    def _completed_matches(self, event, check):
        if not self._owns(event, check, "completed"):
            return False
        marker = self._marker(event, check.marker_field)
        return (
            marker.get("outcome") == check.outcome and "completedAt" in marker
            and marker.get("errorType", "") == check.error_type
        )

    def _complete(self, check):
        event, etag = self._read_event(check.event_id, with_etag=True)
        if event is _READ_FAILED:
            return False
        if self._completed_matches(event, check):
            return True
        if not self._owns(event, check, "processing"):
            print(f"[Medication] event={check.event_id} completion conflict; external update preserved.", flush=True)
            return True
        completed = deepcopy(event)
        marker = completed["meta"][check.marker_field]
        marker.update(status="completed", completedAt=SERVER_TIMESTAMP, outcome=check.outcome)
        if check.error_type:
            marker["errorType"] = check.error_type
        else:
            marker.pop("errorType", None)
        if (check.marker_field == "voiceCheck" and check.outcome == "confirmed"
                and event.get("status") == "open"):
            # Preserve an independent critical escalation made during the reply.
            if event.get("severity") != "critical":
                completed.update(status="resolved", resolvedAt=SERVER_TIMESTAMP)
            else:
                print(f"[Medication] event={check.event_id} confirmed reply; critical alert retained.", flush=True)
        try:
            response = self.http.put(
                self._event_url(check.event_id), json=completed,
                headers={"If-Match": etag}, timeout=self.REQUEST_TIMEOUT,
            )
            if 200 <= response.status_code < 300:
                print(f"[Medication] event={check.event_id} outcome={check.outcome} "
                      f"status={completed.get('status')} recorded.", flush=True)
                return True
        except Exception:
            pass
        delivered, _etag = self._read_event(check.event_id, with_etag=False)
        if self._completed_matches(delivered, check):
            return True
        print(f"[Medication] event={check.event_id} completion pending; audio will not repeat.", flush=True)
        return False

    def retry_pending(self):
        """Retry one pending result per pass; never call the audio callback."""
        if not self._pending_completions:
            return False
        event_id = next(iter(self._pending_completions))
        if self._complete(self._pending_completions[event_id]):
            del self._pending_completions[event_id]
            return True
        return False

    def _run_attempt(self, slot_key, event, callback, marker_field, outcomes):
        self._validate_slot(slot_key)
        self._validate_event(slot_key, event)
        event_id = self._event_id(slot_key, event)
        if event_id in self._scheduled_slots:
            print(f"[Medication] slot={slot_key} already handled; attempt skipped.", flush=True)
            return False
        self._scheduled_slots.add(event_id)
        claimed = deepcopy(event)
        meta = claimed.setdefault("meta", {})
        meta["scheduledFor"] = slot_key
        token = uuid4().hex
        meta[marker_field] = {
            "status": "processing", "claimToken": token,
            "attemptedAt": SERVER_TIMESTAMP, "attemptedBy": self.device_id,
        }
        check = PendingConfirmation(
            event_id, token, self._identity(claimed), marker_field=marker_field,
        )
        if not self._create(check, claimed):
            return False
        self._heartbeat(claimed["createdAt"])
        print(f"[Medication] event={check.event_id} slot={slot_key} claimed; starting voice attempt.", flush=True)
        self._active_check = check
        error_type = ""
        try:
            outcome = callback(check.event_id)
            if not isinstance(outcome, str) or outcome not in outcomes:
                raise ValueError("Unsupported medication confirmation outcome")
        except Exception as exc:
            outcome, error_type = "error", type(exc).__name__
            print(f"[Medication] event={check.event_id} confirmation failed ({error_type}).", flush=True)
        finally:
            self._active_check = None
        completion = PendingConfirmation(
            check.event_id, check.claim_token, check.identity, outcome, error_type,
            marker_field,
        )
        self._pending_completions[check.event_id] = completion
        if self._complete(completion):
            del self._pending_completions[check.event_id]
        return True

    def run_scheduled(self, slot_key, event, run_check):
        """Reserve one voice-confirmation attempt for this daily schedule."""
        return self._run_attempt(
            slot_key, event, run_check, "voiceCheck", OUTCOMES,
        )

    def run_announcement(self, slot_key, event, run_announcement):
        """Reserve an announcement without inventing a voice-response result."""
        return self._run_attempt(
            slot_key, event, run_announcement,
            "reminderDelivery", ANNOUNCEMENT_OUTCOMES,
        )
