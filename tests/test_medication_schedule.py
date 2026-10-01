"""Remote medication schedule validation, fallback, and deduplication tests."""

from copy import deepcopy
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "raspberry-pi-voice-hub"))

from firebase_medication import FirebaseMedicationClient
from medication_care import MedicationCare
from medication_schedule import (
    build_fallback_entries,
    MedicationScheduleCursor,
    MedicationScheduleEntry,
    MedicationScheduleError,
    MedicationScheduleProvider,
    MedicationScheduleSnapshot,
    SEOUL_TIMEZONE,
    validate_schedule_document,
)
from test_firebase_medication import FakeFirebase, reminder


def schedule_document(entries=None, revision="revision_1"):
    return {
        "schemaVersion": 1,
        "timezone": "Asia/Seoul",
        "revision": revision,
        "entries": entries or {},
    }


def raw_entry(**overrides):
    value = {
        "enabled": True,
        "time": "08:00",
        "medicationName": "",
        "weekdays": [1, 2, 3, 4, 5, 6, 7],
    }
    value.update(overrides)
    return value


def entry(schedule_id="schedule_a", **overrides):
    values = {
        "enabled": True,
        "time": "08:00",
        "medication_name": "",
        "weekdays": (1, 2, 3, 4, 5, 6, 7),
    }
    values.update(overrides)
    return MedicationScheduleEntry(schedule_id=schedule_id, **values)


def snapshot(entries, revision="revision_1", generation=1):
    return MedicationScheduleSnapshot(
        tuple(entries), revision, "remote", "", generation,
    )


class Response:
    def __init__(self, data=None, status=200):
        self._data = deepcopy(data)
        self.status_code = status

    def json(self):
        return deepcopy(self._data)


class ScheduleHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.patch_calls = []

    def get(self, _url, timeout=None):
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def patch(self, url, json, timeout=None):
        self.patch_calls.append((url, deepcopy(json), timeout))
        return Response(status=200)


class MedicationScheduleValidationTests(unittest.TestCase):
    def test_valid_and_explicit_empty_documents_are_distinct_from_missing(self):
        revision, entries = validate_schedule_document(schedule_document())
        self.assertEqual(revision, "revision_1")
        self.assertEqual(entries, ())
        with self.assertRaises(MedicationScheduleError):
            validate_schedule_document(None)

    def test_strict_types_times_days_names_and_ids(self):
        invalid_entries = [
            raw_entry(enabled=1),
            raw_entry(time="8:00"),
            raw_entry(time="24:00"),
            raw_entry(weekdays=[]),
            raw_entry(weekdays=[True]),
            raw_entry(weekdays=[0]),
            raw_entry(weekdays=[8]),
            raw_entry(weekdays=[1, 1]),
            raw_entry(medicationName=123),
            raw_entry(medicationName="약" * 81),
        ]
        for number, invalid in enumerate(invalid_entries):
            with self.subTest(number=number), self.assertRaises(MedicationScheduleError):
                validate_schedule_document(schedule_document({"schedule_a": invalid}))
        with self.assertRaises(MedicationScheduleError):
            validate_schedule_document(schedule_document({"bad.id": raw_entry()}))

    def test_same_enabled_time_may_not_overlap_weekdays(self):
        with self.assertRaises(MedicationScheduleError):
            validate_schedule_document(schedule_document({
                "one": raw_entry(weekdays=[1]),
                "two": raw_entry(weekdays=[1, 2]),
            }))
        _revision, entries = validate_schedule_document(schedule_document({
            "one": raw_entry(weekdays=[1]),
            "two": raw_entry(weekdays=[2]),
            "disabled": raw_entry(enabled=False, weekdays=[1]),
        }))
        self.assertEqual(len(entries), 3)

    def test_fallback_times_are_normalized_and_invalid_values_are_safe(self):
        entries = build_fallback_entries(["8:00", "bad", None, "19:00"])
        self.assertEqual([item.time for item in entries], ["08:00", "19:00"])
        defaults = build_fallback_entries(["bad"])
        self.assertEqual([item.time for item in defaults], ["08:00", "12:00", "19:00"])


