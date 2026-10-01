"""Offline medication slot claims, outcomes and concurrent update checks."""

from collections import deque
from contextlib import redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
from urllib.parse import unquote, urlsplit


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "raspberry-pi-voice-hub"))
from firebase_medication import FirebaseMedicationClient, SERVER_TIMESTAMP


class Response:
    def __init__(self, data=None, status=200, etag=None):
        self.data = deepcopy(data)
        self.status_code = status
        self.headers = {"ETag": etag} if etag is not None else {}

    def json(self):
        return deepcopy(self.data)


class FakeFirebase:
    def __init__(self):
        self.records = {}
        self.versions = {}
        self.get_calls = []
        self.put_calls = []
        self.patch_calls = []
        self.fail_gets = set()
        self.omit_etag_gets = set()
        self.put_actions = deque()
        self.patch_action = "success"

    @staticmethod
    def event_id(url):
        parts = urlsplit(url).path.removesuffix(".json").strip("/").split("/")
        return unquote(parts[-1])

    def replace(self, event_id, event):
        self.records[event_id] = deepcopy(event)
        self.versions[event_id] = self.versions.get(event_id, 0) + 1

    def get(self, url, headers=None, timeout=None):
        self.get_calls.append((url, headers, timeout))
        number = len(self.get_calls)
        if number in self.fail_gets:
            raise TimeoutError("private URL in exception")
        event_id = self.event_id(url)
        etag = None
        if headers == {"X-Firebase-ETag": "true"} and number not in self.omit_etag_gets:
            etag = f'"version-{self.versions.get(event_id, 0)}"'
        return Response(self.records.get(event_id), etag=etag)

    def put(self, url, json, headers, timeout):
        self.put_calls.append((url, deepcopy(json), headers, timeout))
        event_id = self.event_id(url)
        action = self.put_actions.popleft() if self.put_actions else "success"
        if callable(action):
            action(event_id)
        if action == "timeout":
            raise TimeoutError("private URL in exception")
        if action == "server_error":
            return Response(status=503)
        if action == "claim_race":
            other = deepcopy(json)
            other["meta"]["voiceCheck"]["claimToken"] = "other-worker"
            self.replace(event_id, other)
        if headers != {"If-Match": f'"version-{self.versions.get(event_id, 0)}"'}:
            return Response(status=412)
        self.replace(event_id, json)
        if action == "commit_timeout":
            raise TimeoutError("committed response lost")
        return Response(json)

    def patch(self, url, json, timeout):
        self.patch_calls.append((url, deepcopy(json), timeout))
        if self.patch_action == "timeout":
            raise TimeoutError("private URL in exception")
        return Response(status=503 if self.patch_action == "server_error" else 200)


def reminder(**overrides):
    event = {
        "type": "medication_reminder_sent", "status": "open", "severity": "info",
        "createdAt": 100, "deviceId": "pi_001", "elderId": "elder_001",
        "title": "복약 안내", "message": "정해진 복약 시간에 음성 안내를 실행했습니다.",
        "location": "거실", "runId": "pi_medication_run_100",
    }
    event.update(overrides)
    return event


