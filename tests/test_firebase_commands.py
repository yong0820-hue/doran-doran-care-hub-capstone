"""Offline command-consumption regressions; no Firebase or audio is contacted."""

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

from firebase_commands import FirebaseCommandClient, SERVER_TIMESTAMP, START_COMMAND


class Response:
    def __init__(self, data=None, status=200, etag=None):
        self.data = deepcopy(data)
        self.status_code = status
        self.headers = {"ETag": etag} if etag is not None else {}

    def json(self):
        return deepcopy(self.data)


class FakeFirebase:
    """Small stateful REST double that enforces conditional writes."""

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
    def command_id(url):
        parts = urlsplit(url).path.removesuffix(".json").strip("/").split("/")
        return unquote(parts[3]) if len(parts) == 4 else None

    def replace(self, command_id, record):
        self.records[command_id] = deepcopy(record)
        self.versions[command_id] = self.versions.get(command_id, 0) + 1

    def get(self, url, headers=None, timeout=None):
        self.get_calls.append((url, headers, timeout))
        number = len(self.get_calls)
        if self.before_get is not None:
            self.before_get(number)
        if number in self.fail_gets:
            raise TimeoutError("simulated offline read")
        command_id = self.command_id(url)
        if command_id is None:
            return Response(self.records)
        etag = None
        if headers == {"X-Firebase-ETag": "true"} and number not in self.omit_etag_gets:
            etag = f'"version-{self.versions.get(command_id, 0)}"'
        return Response(self.records.get(command_id), etag=etag)

    def put(self, url, json, headers, timeout):
        self.put_calls.append((url, deepcopy(json), headers, timeout))
        command_id = self.command_id(url)
        action = self.put_actions.popleft() if self.put_actions else "success"
        if action == "timeout":
            raise TimeoutError("simulated unavailable write")
        if action == "server_error":
            return Response(status=503)
        if action == "race":
            competitor = dict(self.records[command_id], status="processing",
                              acknowledgedBy="pi_001", claimToken="other-worker")
            self.replace(command_id, competitor)
        if headers != {"If-Match": f'"version-{self.versions[command_id]}"'}:
            return Response(status=412)
        self.replace(command_id, json)
        if action == "commit_timeout":
            raise TimeoutError("write committed but response lost")
        return Response(json)


def pending(created_at=100, **overrides):
    result = {"type": START_COMMAND, "status": "pending", "createdAt": created_at}
    result.update(overrides)
    return result


class FirebaseCommandTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.addCleanup(self.output.close)
        self.quiet = redirect_stdout(self.output)
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)
        self.http = FakeFirebase({"command_1": pending()})
        self.client = self.new_client()
        self.turn = Mock(return_value=True)

    def new_client(self, **overrides):
        values = {"base_url": "https://example.firebaseio.com/", "elder_id": "elder_001",
                  "device_id": "pi_001", "http": self.http}
        values.update(overrides)
        return FirebaseCommandClient(**values)

    def test_success_claims_before_audio_and_preserves_command_metadata(self):
        self.http.records["command_1"]["requestedBy"] = "guardian_001"

        def run_turn():
            command = self.http.records["command_1"]
            self.assertEqual(command["status"], "processing")
            self.assertEqual(command["acknowledgedBy"], "pi_001")
            self.assertTrue(command["claimToken"])
            self.assertEqual(command["acknowledgedAt"], SERVER_TIMESTAMP)
            return True

        self.turn.side_effect = run_turn
        self.assertTrue(self.client.run_once(self.turn))
        self.turn.assert_called_once_with()
        command = self.http.records["command_1"]
        self.assertEqual(command["status"], "processed")
        self.assertEqual(command["outcome"], "completed")
        self.assertEqual(command["processedAt"], SERVER_TIMESTAMP)
        self.assertEqual(command["createdAt"], 100)
        self.assertEqual(command["requestedBy"], "guardian_001")
        self.assertEqual(command["commandId"], "command_1")
        self.assertEqual(command["elderId"], "elder_001")
        self.assertEqual(command["deviceId"], "pi_001")
        self.assertNotIn("errorType", command)
        self.assertEqual([call[2] for call in self.http.put_calls],
                         [{"If-Match": '"version-1"'}, {"If-Match": '"version-2"'}])
        self.assertTrue(all(call[2] == 10 for call in self.http.get_calls))
        self.assertTrue(all(call[3] == 10 for call in self.http.put_calls))
        self.assertFalse(self.client.run_once(self.turn))
        self.turn.assert_called_once_with()

    def test_no_speech_is_processed_once_and_clears_stale_error(self):
        self.http.records["command_1"]["errorType"] = "StaleError"
        self.turn.return_value = False
        self.assertTrue(self.client.run_once(self.turn))
        command = self.http.records["command_1"]
        self.assertEqual((command["status"], command["outcome"]), ("processed", "no_speech"))
        self.assertNotIn("errorType", command)
        self.assertFalse(self.client.run_once(self.turn))
        self.turn.assert_called_once_with()

    def test_turn_exception_is_terminal_without_private_error_text(self):
        private_text = "private transcript and credential-shaped content"
        self.turn.side_effect = RuntimeError(private_text)
        self.assertTrue(self.client.run_once(self.turn))
        command = self.http.records["command_1"]
        self.assertEqual((command["status"], command["outcome"]), ("failed", "failed"))
        self.assertEqual(command["errorType"], "RuntimeError")
        self.assertNotIn(private_text, str(command))
        self.assertNotIn(private_text, self.output.getvalue())
        self.assertFalse(self.client.run_once(self.turn))
        self.turn.assert_called_once_with()

    def test_pending_commands_are_consumed_by_timestamp_then_id(self):
        self.http = FakeFirebase({"z": pending(200), "b": pending(100), "a": pending(100)})
        self.client = self.new_client()
        order = []

        def run_turn():
            order.extend(key for key, value in self.http.records.items()
                         if value["status"] == "processing")
            return True

        for _ in range(3):
            self.assertTrue(self.client.run_once(run_turn))
        self.assertEqual(order, ["a", "b", "z"])
        self.assertFalse(self.client.run_once(run_turn))

    def test_invalid_payloads_and_nonpending_statuses_never_start_audio(self):
        invalid = [None, [], "pending", pending(type="other"), pending(status="processing"),
                   pending(status="processed"), pending(status="failed"), pending(status="cancelled"),
                   pending(created_at=-1), pending(created_at=True), pending(created_at=1.5),
                   pending(created_at="100"), pending(created_at=None),
                   pending(commandId="another"), pending(elderId="another"),
                   pending(deviceId="another")]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.http = FakeFirebase({"command_1": payload})
                self.client = self.new_client()
                self.assertFalse(self.client.run_once(self.turn))
                self.assertEqual(self.http.put_calls, [])
        self.turn.assert_not_called()

    def test_matching_optional_identity_fields_are_accepted(self):
        self.http.records["command_1"].update(commandId="command_1", elderId="elder_001",
                                              deviceId="pi_001")
        self.assertTrue(self.client.run_once(self.turn))
        self.turn.assert_called_once_with()

    def test_invalid_command_keys_never_start_audio(self):
        for key in ["", "a.b", "a#b", "a$b", "a[b", "a]b", "a/b", "a\nb", "a\x7fb"]:
            with self.subTest(key=key):
                self.http = FakeFirebase({key: pending()})
                self.client = self.new_client()
                self.assertFalse(self.client.run_once(self.turn))
                self.assertEqual(self.http.put_calls, [])
        self.turn.assert_not_called()

    def test_configuration_rejects_invalid_keys_and_poll_intervals(self):
        for key in [None, 3, "", "a.b", "a#b", "a$b", "a[b", "a]b", "a/b", "a\nb", "a\x7fb"]:
            for name in ("elder_id", "device_id"):
                with self.subTest(name=name, key=key), self.assertRaises(ValueError):
                    self.new_client(**{name: key})
        for interval in [0, 0.5, 61, float("inf"), float("nan"), "invalid"]:
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                self.new_client(poll_seconds=interval)
        with self.assertRaises(ValueError):
            self.new_client(base_url="")
        for interval in (1, 60, "2.5"):
            with self.subTest(interval=interval):
                self.assertEqual(self.new_client(poll_seconds=interval).poll_seconds, float(interval))

    def test_legal_identity_keys_are_encoded_as_individual_url_segments(self):
        client = self.new_client(elder_id="어르신 ?%", device_id="거실 ?%")
        prefix = "https://example.firebaseio.com/commands/"
        expected = "%EC%96%B4%EB%A5%B4%EC%8B%A0%20%3F%25/%EA%B1%B0%EC%8B%A4%20%3F%25"
        self.assertEqual(client._collection_url(), prefix + expected + ".json")
        self.assertEqual(client._command_url("명령 ?%"), prefix + expected
                         + "/%EB%AA%85%EB%A0%B9%20%3F%25.json")

    def test_malformed_empty_and_unavailable_collection_do_not_start_audio(self):
        responses = [Response(None), Response([]), Response("invalid"), Response({}),
                     Response(status=503), TimeoutError("offline")]
        for response in responses:
            with self.subTest(response=repr(response)):
                http = Mock()
                if isinstance(response, Exception):
                    http.get.side_effect = response
                else:
                    http.get.return_value = response
                self.assertFalse(self.new_client(http=http).run_once(self.turn))
                http.put.assert_not_called()
        self.turn.assert_not_called()

    def test_missing_etag_defers_audio_until_a_later_valid_read(self):
        self.http.omit_etag_gets.add(2)
        self.assertFalse(self.client.run_once(self.turn))
        self.turn.assert_not_called()
        self.assertEqual(self.http.put_calls, [])
        self.assertTrue(self.client.run_once(self.turn))
        self.turn.assert_called_once_with()

    def test_payload_changed_after_poll_is_revalidated_before_claim(self):
        def cancel_before_claim(number):
            if number == 2:
                self.http.replace("command_1", pending(status="cancelled"))

        self.http.before_get = cancel_before_claim
        self.assertFalse(self.client.run_once(self.turn))
        self.turn.assert_not_called()
        self.assertEqual(self.http.put_calls, [])

    def test_conditional_claim_race_never_starts_losing_worker(self):
        self.http.put_actions.append("race")
        self.assertFalse(self.client.run_once(self.turn))
        self.assertFalse(self.client.run_once(self.turn))
        self.turn.assert_not_called()
        self.assertEqual(self.http.records["command_1"]["claimToken"], "other-worker")
        self.assertEqual(len(self.http.put_calls), 1)

    def test_lost_claim_response_executes_only_after_owned_claim_readback(self):
        self.http.put_actions.append("commit_timeout")
        self.assertTrue(self.client.run_once(self.turn))
        self.turn.assert_called_once_with()
        self.assertEqual(self.http.records["command_1"]["status"], "processed")
        self.assertFalse(self.client.run_once(self.turn))
        self.turn.assert_called_once_with()

    def test_lost_claim_and_readback_responses_defer_then_resume_owned_claim(self):
        self.http.put_actions.append("commit_timeout")
        self.http.fail_gets.update({3, 4})
        self.assertFalse(self.client.run_once(self.turn))
        self.assertFalse(self.client.run_once(self.turn))
        self.turn.assert_not_called()
        self.assertEqual(self.http.records["command_1"]["status"], "processing")
        self.assertTrue(self.client.run_once(self.turn))
        self.turn.assert_called_once_with()
        self.assertEqual(len(self.http.put_calls), 2)

    def test_uncommitted_claim_timeout_waits_for_successful_retry(self):
        self.http.put_actions.append("timeout")
        self.assertFalse(self.client.run_once(self.turn))
        self.turn.assert_not_called()
        self.assertEqual(self.http.records["command_1"]["status"], "pending")
        self.assertTrue(self.client.run_once(self.turn))
        self.turn.assert_called_once_with()

    def test_claim_readback_with_other_owner_never_starts_audio(self):
        for field, value in (("acknowledgedBy", "other-device"), ("claimToken", "other-worker")):
            with self.subTest(field=field):
                self.http = FakeFirebase({"command_1": pending()})
                self.client = self.new_client()
                self.http.put_actions.append("commit_timeout")

                def change_owner(number):
                    if number == 3:
                        command = dict(self.http.records["command_1"], **{field: value})
                        self.http.replace("command_1", command)

                self.http.before_get = change_owner
                self.assertFalse(self.client.run_once(self.turn))
                self.assertFalse(self.client.run_once(self.turn))
                self.assertEqual(self.http.records["command_1"][field], value)
        self.turn.assert_not_called()

    def test_completion_retry_blocks_next_command_and_never_reexecutes_audio(self):
        self.http.replace("command_2", pending(200))
        self.http.put_actions.extend(["success", "server_error", "server_error", "success"])
        self.assertTrue(self.client.run_once(self.turn))
        self.assertEqual(self.http.records["command_1"]["status"], "processing")
        self.assertFalse(self.client.run_once(self.turn))
        self.turn.assert_called_once_with()
        self.assertTrue(self.client.run_once(self.turn))
        self.turn.assert_called_once_with()
        self.assertEqual(self.http.records["command_1"]["status"], "processed")
        self.assertEqual(self.http.records["command_2"]["status"], "pending")
        self.assertTrue(self.client.run_once(self.turn))
        self.assertEqual(self.turn.call_count, 2)

    def test_lost_completion_response_is_recognized_without_replaying_audio(self):
        self.http.put_actions.extend(["success", "commit_timeout"])
        self.assertTrue(self.client.run_once(self.turn))
        self.assertEqual(self.http.records["command_1"]["status"], "processed")
        self.assertFalse(self.client.run_once(self.turn))
        self.turn.assert_called_once_with()
        self.assertEqual(len(self.http.put_calls), 2)

    def test_completion_and_readback_lost_then_terminal_result_recognized(self):
        self.http.put_actions.extend(["success", "commit_timeout"])
        self.http.fail_gets.add(4)
        self.assertTrue(self.client.run_once(self.turn))
        self.assertTrue(self.client.run_once(self.turn))
        self.assertFalse(self.client.run_once(self.turn))
        self.turn.assert_called_once_with()
        self.assertEqual(len(self.http.put_calls), 2)

    def test_acknowledgement_does_not_overwrite_replaced_claim(self):
        self.http.put_actions.extend(["success", "server_error"])
        self.assertTrue(self.client.run_once(self.turn))
        replacement = dict(self.http.records["command_1"], claimToken="other-worker")
        self.http.replace("command_1", replacement)
        self.assertTrue(self.client.run_once(self.turn))
        self.assertEqual(self.http.records["command_1"], replacement)
        self.turn.assert_called_once_with()
        self.assertEqual(len(self.http.put_calls), 2)

    def test_restart_ignores_processing_processed_and_failed_records(self):
        self.http = FakeFirebase({status: pending(status=status, claimToken="old-process",
                                                acknowledgedBy="pi_001")
                                  for status in ("processing", "processed", "failed")})
        restarted = self.new_client()
        self.assertFalse(restarted.run_once(self.turn))
        self.turn.assert_not_called()
        self.assertEqual(self.http.put_calls, [])

    def test_process_interrupt_leaves_processing_record_without_replay(self):
        self.turn.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.client.run_once(self.turn)
        self.assertEqual(self.http.records["command_1"]["status"], "processing")
        self.assertFalse(self.client.run_once(self.turn))
        self.assertFalse(self.new_client().run_once(self.turn))
        self.turn.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