class MedicationScheduleProviderTests(unittest.TestCase):
    def make_provider(self, responses):
        return MedicationScheduleProvider(
            "https://example.firebaseio.com", "elder_001", "device_001",
            ["08:00", "12:00", "19:00"],
            http=ScheduleHttp(responses), publish_status=False,
        )

    def test_missing_and_unavailable_cold_start_are_explicit_fallbacks(self):
        missing = self.make_provider([Response(None)]).refresh()
        self.assertEqual((missing.source, missing.reason), ("fallback", "missing"))
        self.assertEqual([item.time for item in missing.entries], ["08:00", "12:00", "19:00"])

        unavailable = self.make_provider([TimeoutError("private")]).refresh()
        self.assertEqual((unavailable.source, unavailable.reason), ("fallback", "unavailable"))

    def test_explicit_empty_is_remote_success_and_later_errors_keep_it(self):
        provider = self.make_provider([
            Response(schedule_document({}, "empty_revision")),
            Response({"schemaVersion": 99}),
            TimeoutError("private"),
        ])
        current = provider.refresh()
        self.assertEqual((current.source, current.entries), ("remote", ()))
        invalid = provider.refresh()
        self.assertEqual((invalid.source, invalid.reason, invalid.entries),
                         ("last_valid", "invalid", ()))
        unavailable = provider.refresh()
        self.assertEqual((unavailable.source, unavailable.reason, unavailable.entries),
                         ("last_valid", "unavailable", ()))

    def test_last_valid_nonempty_settings_survive_missing_document(self):
        provider = self.make_provider([
            Response(schedule_document({"schedule_a": raw_entry()}, "valid_revision")),
            Response(None),
        ])
        self.assertEqual(provider.refresh().source, "remote")
        current = provider.refresh()
        self.assertEqual((current.source, current.reason, current.revision),
                         ("last_valid", "missing", "valid_revision"))
        self.assertEqual(current.entries[0].schedule_id, "schedule_a")


class MedicationScheduleCursorTests(unittest.TestCase):
    def setUp(self):
        self.cursor = MedicationScheduleCursor()

    @staticmethod
    def at(hour, minute=0, day=16):
        return datetime(2026, 9, day, hour, minute, tzinfo=SEOUL_TIMEZONE)

    def test_one_schedule_runs_once_per_day_and_respects_weekday(self):
        current = snapshot([entry(weekdays=(3,))])
        self.assertEqual(len(self.cursor.due(current, self.at(8))), 1)
        self.assertEqual(self.cursor.due(current, self.at(8, 0)), [])
        self.assertEqual(len(self.cursor.due(current, self.at(8, day=23))), 1)

    def test_iso_monday_and_sunday_are_distinct_due_days(self):
        monday = snapshot([entry(weekdays=(1,))])
        monday_cursor = MedicationScheduleCursor()
        self.assertEqual(len(monday_cursor.due(monday, self.at(8, day=14))), 1)
        self.assertEqual(monday_cursor.due(monday, self.at(8, day=20)), [])

        sunday = snapshot([entry(weekdays=(7,))])
        sunday_cursor = MedicationScheduleCursor()
        self.assertEqual(sunday_cursor.due(sunday, self.at(8, day=14)), [])
        self.assertEqual(len(sunday_cursor.due(sunday, self.at(8, day=20))), 1)

    def test_time_edit_after_a_fired_occurrence_never_runs_twice_that_day(self):
        first = snapshot([entry(time="08:00")])
        self.assertEqual(len(self.cursor.due(first, self.at(8))), 1)
        moved = snapshot([entry(time="09:00")], revision="revision_2", generation=2)
        self.assertEqual(self.cursor.due(moved, self.at(8, 30)), [])
        self.assertEqual(self.cursor.due(moved, self.at(9)), [])

    def test_future_edit_can_run_when_original_occurrence_never_ran(self):
        original = snapshot([entry(time="08:00")])
        self.assertEqual(self.cursor.due(original, self.at(8, 30)), [])
        moved = snapshot([entry(time="09:00")], revision="revision_2", generation=2)
        self.assertEqual(self.cursor.due(moved, self.at(8, 31)), [])
        self.assertEqual(len(self.cursor.due(moved, self.at(9))), 1)

    def test_new_or_changed_current_and_past_minutes_do_not_fire_retroactively(self):
        self.cursor.due(snapshot([]), self.at(10))
        changed = snapshot([entry(time="10:00")], revision="revision_2", generation=2)
        self.assertEqual(self.cursor.due(changed, self.at(10)), [])
        self.assertEqual(self.cursor.due(changed, self.at(10, 5)), [])

    def test_suppressed_current_minute_can_be_moved_to_a_future_minute(self):
        self.cursor.due(snapshot([]), self.at(10))
        current = snapshot([entry(time="10:00")], revision="revision_2", generation=2)
        self.assertEqual(self.cursor.due(current, self.at(10)), [])
        future = snapshot([entry(time="11:00")], revision="revision_3", generation=3)
        self.assertEqual(self.cursor.due(future, self.at(10, 1)), [])
        self.assertEqual(len(self.cursor.due(future, self.at(11))), 1)


