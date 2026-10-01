"""Validated, hot-reloadable medication schedules from Firebase RTDB."""

from dataclasses import dataclass
from datetime import datetime
import re
import threading
import time
from urllib.parse import quote
from zoneinfo import ZoneInfo

from firebase_auth import DiagnosticReporter, safe_error_details


SCHEMA_VERSION = 1
TIMEZONE_NAME = "Asia/Seoul"
SEOUL_TIMEZONE = ZoneInfo(TIMEZONE_NAME)
SERVER_TIMESTAMP = {".sv": "timestamp"}
TIME_PATTERN = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", re.ASCII)
SCHEDULE_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,120}", re.ASCII)


class MedicationScheduleError(ValueError):
    """The remote schedule exists but is unsafe to apply."""


@dataclass(frozen=True)
class MedicationScheduleEntry:
    schedule_id: str
    enabled: bool
    time: str
    medication_name: str
    weekdays: tuple

    @property
    def scheduling_fingerprint(self):
        return self.enabled, self.time, self.weekdays


@dataclass(frozen=True)
class MedicationScheduleSnapshot:
    entries: tuple
    revision: str
    source: str
    reason: str
    generation: int

    def entry_map(self):
        return {entry.schedule_id: entry for entry in self.entries}


@dataclass(frozen=True)
class DueMedication:
    entry: MedicationScheduleEntry
    date: str
    slot_key: str
    schedule_revision: str


