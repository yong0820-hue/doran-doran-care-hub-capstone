"""Offline proactive-care claims, outcomes and concurrency regressions."""

from collections import deque
from contextlib import redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
from urllib.parse import unquote, urlsplit


HUB_DIR = Path(__file__).resolve().parents[1] / "raspberry-pi-voice-hub"
sys.path.insert(0, str(HUB_DIR))

from firebase_inactivity import FirebaseInactivityClient, HELP_TITLE, SERVER_TIMESTAMP


class Response:
    def __init__(self, data=None, status=200, etag=None):
        self.data = deepcopy(data)
        self.status_code = status
        self.headers = {"ETag": etag} if etag is not None else {}

    def json(self):
        return deepcopy(self.data)


class FakeFirebase:
    """A stateful source-event REST double with conditional-write enforcement."""

    def __init__(self, records):
        self.records = deepcopy(records)
        self.versions = {key: 1 for key in records}
        self.get_calls = []
        self.put_calls = []
        self.fail_gets = set()
        self.omit_etag_gets = set()
        self.put_actions = deque()
        self.before_get = None

    @staticmethod
    def event_id(url):
        parts = urlsplit(url).path.removesuffix(".json").strip("/").split("/")
        return unquote(parts[2]) if len(parts) == 3 else None

    def replace(self, event_id, event):
        self.records[event_id] = deepcopy(event)
        self.versions[event_id] = self.versions.get(event_id, 0) + 1

    def get(self, url, headers=None, timeout=None):
        self.get_calls.append((url, headers, timeout))
        number = len(self.get_calls)
        if self.before_get:
            self.before_get(number)
        if number in self.fail_gets:
            raise TimeoutError("simulated unavailable read")
        event_id = self.event_id(url)
        if event_id is None:
            return Response(self.records)
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
            raise TimeoutError("simulated unavailable write")
        if action == "server_error":
            return Response(status=503)
        if action == "claim_race":
            event = deepcopy(self.records[event_id])
            event.setdefault("meta", {})["voiceCheck"] = {
                "status": "processing", "claimToken": "other-worker", "attemptedBy": "pi_002"
            }
            self.replace(event_id, event)
        if headers != {"If-Match": f'"version-{self.versions.get(event_id, 0)}"'}:
            return Response(status=412)
        self.replace(event_id, json)
        if action == "commit_timeout":
            raise TimeoutError("committed write response lost")
        return Response(json)


def source(created_at=100, **overrides):
    event = {
        "type": "long_inactivity", "status": "open", "severity": "warning",
        "createdAt": created_at, "deviceId": "sensor_001", "elderId": "elder_001",
        "title": "장시간 미활동", "message": "센서 미활동 안내", "location": "거실",
    }
    event.update(overrides)
    return event


class FirebaseInactivityTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.addCleanup(self.output.close)
        quiet = redirect_stdout(self.output)
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.http = FakeFirebase({"sensor_event_1": source()})
        self.client = self.new_client()
        self.check = Mock(return_value="okay")

    def new_client(self, **overrides):
        values = {"base_url": "https://example.firebaseio.com/", "elder_id": "elder_001",
                  "device_id": "pi_001", "http": self.http}
        values.update(overrides)
        return FirebaseInactivityClient(**values)

    @property
    def event(self):
        return self.http.records["sensor_event_1"]

    @property
    def marker(self):
        return self.event["meta"]["voiceCheck"]

    def mutate_event(self, **updates):
        event = deepcopy(self.event)
        event.update(updates)
        self.http.replace("sensor_event_1", event)

    def test_claim_precedes_audio_and_okay_resolves_source_with_metadata_preserved(self):
        original = source(meta={"confidence": 0.98, "sensor": {"reading": 5}}, extra="retain")
        self.http.replace("sensor_event_1", original)

        def check(event_id):
            self.assertEqual(event_id, "sensor_event_1")
            self.assertEqual(self.marker["status"], "processing")
            self.assertEqual(self.marker["attemptedBy"], "pi_001")
            self.assertEqual(self.marker["attemptedAt"], SERVER_TIMESTAMP)
            self.assertTrue(self.marker["claimToken"])
            self.assertEqual(self.event["status"], "open")
            return "okay"

        self.check.side_effect = check
        self.assertTrue(self.client.run_once(self.check))
        self.check.assert_called_once_with("sensor_event_1")
        self.assertEqual(self.event["status"], "resolved")
        self.assertEqual(self.event["resolvedAt"], SERVER_TIMESTAMP)
        self.assertEqual(self.marker["status"], "completed")
        self.assertEqual(self.marker["outcome"], "okay")
        self.assertEqual(self.marker["completedAt"], SERVER_TIMESTAMP)
        for key, value in original.items():
            if key not in {"status", "meta"}:
                self.assertEqual(self.event[key], value)
        self.assertEqual(self.event["meta"]["sensor"], {"reading": 5})
        self.assertEqual(self.event["meta"]["confidence"], 0.98)
        self.assertEqual([call[2] for call in self.http.put_calls],
                         [{"If-Match": '"version-2"'}, {"If-Match": '"version-3"'}])
        self.assertTrue(all(call[2] == 10 for call in self.http.get_calls))
        self.assertTrue(all(call[3] == 10 for call in self.http.put_calls))
        self.assertFalse(self.client.run_once(self.check))

    def test_help_escalates_existing_event_and_label_without_new_event_or_device_write(self):
        self.event["label"] = "기존 안내"
        self.check.return_value = "help"
        self.assertTrue(self.client.run_once(self.check))
        self.assertEqual(len(self.http.records), 1)
        self.assertEqual((self.event["severity"], self.event["status"]), ("critical", "open"))
        self.assertEqual(self.event["type"], "long_inactivity")
        self.assertEqual(self.event["title"], HELP_TITLE)
        self.assertEqual(self.event["label"], HELP_TITLE)
        self.assertEqual(self.marker["outcome"], "help")
        self.assertNotIn("resolvedAt", self.event)
        self.assertTrue(all("/events/" in call[0] for call in self.http.put_calls))

    def test_unclear_retains_open_status_and_severity(self):
        self.check.return_value = "unclear"
        self.assertTrue(self.client.run_once(self.check))
        self.assertEqual((self.event["status"], self.event["severity"]), ("open", "warning"))
        self.assertEqual(self.marker["outcome"], "unclear")
        self.assertNotIn("resolvedAt", self.event)

    def test_no_speech_records_attempt_without_resolving_or_repeating(self):
        self.check.return_value = "no_speech"
        self.assertTrue(self.client.run_once(self.check))
        self.assertEqual(self.event["status"], "open")
        self.assertEqual(self.marker["outcome"], "no_speech")
        self.assertFalse(self.client.run_once(self.check))
        self.assertFalse(self.new_client().run_once(self.check))
        self.check.assert_called_once_with("sensor_event_1")

    def test_callback_exception_records_only_safe_error_type(self):
        private = "secret transcript and private URL"
        self.check.side_effect = RuntimeError(private)
        self.assertTrue(self.client.run_once(self.check))
        self.assertEqual((self.marker["status"], self.marker["outcome"]), ("completed", "error"))
        self.assertEqual(self.marker["errorType"], "RuntimeError")
        self.assertEqual(self.event["status"], "open")
        self.assertNotIn(private, str(self.event))
        self.assertNotIn(private, self.output.getvalue())
        self.assertFalse(self.new_client().run_once(self.check))

    def test_invalid_callback_outcome_is_recorded_as_error_without_resolution(self):
        for result in (None, True, "resolved", [], {"outcome": "okay"}):
            with self.subTest(result=result):
                self.http = FakeFirebase({"sensor_event_1": source()})
                self.check.return_value = result
                self.assertTrue(self.new_client().run_once(self.check))
                self.assertEqual(self.event["status"], "open")
                self.assertEqual(self.marker["outcome"], "error")
                self.assertEqual(self.marker["errorType"], "ValueError")

    def test_old_open_backlog_is_handled_in_timestamp_then_id_order(self):
        self.http = FakeFirebase({"z": source(200), "b": source(0), "a": source(0)})
        self.client = self.new_client()
        for _ in range(3):
            self.assertTrue(self.client.run_once(self.check))
        self.assertEqual([call.args[0] for call in self.check.call_args_list], ["a", "b", "z"])
        self.assertFalse(self.client.run_once(self.check))

    def test_missing_optional_elder_and_status_fields_are_compatible(self):
        self.event.pop("elderId")
        self.event.pop("status")
        self.assertTrue(self.client.run_once(self.check))
        self.assertEqual(self.event["deviceId"], "sensor_001")
        self.assertNotIn("elderId", self.event)

    def test_sensor_device_id_need_not_match_pi(self):
        self.event["deviceId"] = "esp32_radar_42"
        self.assertTrue(self.client.run_once(self.check))
        self.assertEqual(self.event["deviceId"], "esp32_radar_42")
        self.assertEqual(self.marker["attemptedBy"], "pi_001")

    def test_malformed_sources_and_other_elders_never_start_audio(self):
        invalid = [None, [], "open", source(type="other"), source(status="resolved"),
                   source(status="cancelled"), source(created_at=-1), source(created_at=True),
                   source(created_at=1.5), source(created_at="100"), source(created_at=None),
                   source(elderId="elder_002"), source(meta=[]), source(meta="invalid"),
                   source(meta=None)]
        for event in invalid:
            with self.subTest(event=event):
                self.http = FakeFirebase({"sensor_event_1": event})
                self.assertFalse(self.new_client().run_once(self.check))
                self.assertEqual(self.http.put_calls, [])
        self.check.assert_not_called()

    def test_any_existing_attempt_marker_prevents_new_or_restart_audio(self):
        for marker in (None, False, "unknown", {}, {"status": "processing"},
                       {"status": "completed", "outcome": "okay"}):
            with self.subTest(marker=marker):
                self.http = FakeFirebase({"sensor_event_1": source(meta={"voiceCheck": marker})})
                self.assertFalse(self.new_client().run_once(self.check))
                self.assertEqual(self.http.put_calls, [])
        self.check.assert_not_called()

    def test_existing_legacy_voice_check_suppresses_its_source_even_when_resolved(self):
        self.http.records["old_check"] = {
            "type": "inactivity_voice_check_sent", "sourceEventId": "sensor_event_1",
            "status": "resolved", "createdAt": 200,
        }
        self.assertFalse(self.client.run_once(self.check))
        self.check.assert_not_called()

    def test_legacy_check_does_not_suppress_other_source_or_other_elder(self):
        self.http.records["other_check"] = {
            "type": "inactivity_voice_check_sent", "sourceEventId": "sensor_event_2"
        }
        self.http.records["wrong_elder_check"] = {
            "type": "inactivity_voice_check_sent", "sourceEventId": "sensor_event_1",
            "elderId": "elder_002",
        }
        self.assertTrue(self.client.run_once(self.check))

    def test_invalid_source_keys_never_start_audio(self):
        for key in ("", "a.b", "a#b", "a$b", "a[b", "a]b", "a/b", "a\nb", "a\x7fb"):
            with self.subTest(key=key):
                self.http = FakeFirebase({key: source()})
                self.assertFalse(self.new_client().run_once(self.check))
                self.assertEqual(self.http.put_calls, [])
        self.check.assert_not_called()

    def test_configuration_validation_and_url_segment_encoding(self):
        for key in (None, 3, "", "a.b", "a/b", "a\nb"):
            for name in ("elder_id", "device_id"):
                with self.subTest(name=name, key=key), self.assertRaises(ValueError):
                    self.new_client(**{name: key})
        for interval in (0, 0.5, 61, float("inf"), float("nan"), "invalid"):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                self.new_client(poll_seconds=interval)
        with self.assertRaises(ValueError):
            self.new_client(base_url="")
        client = self.new_client(elder_id="어르신 ?%", poll_seconds="2.5")
        expected = "https://example.firebaseio.com/events/%EC%96%B4%EB%A5%B4%EC%8B%A0%20%3F%25"
        self.assertEqual(client._collection_url(), expected + ".json")
        self.assertEqual(client._event_url("안부 ?%"), expected + "/%EC%95%88%EB%B6%80%20%3F%25.json")
        self.assertEqual(client.poll_seconds, 2.5)

    def test_malformed_empty_and_unavailable_collection_does_not_start_audio(self):
        for response in (Response(None), Response([]), Response("invalid"), Response({}),
                         Response(status=503), TimeoutError("offline")):
            with self.subTest(response=repr(response)):
                http = Mock()
                if isinstance(response, Exception):
                    http.get.side_effect = response
                else:
                    http.get.return_value = response
                self.assertFalse(self.new_client(http=http).run_once(self.check))
                http.put.assert_not_called()
        self.check.assert_not_called()

    def test_missing_etag_defers_audio_until_owned_conditional_claim_is_possible(self):
        self.http.omit_etag_gets.add(2)
        self.assertFalse(self.client.run_once(self.check))
        self.check.assert_not_called()
        self.assertEqual(self.http.put_calls, [])
        self.assertTrue(self.client.run_once(self.check))

    def test_source_resolved_after_poll_is_revalidated_before_claim(self):
        self.http.before_get = lambda number: self.mutate_event(status="resolved") if number == 2 else None
        self.assertFalse(self.client.run_once(self.check))
        self.check.assert_not_called()
        self.assertEqual(self.http.put_calls, [])

    def test_source_identity_changed_after_poll_is_not_claimed(self):
        self.http.before_get = lambda number: self.mutate_event(createdAt=200) if number == 2 else None
        self.assertFalse(self.client.run_once(self.check))
        self.check.assert_not_called()
        self.assertEqual(self.http.put_calls, [])

    def test_competing_conditional_claim_loser_never_starts_audio(self):
        self.http.put_actions.append("claim_race")
        self.assertFalse(self.client.run_once(self.check))
        self.assertFalse(self.client.run_once(self.check))
        self.check.assert_not_called()
        self.assertEqual(self.marker["claimToken"], "other-worker")
        self.assertEqual(len(self.http.put_calls), 1)

    def test_lost_claim_response_runs_only_after_owned_readback(self):
        self.http.put_actions.append("commit_timeout")
        self.assertTrue(self.client.run_once(self.check))
        self.check.assert_called_once_with("sensor_event_1")
        self.assertEqual(self.marker["status"], "completed")
        self.assertFalse(self.client.run_once(self.check))

    def test_lost_claim_and_readback_responses_defer_then_resume_same_owned_claim(self):
        self.http.put_actions.append("commit_timeout")
        self.http.fail_gets.update({3, 4})
        self.assertFalse(self.client.run_once(self.check))
        self.assertFalse(self.client.run_once(self.check))
        self.check.assert_not_called()
        token = self.marker["claimToken"]
        self.assertTrue(self.client.run_once(self.check))
        self.assertEqual(self.marker["claimToken"], token)
        self.check.assert_called_once_with("sensor_event_1")
        self.assertEqual(len(self.http.put_calls), 2)

    def test_failed_uncommitted_claim_is_retried_without_premature_audio(self):
        self.http.put_actions.append("timeout")
        self.assertFalse(self.client.run_once(self.check))
        self.check.assert_not_called()
        self.assertNotIn("meta", self.event)
        self.assertTrue(self.client.run_once(self.check))
        self.check.assert_called_once_with("sensor_event_1")

    def test_server_error_claim_is_retried(self):
        self.http.put_actions.append("server_error")
        self.assertFalse(self.client.run_once(self.check))
        self.check.assert_not_called()
        self.assertTrue(self.client.run_once(self.check))

    def test_restart_never_reclaims_uncertain_processing_attempt(self):
        self.http.put_actions.append("commit_timeout")
        self.http.fail_gets.add(3)
        self.assertFalse(self.client.run_once(self.check))
        self.assertEqual(self.marker["status"], "processing")
        self.assertFalse(self.new_client().run_once(self.check))
        self.check.assert_not_called()

    def test_resolved_during_uncertain_claim_wait_is_not_spoken(self):
        self.http.put_actions.append("commit_timeout")
        self.http.fail_gets.add(3)
        self.assertFalse(self.client.run_once(self.check))
        self.mutate_event(status="resolved", resolvedAt=777)
        self.assertFalse(self.client.run_once(self.check))
        self.check.assert_not_called()
        self.assertEqual(self.event["resolvedAt"], 777)

    def test_completion_write_failure_retries_only_result_and_blocks_next_audio(self):
        self.http.records["next_event"] = source(200)
        self.http.versions["next_event"] = 1
        self.http.put_actions.extend(["success", "timeout", "timeout"])
        self.assertTrue(self.client.run_once(self.check))
        self.assertEqual(self.marker["status"], "processing")
        self.assertFalse(self.client.run_once(self.check))
        self.check.assert_called_once_with("sensor_event_1")
        self.assertTrue(self.client.run_once(self.check))
        self.check.assert_called_once_with("sensor_event_1")
        self.assertTrue(self.client.run_once(self.check))
        self.assertEqual(self.check.call_count, 2)
        self.assertEqual(self.check.call_args.args, ("next_event",))

    def test_lost_completion_response_readback_preserves_guardian_resolution(self):
        self.check.return_value = "help"
        self.http.put_actions.extend(["success", "commit_timeout"])

        def resolve_before_readback(number):
            if number == 4:
                self.mutate_event(status="resolved", resolvedAt=777)

        self.http.before_get = resolve_before_readback
        self.assertTrue(self.client.run_once(self.check))
        self.assertEqual((self.event["status"], self.event["resolvedAt"]), ("resolved", 777))
        self.assertFalse(self.client.run_once(self.check))
        self.assertEqual(len(self.http.put_calls), 2)

    def test_lost_completion_and_readback_responses_are_recovered_without_rewrite(self):
        self.http.put_actions.extend(["success", "commit_timeout"])
        self.http.fail_gets.add(4)
        self.assertTrue(self.client.run_once(self.check))
        self.mutate_event(resolvedAt=777)
        self.assertTrue(self.client.run_once(self.check))
        self.assertEqual(self.event["resolvedAt"], 777)
        self.assertEqual(len(self.http.put_calls), 2)
        self.check.assert_called_once_with("sensor_event_1")

    def test_okay_preserves_guardian_resolution_timestamp_during_audio(self):
        self.check.side_effect = lambda _event: (self.mutate_event(status="resolved", resolvedAt=777) or "okay")
        self.assertTrue(self.client.run_once(self.check))
        self.assertEqual(self.marker["outcome"], "okay")
        self.assertEqual(self.event["resolvedAt"], 777)

    def test_help_preserves_guardian_resolution_during_audio(self):
        self.check.side_effect = lambda _event: (self.mutate_event(status="resolved", resolvedAt=777) or "help")
        self.assertTrue(self.client.run_once(self.check))
        self.assertEqual((self.event["status"], self.event["severity"]), ("resolved", "critical"))
        self.assertEqual(self.event["resolvedAt"], 777)

    def test_okay_does_not_clear_concurrent_critical_severity(self):
        self.check.side_effect = lambda _event: (self.mutate_event(severity="critical", title="보호자 긴급 표시") or "okay")
        self.assertTrue(self.client.run_once(self.check))
        self.assertEqual((self.event["status"], self.event["severity"]), ("open", "critical"))
        self.assertEqual(self.event["title"], "보호자 긴급 표시")
        self.assertEqual(self.marker["outcome"], "okay")
        self.assertNotIn("resolvedAt", self.event)

    def test_okay_does_not_clear_already_critical_source(self):
        self.event["severity"] = "critical"
        self.assertTrue(self.client.run_once(self.check))
        self.assertEqual(self.event["status"], "open")
        self.assertEqual(self.event["severity"], "critical")

    def test_completion_cas_race_retries_with_guardian_metadata_preserved(self):
        def concurrent_change(_event_id):
            self.mutate_event(status="resolved", resolvedAt=777,
                              meta={**self.event["meta"], "guardianNote": "확인"})

        self.http.put_actions.extend(["success", concurrent_change])
        self.assertTrue(self.client.run_once(self.check))
        self.assertEqual(self.marker["status"], "processing")
        self.assertTrue(self.client.run_once(self.check))
        self.assertEqual(self.event["resolvedAt"], 777)
        self.assertEqual(self.event["meta"]["guardianNote"], "확인")
        self.check.assert_called_once_with("sensor_event_1")

    def test_completion_cas_race_with_new_critical_does_not_resolve_on_retry(self):
        self.http.put_actions.extend(["success", lambda _event: self.mutate_event(severity="critical")])
        self.assertTrue(self.client.run_once(self.check))
        self.assertTrue(self.client.run_once(self.check))
        self.assertEqual((self.event["status"], self.event["severity"]), ("open", "critical"))
        self.check.assert_called_once_with("sensor_event_1")

    def test_changed_source_identity_during_audio_is_never_overwritten(self):
        for update in ({"type": "emergency_call"}, {"createdAt": 999},
                       {"createdAt": 100.0}, {"elderId": "elder_002"},
                       {"deviceId": "other_sensor"}):
            with self.subTest(update=update):
                self.http = FakeFirebase({"sensor_event_1": source()})
                self.check.side_effect = lambda _event, update=update: (self.mutate_event(**update) or "okay")
                self.assertTrue(self.new_client().run_once(self.check))
                self.assertEqual(len(self.http.put_calls), 1)
                for key, value in update.items():
                    self.assertEqual(self.event[key], value)
                self.assertEqual(self.event["status"], "open")

    def test_replaced_attempt_marker_is_never_overwritten(self):
        external = {"status": "processing", "claimToken": "other", "attemptedBy": "pi_002"}
        self.check.side_effect = lambda _event: (self.mutate_event(meta={"voiceCheck": external}) or "help")
        self.assertTrue(self.client.run_once(self.check))
        self.assertEqual(self.marker, external)
        self.assertEqual(self.event["severity"], "warning")
        self.assertEqual(len(self.http.put_calls), 1)

    def test_deleted_event_during_audio_is_not_recreated(self):
        self.check.side_effect = lambda _event: (self.http.replace("sensor_event_1", None) or "help")
        self.assertTrue(self.client.run_once(self.check))
        self.assertIsNone(self.event)
        self.assertEqual(len(self.http.put_calls), 1)

    def test_interruption_after_claim_never_repeats_audio_same_process_or_restart(self):
        self.check.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.client.run_once(self.check)
        self.assertEqual(self.marker["status"], "processing")
        self.assertFalse(self.client.run_once(self.check))
        self.assertFalse(self.new_client().run_once(self.check))
        self.check.assert_called_once_with("sensor_event_1")

    def test_marker_removed_externally_still_does_not_repeat_audio_in_same_process(self):
        self.check.return_value = "unclear"
        self.assertTrue(self.client.run_once(self.check))
        self.mutate_event(meta={})
        self.assertFalse(self.client.run_once(self.check))
        self.check.assert_called_once_with("sensor_event_1")

    def test_pre_audio_validation_accepts_only_active_owned_open_source(self):
        def check(event_id):
            self.assertTrue(self.client.validate_before_audio(event_id))
            return "okay"

        self.check.side_effect = check
        self.assertTrue(self.client.run_once(self.check))
        self.assertIsNone(self.client._active_check)
        with self.assertRaises(RuntimeError):
            self.client.validate_before_audio("sensor_event_1")

    def test_pre_audio_validation_after_guardian_resolution_cancels_without_audio(self):
        audio = Mock()

        def check(event_id):
            self.mutate_event(status="resolved", resolvedAt=777)
            if not self.client.validate_before_audio(event_id):
                return "cancelled"
            audio()
            return "okay"

        self.check.side_effect = check
        self.assertTrue(self.client.run_once(self.check))
        audio.assert_not_called()
        self.assertEqual(self.marker["outcome"], "cancelled")
        self.assertEqual(self.marker["status"], "completed")
        self.assertEqual((self.event["status"], self.event["resolvedAt"]), ("resolved", 777))
        self.assertIsNone(self.client._active_check)
        self.assertFalse(self.new_client().run_once(self.check))

    def test_pre_audio_validation_read_failure_fails_closed_and_records_safe_error(self):
        audio = Mock()
        self.http.fail_gets.add(3)

        def check(event_id):
            self.client.validate_before_audio(event_id)
            audio()
            return "okay"

        self.check.side_effect = check
        self.assertTrue(self.client.run_once(self.check))
        audio.assert_not_called()
        self.assertEqual(self.marker["outcome"], "error")
        self.assertEqual(self.marker["errorType"], "RuntimeError")
        self.assertEqual(self.event["status"], "open")
        self.assertIsNone(self.client._active_check)

    def test_pre_audio_validation_rejects_changed_identity_and_external_claim(self):
        for update in ({"createdAt": 999}, {"deviceId": "other_sensor"},
                       {"elderId": "elder_002"}, {"type": "emergency_call"},
                       {"meta": {"voiceCheck": {"status": "processing",
                                                  "claimToken": "external",
                                                  "attemptedBy": "pi_002"}}}):
            with self.subTest(update=update):
                self.http = FakeFirebase({"sensor_event_1": source()})
                client = self.new_client()
                audio = Mock()

                def check(event_id):
                    self.mutate_event(**update)
                    client.validate_before_audio(event_id)
                    audio()
                    return "okay"

                self.assertTrue(client.run_once(check))
                audio.assert_not_called()
                self.assertEqual(len(self.http.put_calls), 1)
                self.assertIsNone(client._active_check)

    def test_pre_audio_validation_rejects_wrong_source_id_without_reading_it(self):
        def check(_event_id):
            count = len(self.http.get_calls)
            with self.assertRaises(RuntimeError):
                self.client.validate_before_audio("other_event")
            self.assertEqual(len(self.http.get_calls), count)
            return "unclear"

        self.assertTrue(self.client.run_once(check))
        self.assertEqual(self.marker["outcome"], "unclear")

    def test_pre_audio_validation_rejects_unexpected_source_status(self):
        audio = Mock()

        def check(event_id):
            self.mutate_event(status="cancelled")
            self.client.validate_before_audio(event_id)
            audio()
            return "okay"

        self.assertTrue(self.client.run_once(check))
        audio.assert_not_called()
        self.assertEqual(self.event["status"], "cancelled")
        self.assertEqual(self.marker["outcome"], "error")

    def test_active_check_is_cleared_when_callback_is_interrupted(self):
        def check(event_id):
            self.assertTrue(self.client.validate_before_audio(event_id))
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.client.run_once(check)
        self.assertIsNone(self.client._active_check)
        with self.assertRaises(RuntimeError):
            self.client.validate_before_audio("sensor_event_1")
        self.assertFalse(self.client.run_once(self.check))
        self.check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