class MedicationClaimIdentityTests(unittest.TestCase):
    def setUp(self):
        self.http = FakeFirebase()
        self.client = FirebaseMedicationClient(
            "https://example.firebaseio.com", "elder_001", "pi_001", http=self.http,
        )

    @staticmethod
    def scheduled_event(name="아침약"):
        return reminder(meta={
            "scheduleId": "schedule_a",
            "scheduleRevision": "revision_1",
            "medicationName": name,
        })

    def test_same_schedule_id_uses_one_daily_claim_after_time_edit(self):
        callback = Mock(return_value="confirmed")
        self.assertTrue(self.client.run_scheduled(
            "2026-09-16_08:00", self.scheduled_event(), callback,
        ))
        second = FirebaseMedicationClient(
            "https://example.firebaseio.com", "elder_001", "pi_001", http=self.http,
        )
        self.assertFalse(second.run_scheduled(
            "2026-09-16_09:00", self.scheduled_event(), callback,
        ))
        callback.assert_called_once()
        self.assertIn(
            "pi_medication_reminder_sent_pi_001_2026-09-16_schedule_a",
            self.http.records,
        )

    def test_announcement_has_separate_delivery_marker_and_no_fake_voice_outcome(self):
        callback = Mock(return_value="announced")
        self.assertTrue(self.client.run_announcement(
            "2026-09-16_08:00", self.scheduled_event(), callback,
        ))
        event = next(iter(self.http.records.values()))
        self.assertNotIn("voiceCheck", event["meta"])
        self.assertEqual(event["meta"]["reminderDelivery"]["status"], "completed")
        self.assertEqual(event["meta"]["reminderDelivery"]["outcome"], "announced")
        self.assertEqual(event["status"], "open")


class MedicationQueuedChangeTests(unittest.TestCase):
    def test_disabled_or_rescheduled_item_is_cancelled_before_voice_audio(self):
        client = Mock()
        client.validate_before_audio.return_value = True
        conversation = Mock()
        conversation.stop_event = threading.Event()
        conversation.use_interaction.return_value = nullcontext()
        def run_medication_check(_event_id, _validity_check=None, *, before_start=None):
            return "confirmed" if before_start() else "cancelled"

        conversation.run_medication_check = Mock(side_effect=run_medication_check)
        completed = threading.Event()
        outcomes = []

        def run_scheduled(_slot, _event, callback):
            outcomes.append(callback("event_schedule_a"))
            completed.set()
            return True

        client.run_scheduled.side_effect = run_scheduled
        checks = iter([True, False])
        validity = Mock(side_effect=lambda: next(checks))
        care = MedicationCare(client, conversation, Mock()).start()
        try:
            self.assertTrue(care.run_scheduled(
                "2026-09-16_08:00",
                reminder(meta={"scheduleId": "schedule_a"}),
                validity,
            ))
            self.assertTrue(completed.wait(2))
        finally:
            care.stop()
        self.assertEqual(outcomes, ["cancelled"])
        conversation.run_medication_check.assert_called_once()
        client.validate_before_audio.assert_not_called()


if __name__ == "__main__":
    unittest.main()