def _valid_firebase_key(value):
    return (
        isinstance(value, str) and bool(value)
        and not any(char in value for char in ".#$[]/")
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def _normalize_time(value):
    if not isinstance(value, str) or not TIME_PATTERN.fullmatch(value):
        raise MedicationScheduleError("time must use 24-hour HH:MM")
    return value


def _normalize_weekdays(value):
    if not isinstance(value, list) or not value:
        raise MedicationScheduleError("weekdays must be a non-empty list")
    if any(type(day) is not int or day < 1 or day > 7 for day in value):
        raise MedicationScheduleError("weekdays must contain ISO integers 1 through 7")
    if len(set(value)) != len(value):
        raise MedicationScheduleError("weekdays must not contain duplicates")
    return tuple(sorted(value))


def _normalize_entry(schedule_id, value):
    if not SCHEDULE_ID_PATTERN.fullmatch(schedule_id or ""):
        raise MedicationScheduleError("schedule ID is invalid")
    if not isinstance(value, dict):
        raise MedicationScheduleError(f"entry {schedule_id} must be an object")
    enabled = value.get("enabled")
    if type(enabled) is not bool:
        raise MedicationScheduleError(f"entry {schedule_id} enabled must be boolean")
    medication_name = value.get("medicationName", "")
    if not isinstance(medication_name, str):
        raise MedicationScheduleError(f"entry {schedule_id} medicationName must be text")
    medication_name = medication_name.strip()
    if len(medication_name) > 80:
        raise MedicationScheduleError(f"entry {schedule_id} medicationName is too long")
    return MedicationScheduleEntry(
        schedule_id=schedule_id,
        enabled=enabled,
        time=_normalize_time(value.get("time")),
        medication_name=medication_name,
        weekdays=_normalize_weekdays(value.get("weekdays")),
    )


def validate_schedule_document(value):
    """Return revision and entries or raise for any unsafe document."""
    if not isinstance(value, dict):
        raise MedicationScheduleError("schedule document must be an object")
    if type(value.get("schemaVersion")) is not int or value["schemaVersion"] != SCHEMA_VERSION:
        raise MedicationScheduleError("unsupported medication schedule schemaVersion")
    if value.get("timezone") != TIMEZONE_NAME:
        raise MedicationScheduleError("medication schedule timezone must be Asia/Seoul")
    revision = value.get("revision")
    if not isinstance(revision, str) or not revision or len(revision) > 160:
        raise MedicationScheduleError("medication schedule revision is missing or invalid")
    raw_entries = value.get("entries")
    if raw_entries is None:
        raw_entries = {}
    if not isinstance(raw_entries, dict):
        raise MedicationScheduleError("medication schedule entries must be an object")
    entries = tuple(sorted(
        (_normalize_entry(schedule_id, entry) for schedule_id, entry in raw_entries.items()),
        key=lambda entry: (entry.time, entry.schedule_id),
    ))
    enabled = [entry for entry in entries if entry.enabled]
    for index, left in enumerate(enabled):
        for right in enabled[index + 1:]:
            if left.time == right.time and set(left.weekdays).intersection(right.weekdays):
                raise MedicationScheduleError(
                    "enabled schedules cannot overlap at the same time and weekday"
                )
    return revision, entries


def build_fallback_entries(times):
    entries = []
    for value in times:
        if not isinstance(value, str):
            continue
        try:
            # Keep the older MEDICATION_TIMES contract, which accepted 8:00,
            # while always exposing strict HH:MM entries to the new scheduler.
            normalized = datetime.strptime(value.strip(), "%H:%M").strftime("%H:%M")
        except ValueError:
            continue
        entries.append(MedicationScheduleEntry(
            schedule_id="fallback_" + normalized.replace(":", ""),
            enabled=True,
            time=normalized,
            medication_name="",
            weekdays=(1, 2, 3, 4, 5, 6, 7),
        ))
    if not entries:
        entries = [
            MedicationScheduleEntry(
                schedule_id="fallback_" + value.replace(":", ""),
                enabled=True,
                time=value,
                medication_name="",
                weekdays=(1, 2, 3, 4, 5, 6, 7),
            )
            for value in ("08:00", "12:00", "19:00")
        ]
    unique = {entry.time: entry for entry in entries}
    return tuple(sorted(unique.values(), key=lambda entry: entry.time))


class MedicationScheduleProvider:
    """Poll RTDB in the background and publish immutable validated snapshots."""

    REQUEST_TIMEOUT = 10

    def __init__(self, base_url, elder_id, device_id, fallback_times, *, http=None,
                 poll_seconds=5, publish_status=True):
        if not _valid_firebase_key(elder_id) or not _valid_firebase_key(device_id):
            raise ValueError("ELDER_ID and DEVICE_ID must be valid Firebase keys")
        if not isinstance(poll_seconds, (int, float)) or isinstance(poll_seconds, bool) \
                or not 1 <= poll_seconds <= 60:
            raise ValueError("medication schedule poll interval must be from 1 to 60 seconds")
        self.base_url = (base_url or "").rstrip("/")
        self.elder_id = elder_id
        self.device_id = device_id
        self.poll_seconds = float(poll_seconds)
        self.publish_status = publish_status
        if http is None:
            import requests
            http = requests
        self.http = http
        self._fallback_entries = build_fallback_entries(fallback_times)
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._generation = 0
        self._last_valid_remote = None
        self._snapshot = MedicationScheduleSnapshot(
            self._fallback_entries, "fallback-env", "fallback", "pending", 0,
        )
        self._published_key = None
        self._publish_diagnostics = DiagnosticReporter('medication_apply_status_publish')

    def _schedule_url(self):
        elder = quote(self.elder_id, safe="")
        device = quote(self.device_id, safe="")
        return f"{self.base_url}/medicationSchedules/{elder}/{device}.json"

    @staticmethod
    def _snapshot_key(snapshot):
        entries = tuple(
            (entry.schedule_id, entry.enabled, entry.time,
             entry.medication_name, entry.weekdays)
            for entry in snapshot.entries
        )
        return snapshot.source, snapshot.reason, snapshot.revision, entries

    def snapshot(self):
        with self._lock:
            return self._snapshot

    def _install(self, entries, revision, source, reason):
        with self._lock:
            candidate = MedicationScheduleSnapshot(
                tuple(entries), revision, source, reason, self._generation,
            )
            if self._snapshot_key(candidate) != self._snapshot_key(self._snapshot):
                self._generation += 1
                candidate = MedicationScheduleSnapshot(
                    tuple(entries), revision, source, reason, self._generation,
                )
            else:
                candidate = MedicationScheduleSnapshot(
                    tuple(entries), revision, source, reason, self._snapshot.generation,
                )
            self._snapshot = candidate
        self._publish_status(candidate)
        return candidate

    def _failure_snapshot(self, reason):
        if self._last_valid_remote is not None:
            revision, entries = self._last_valid_remote
            return self._install(entries, revision, "last_valid", reason)
        return self._install(self._fallback_entries, "fallback-env", "fallback", reason)

    def refresh(self):
        with self._refresh_lock:
            if not self.base_url:
                return self._failure_snapshot("unavailable")
            try:
                response = self.http.get(self._schedule_url(), timeout=self.REQUEST_TIMEOUT)
                status_code = getattr(response, "status_code", None)
                if type(status_code) is not int or status_code != 200:
                    return self._failure_snapshot("unavailable")
                payload = response.json()
            except Exception:
                return self._failure_snapshot("unavailable")
            if payload is None:
                return self._failure_snapshot("missing")
            try:
                revision, entries = validate_schedule_document(payload)
            except MedicationScheduleError:
                return self._failure_snapshot("invalid")
            self._last_valid_remote = (revision, entries)
            return self._install(entries, revision, "remote", "")

    def _publish_status(self, snapshot):
        if not self.publish_status or not self.base_url:
            return
        state_key = self._snapshot_key(snapshot)
        if state_key == self._published_key:
            return
        status = {
            "state": snapshot.source,
            "reason": snapshot.reason or None,
            "revision": snapshot.revision,
            "scheduleCount": len(snapshot.entries),
            "enabledCount": sum(entry.enabled for entry in snapshot.entries),
            "activeTimes": [entry.time for entry in snapshot.entries if entry.enabled],
            "checkedAt": SERVER_TIMESTAMP,
        }
        payload = {f"devices/{self.device_id}/medicationScheduleStatus": status}
        started = time.monotonic()
        status_code = None
        fields = {
            'method': 'PATCH', 'path_template': '/.json',
            'target_template': '/devices/{device}/medicationScheduleStatus',
            'stage': 'apply_status_publish', 'snapshot_generation': snapshot.generation,
            'connect_timeout_ms': self.REQUEST_TIMEOUT * 1000,
            'read_timeout_ms': self.REQUEST_TIMEOUT * 1000,
        }
        try:
            response = self.http.patch(
                f"{self.base_url}/.json", json=payload, timeout=self.REQUEST_TIMEOUT,
            )
            status_code = response.status_code
            if 200 <= status_code < 300:
                self._published_key = state_key
                self._publish_diagnostics.success(
                    **fields, http_status=status_code, request_phase='response',
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )
                return
            # This PATCH has no if-match: a 412 here is an HTTP failure, not a
            # successful handling of an expected conditional-write conflict.
            details = safe_error_details(status=status_code, conditional=False)
        except Exception as exc:
            details = safe_error_details(exc, status=status_code, conditional=False)
        if details['request_phase'] == 'before_request':
            fields.update(stage='transport_pre_request', source_stage='apply_status_publish')
        self._publish_diagnostics.failure(
            **fields, **details, elapsed_ms=(time.monotonic() - started) * 1000,
        )

    def start(self):
        if self._thread is not None:
            return self
        initial = self.refresh()
        print(
            f"[MedicationSchedule] source={initial.source} reason={initial.reason or 'none'} "
            f"revision={initial.revision} entries={len(initial.entries)}",
            flush=True,
        )
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="medication-schedule",
        )
        self._thread.start()
        return self

    def _run(self):
        while not self._stop_event.wait(self.poll_seconds):
            previous = self.snapshot()
            current = self.refresh()
            if current.generation != previous.generation or current.source != previous.source \
                    or current.reason != previous.reason:
                print(
                    f"[MedicationSchedule] source={current.source} "
                    f"reason={current.reason or 'none'} revision={current.revision} "
                    f"entries={len(current.entries)}",
                    flush=True,
                )

    def is_current_occurrence(self, entry, date):
        try:
            weekday = datetime.strptime(date, "%Y-%m-%d").isoweekday()
        except (TypeError, ValueError):
            return False
        current = self.snapshot().entry_map().get(entry.schedule_id)
        return bool(
            current and current.enabled and current.time == entry.time
            and weekday in current.weekdays
        )

    def stop(self):
        self._stop_event.set()
        thread = self._thread
        if thread is not None and threading.current_thread() is not thread:
            thread.join(timeout=3)