class FirebaseMedicationTests(unittest.TestCase):
    SLOT = "2026-09-15_08:00"

    def setUp(self):
        self.output = io.StringIO()
        self.addCleanup(self.output.close)
        quiet = redirect_stdout(self.output)
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.http = FakeFirebase()
        self.client = self.new_client()
        self.check = Mock(return_value="confirmed")

    def new_client(self, **overrides):
        values = {"base_url": "https://example.firebaseio.com/", "elder_id": "elder_001",
                  "device_id": "pi_001", "http": self.http}
        values.update(overrides)
        return FirebaseMedicationClient(**values)

    @property
    def event_id(self):
        return self.client._event_id(self.SLOT)

    @property
    def event(self):
        return self.http.records[self.event_id]

    @property
    def marker(self):
        return self.event["meta"]["voiceCheck"]

    def run_slot(self, **event_fields):
        return self.client.run_scheduled(self.SLOT, reminder(**event_fields), self.check)

    def mutate(self, **updates):
        changed = deepcopy(self.event)
        changed.update(updates)
        self.http.replace(self.event_id, changed)

    def test_claim_precedes_callback_and_confirmed_resolves_existing_event(self):
        original = reminder(meta={"source": "schedule"}, extra="preserved")

        def check(event_id):
            self.assertEqual(event_id, self.event_id)
            self.assertEqual(self.marker["status"], "processing")
            self.assertEqual(self.marker["attemptedBy"], "pi_001")
            self.assertEqual(self.marker["attemptedAt"], SERVER_TIMESTAMP)
            self.assertTrue(self.marker["claimToken"])
            self.assertTrue(self.client.validate_before_audio(event_id))
            return "confirmed"

        self.check.side_effect = check
        self.assertTrue(self.client.run_scheduled(self.SLOT, original, self.check))
        self.assertEqual(self.event["status"], "resolved")
        self.assertEqual(self.event["resolvedAt"], SERVER_TIMESTAMP)
        self.assertEqual(self.marker["outcome"], "confirmed")
        self.assertEqual(self.marker["status"], "completed")
        self.assertEqual(self.marker["completedAt"], SERVER_TIMESTAMP)
        self.assertEqual(self.event["meta"]["scheduledFor"], self.SLOT)
        self.assertEqual(self.event["meta"]["source"], "schedule")
        self.assertEqual(self.event["extra"], "preserved")
        self.assertEqual(self.event["type"], "medication_reminder_sent")
        self.assertEqual(original, reminder(meta={"source": "schedule"}, extra="preserved"))
        self.assertEqual(len(self.http.records), 1)
        self.assertTrue(all(call[2] == 10 for call in self.http.get_calls))
        self.assertTrue(all(call[3] == 10 for call in self.http.put_calls))

    def test_defer_keeps_open_and_records_completed_attempt(self):
        self.check.return_value = "defer"
        self.assertTrue(self.run_slot())
        self.assertEqual((self.event["status"], self.marker["outcome"]), ("open", "defer"))
        self.assertNotIn("resolvedAt", self.event)
        self.assertEqual(self.marker["status"], "completed")

    def test_no_speech_keeps_open_and_records_completed_attempt(self):
        self.check.return_value = "no_speech"
        self.assertTrue(self.run_slot())
        self.assertEqual((self.event["status"], self.marker["outcome"]), ("open", "no_speech"))
        self.assertNotIn("resolvedAt", self.event)

    def test_unclear_keeps_open_and_records_completed_attempt(self):
        self.check.return_value = "unclear"
        self.assertTrue(self.run_slot())
        self.assertEqual((self.event["status"], self.marker["outcome"]), ("open", "unclear"))
        self.assertNotIn("resolvedAt", self.event)

    def test_same_slot_and_restarted_client_never_repeat_any_completed_outcome(self):
        for outcome in ("confirmed", "defer", "no_speech", "unclear", "cancelled"):
            with self.subTest(outcome=outcome):
                self.http = FakeFirebase()
                self.client = self.new_client()
                self.check.reset_mock()
                self.check.return_value = outcome
                self.assertTrue(self.run_slot())
                self.assertFalse(self.run_slot(createdAt=999, runId="different"))
                self.assertFalse(self.new_client().run_scheduled(self.SLOT, reminder(), self.check))
                self.check.assert_called_once_with(self.event_id)
                self.assertEqual(len(self.http.patch_calls), 1)

    def test_next_slot_and_next_day_are_distinct_reminders(self):
        for slot in (self.SLOT, "2026-09-15_12:00", "2026-09-16_08:00"):
            self.assertTrue(self.client.run_scheduled(slot, reminder(), self.check))
        self.assertEqual(len(self.http.records), 3)
        self.assertEqual(self.check.call_count, 3)

    def test_different_devices_have_distinct_slot_ids(self):
        self.assertTrue(self.run_slot())
        other = self.new_client(device_id="pi_002")
        self.assertTrue(other.run_scheduled(self.SLOT, reminder(deviceId="pi_002"), self.check))
        self.assertEqual(len(self.http.records), 2)

    def test_any_existing_record_including_unfinished_claim_blocks_restart_audio(self):
        for record in ({}, [], "malformed", False, reminder(), reminder(status="resolved"),
                       reminder(meta={"voiceCheck": {"status": "processing"}})):
            with self.subTest(record=record):
                self.http = FakeFirebase()
                self.client = self.new_client()
                self.http.replace(self.event_id, record)
                self.assertFalse(self.run_slot())
                self.assertEqual(self.http.put_calls, [])
                self.assertEqual(self.http.patch_calls, [])
        self.check.assert_not_called()

    def test_competing_conditional_create_preserves_winner_without_audio(self):
        self.http.put_actions.append("claim_race")
        self.assertFalse(self.run_slot())
        self.assertEqual(self.marker["claimToken"], "other-worker")
        self.assertFalse(self.new_client().run_scheduled(self.SLOT, reminder(), self.check))
        self.check.assert_not_called()
        self.assertEqual(len(self.http.put_calls), 1)

    def test_lost_create_response_runs_only_after_owned_readback(self):
        self.http.put_actions.append("commit_timeout")
        self.assertTrue(self.run_slot())
        self.check.assert_called_once_with(self.event_id)
        self.assertEqual(self.marker["outcome"], "confirmed")
        self.assertEqual(len(self.http.patch_calls), 1)

    def test_lost_create_and_readback_leave_processing_without_retry_audio(self):
        self.http.put_actions.append("commit_timeout")
        self.http.fail_gets.add(2)
        self.assertFalse(self.run_slot())
        self.assertEqual(self.marker["status"], "processing")
        self.assertFalse(self.run_slot())
        self.assertFalse(self.new_client().run_scheduled(self.SLOT, reminder(), self.check))
        self.assertFalse(self.client.retry_pending())
        self.check.assert_not_called()

    def test_unavailable_create_is_consumed_without_audio_or_same_process_retry(self):
        for action in ("timeout", "server_error"):
            with self.subTest(action=action):
                self.http = FakeFirebase()
                self.client = self.new_client()
                self.http.put_actions.append(action)
                self.assertFalse(self.run_slot())
                self.assertFalse(self.run_slot())
                self.assertFalse(self.client.retry_pending())
                self.assertEqual(self.http.records, {})
                self.assertEqual(len(self.http.put_calls), 1)
        self.check.assert_not_called()

    def test_failed_read_or_missing_etag_never_creates_or_speaks(self):
        for failure in ("read", "etag"):
            with self.subTest(failure=failure):
                self.http = FakeFirebase()
                self.client = self.new_client()
                getattr(self.http, "fail_gets" if failure == "read" else "omit_etag_gets").add(1)
                self.assertFalse(self.run_slot())
                self.assertFalse(self.run_slot())
                self.assertEqual(self.http.put_calls, [])
        self.check.assert_not_called()

    def test_heartbeat_only_patches_existing_device_presence_fields(self):
        self.assertTrue(self.run_slot())
        self.assertEqual(self.http.patch_calls, [(
            "https://example.firebaseio.com/.json",
            {"devices/pi_001/lastSeen": 100, "devices/pi_001/lastSignal": 100}, 10,
        )])

    def test_failed_heartbeat_does_not_lose_claim_or_repeat_audio(self):
        for action in ("timeout", "server_error"):
            with self.subTest(action=action):
                self.http = FakeFirebase()
                self.client = self.new_client()
                self.http.patch_action = action
                self.assertTrue(self.run_slot())
                self.assertEqual(self.marker["status"], "completed")
                self.assertFalse(self.run_slot())
        self.assertEqual(self.check.call_count, 2)

    def test_completion_failure_retries_only_result_without_audio(self):
        self.http.put_actions.extend(["success", "timeout", "server_error"])
        self.assertTrue(self.run_slot())
        self.assertEqual(self.marker["status"], "processing")
        self.assertFalse(self.client.retry_pending())
        self.assertTrue(self.client.retry_pending())
        self.assertFalse(self.client.retry_pending())
        self.assertEqual(self.marker["status"], "completed")
        self.check.assert_called_once_with(self.event_id)

    def test_pending_completion_does_not_block_next_scheduled_slot(self):
        self.http.put_actions.extend(["success", "timeout"])
        self.assertTrue(self.run_slot())
        self.assertTrue(self.client.run_scheduled("2026-09-15_12:00", reminder(), self.check))
        self.assertEqual(self.check.call_count, 2)
        self.assertTrue(self.client.retry_pending())
        self.assertEqual(self.check.call_count, 2)

    def test_lost_completion_response_recognizes_completed_marker_without_rewrite(self):
        self.http.put_actions.extend(["success", "commit_timeout"])
        self.assertTrue(self.run_slot())
        self.assertFalse(self.client.retry_pending())
        self.assertEqual(len(self.http.put_calls), 2)
        self.assertEqual(self.marker["outcome"], "confirmed")

    def test_lost_completion_and_readback_are_recovered_without_rewrite(self):
        self.http.put_actions.extend(["success", "commit_timeout"])
        self.http.fail_gets.add(3)
        self.assertTrue(self.run_slot())
        self.mutate(resolvedAt=777)
        self.assertTrue(self.client.retry_pending())
        self.assertEqual(self.event["resolvedAt"], 777)
        self.assertEqual(len(self.http.put_calls), 2)
        self.check.assert_called_once_with(self.event_id)

    def test_guardian_resolution_during_reply_preserves_timestamp_and_other_metadata(self):
        def check(_event_id):
            self.mutate(status="resolved", resolvedAt=777, guardianNote="checked")
            return "confirmed"

        self.check.side_effect = check
        self.assertTrue(self.run_slot())
        self.assertEqual((self.event["status"], self.event["resolvedAt"]), ("resolved", 777))
        self.assertEqual(self.event["guardianNote"], "checked")
        self.assertEqual(self.marker["outcome"], "confirmed")

    def test_guardian_resolution_while_queued_cancels_before_audio(self):
        audio = Mock()

        def check(event_id):
            self.mutate(status="resolved", resolvedAt=777)
            if not self.client.validate_before_audio(event_id):
                return "cancelled"
            audio()
            return "confirmed"

        self.check.side_effect = check
        self.assertTrue(self.run_slot())
        audio.assert_not_called()
        self.assertEqual(self.marker["outcome"], "cancelled")
        self.assertEqual(self.event["resolvedAt"], 777)

    def test_confirmed_reply_preserves_independent_critical_escalation(self):
        def check(_event_id):
            self.mutate(severity="critical", title="보호자 확인 필요")
            return "confirmed"

        self.check.side_effect = check
        self.assertTrue(self.run_slot())
        self.assertEqual(self.marker["outcome"], "confirmed")
        self.assertEqual((self.event["status"], self.event["severity"]), ("open", "critical"))
        self.assertEqual(self.event["title"], "보호자 확인 필요")
        self.assertNotIn("resolvedAt", self.event)

    def test_guardian_race_at_completion_retries_merge_without_reopening(self):
        self.check.return_value = "defer"

        def resolve(_event_id):
            self.mutate(status="resolved", resolvedAt=777)

        self.http.put_actions.extend(["success", resolve])
        self.assertTrue(self.run_slot())
        self.assertEqual(self.marker["status"], "processing")
        self.assertTrue(self.client.retry_pending())
        self.assertEqual(self.marker["outcome"], "defer")
        self.assertEqual((self.event["status"], self.event["resolvedAt"]), ("resolved", 777))
        self.check.assert_called_once_with(self.event_id)

    def test_changed_source_identity_is_not_overwritten_by_completion(self):
        def check(_event_id):
            self.mutate(createdAt=999, title="replacement")
            return "confirmed"

        self.check.side_effect = check
        self.assertTrue(self.run_slot())
        self.assertFalse(self.client.retry_pending())
        self.assertEqual(self.event["createdAt"], 999)
        self.assertEqual(self.event["status"], "open")
        self.assertEqual(len(self.http.put_calls), 1)

    def test_changed_claim_before_audio_raises_and_preserves_external_event(self):
        def check(event_id):
            self.event["meta"]["voiceCheck"]["claimToken"] = "replacement"
            return self.client.validate_before_audio(event_id)

        self.check.side_effect = check
        self.assertTrue(self.run_slot())
        self.assertEqual(self.marker["claimToken"], "replacement")
        self.assertEqual(self.event["status"], "open")
        self.assertNotIn("outcome", self.marker)

    def test_unavailable_validation_prevents_audio_and_records_error(self):
        self.http.fail_gets.add(2)
        self.check.side_effect = self.client.validate_before_audio
        self.assertTrue(self.run_slot())
        self.assertEqual(self.marker["outcome"], "error")
        self.assertEqual(self.marker["errorType"], "RuntimeError")
        self.assertEqual(self.event["status"], "open")

    def test_no_active_confirmation_cannot_validate_audio(self):
        with self.assertRaises(RuntimeError):
            self.client.validate_before_audio(self.event_id)

    def test_callback_exception_records_only_error_class(self):
        private = "private transcript and private URL"
        self.check.side_effect = RuntimeError(private)
        self.assertTrue(self.run_slot())
        self.assertEqual((self.marker["outcome"], self.marker["errorType"]), ("error", "RuntimeError"))
        self.assertEqual(self.event["status"], "open")
        self.assertNotIn(private, self.output.getvalue())
        self.assertNotIn(private, str(self.event))

    def test_invalid_callback_outcomes_cannot_resolve(self):
        for outcome in (None, True, "okay", "resolved", [], {}):
            with self.subTest(outcome=outcome):
                self.http = FakeFirebase()
                self.client = self.new_client()
                self.check.return_value = outcome
                self.assertTrue(self.run_slot())
                self.assertEqual(self.marker["outcome"], "error")
                self.assertEqual(self.event["status"], "open")

    def test_invalid_slots_are_rejected_before_network(self):
        for slot in (None, 3, "", "2026-9-15_08:00", "2026-09-15_8:00",
                     "2026-09-15_24:00", "2026-02-30_08:00", "2026-09-15_08:00/x"):
            with self.subTest(slot=slot), self.assertRaises(ValueError):
                self.client.run_scheduled(slot, reminder(), self.check)
        self.assertEqual(self.http.get_calls, [])

    def test_invalid_events_are_rejected_before_network(self):
        invalid = [None, [], reminder(type="medication_taken"), reminder(status="resolved"),
                   reminder(deviceId="pi_002"), reminder(elderId="elder_002"),
                   reminder(createdAt=True), reminder(createdAt=-1), reminder(createdAt="100"),
                   reminder(runId=None), reminder(runId=""), reminder(meta=[]),
                   reminder(meta={"voiceCheck": None}), reminder(meta={"scheduledFor": "other"})]
        for event in invalid:
            with self.subTest(event=event), self.assertRaises(ValueError):
                self.client.run_scheduled(self.SLOT, event, self.check)
        self.assertEqual(self.http.get_calls, [])

    def test_configuration_validation_and_url_encoding(self):
        for key in (None, 3, "", "a.b", "a/b", "a\nb"):
            for name in ("elder_id", "device_id"):
                with self.subTest(name=name, key=key), self.assertRaises(ValueError):
                    self.new_client(**{name: key})
        with self.assertRaises(ValueError):
            self.new_client(base_url="")
        client = self.new_client(elder_id="어르신 ?%", device_id="pi ?%")
        self.assertIn("%EC%96%B4%EB%A5%B4%EC%8B%A0%20%3F%25", client._event_url("event ?%"))
        self.assertTrue(client._event_url("event ?%").endswith("/event%20%3F%25.json"))


if __name__ == "__main__":
    unittest.main()