class MedicationScheduleCursor:
    """Select due entries once while keeping edit activation rules in memory."""

    def __init__(self):
        self._previous = None
        self._date = None
        self._handled = set()
        self._suppressed = set()

    def due(self, snapshot, now=None):
        now = now or datetime.now(SEOUL_TIMEZONE)
        if now.tzinfo is None:
            now = now.replace(tzinfo=SEOUL_TIMEZONE)
        else:
            now = now.astimezone(SEOUL_TIMEZONE)
        date = now.strftime("%Y-%m-%d")
        hm = now.strftime("%H:%M")
        weekday = now.isoweekday()
        if self._date != date:
            self._date = date
            self._handled.clear()
            self._suppressed.clear()

        current = snapshot.entry_map()
        if self._previous is not None:
            for schedule_id, entry in current.items():
                prior = self._previous.get(schedule_id)
                if prior != entry.scheduling_fingerprint:
                    occurrence = (date, schedule_id)
                    if entry.enabled and weekday in entry.weekdays and entry.time <= hm:
                        self._suppressed.add(occurrence)
                    elif occurrence not in self._handled:
                        # A previously retroactive edit may later be moved to a
                        # future minute. It is eligible then unless it already ran.
                        self._suppressed.discard(occurrence)
        self._previous = {
            schedule_id: entry.scheduling_fingerprint
            for schedule_id, entry in current.items()
        }

        found = []
        for entry in snapshot.entries:
            occurrence = (date, entry.schedule_id)
            if occurrence in self._handled or occurrence in self._suppressed:
                continue
            if entry.enabled and weekday in entry.weekdays and entry.time == hm:
                self._handled.add(occurrence)
                found.append(DueMedication(
                    entry=entry,
                    date=date,
                    slot_key=f"{date}_{entry.time}",
                    schedule_revision=snapshot.revision,
                ))
        return found
