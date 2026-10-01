import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

HUB_DIR = Path(__file__).resolve().parents[1] / "raspberry-pi-voice-hub"
sys.path.insert(0, str(HUB_DIR))

from conversation_store import ConversationStore


class ConversationStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.outbox = Path(self.directory.name) / "outbox"
        self.http = Mock()
        self.http.get.return_value.status_code = 200
        self.http.get.return_value.json.return_value = None
        self.http.patch.return_value.status_code = 200
        self.store = self.new_store()

    def new_store(self, base_url="https://example.firebaseio.com/"):
        return ConversationStore(
            base_url, "elder_001", "pi_001", "거실", self.outbox, http=self.http
        )

    def write(self, message_id="message_1", **overrides):
        values = {
            "session_id": "session_1",
            "session_started_at": 100,
            "message_id": message_id,
            "role": "user",
            "text": "오늘은 뉴스를 봤어",
            "created_at": 120,
            "turn_index": 1,
            "audio_status": "recorded",
        }
        values.update(overrides)
        return self.store.write_message(**values)

    def pending(self):
        return sorted(self.outbox.glob("*.json"))

    def test_one_atomic_patch_links_message_event_session_and_device(self):
        def assert_saved_before_network(*args, **kwargs):
            self.assertEqual(len(self.pending()), 1)
            self.assertEqual(list(self.outbox.glob("*.tmp")), [])
            self.assertEqual(
                json.loads(self.pending()[0].read_text(encoding="utf-8")),
                kwargs["json"],
            )
            return Mock(status_code=200)

        self.http.patch.side_effect = assert_saved_before_network
        self.assertTrue(self.write())
        self.http.patch.assert_called_once()
        self.http.get.assert_called_once_with(
            "https://example.firebaseio.com/events/elder_001/"
            "pi_ai_conversation_message_message_1.json", timeout=10,
        )
        args, kwargs = self.http.patch.call_args
        self.assertEqual(args, ("https://example.firebaseio.com/.json",))
        self.assertEqual(kwargs["timeout"], 10)
        data = kwargs["json"]
        message = data["conversations/elder_001/session_1/messages/message_1"]
        event = data["events/elder_001/pi_ai_conversation_message_message_1"]
        self.assertEqual(message["text"], event["message"])
        self.assertEqual(message["role"], event["role"])
        self.assertEqual(message["messageId"], event["messageId"])
        self.assertEqual(message["audioStatus"], "recorded")
        self.assertEqual(message["turnIndex"], 1)
        self.assertEqual(event["type"], "ai_conversation_message")
        self.assertEqual(event["sessionId"], "session_1")
        self.assertEqual(event["status"], "open")
        self.assertEqual(event["severity"], "info")
        self.assertEqual(event["location"], "거실")
        self.assertEqual(event["runId"], "pi_ai_conversation_run_session_1")
        self.assertEqual(data["conversations/elder_001/session_1/createdAt"], 100)
        self.assertEqual(data["conversations/elder_001/session_1/updatedAt"], 120)
        self.assertEqual(data["devices/pi_001/lastSeen"], {".sv": "timestamp"})
        self.assertEqual(data["devices/pi_001/lastSignal"], {".sv": "timestamp"})
        self.assertEqual(self.pending(), [])

    def test_enqueue_only_does_no_network_io(self):
        self.assertTrue(self.write(flush=False))
        self.http.patch.assert_not_called()
        self.http.get.assert_not_called()
        self.assertEqual(len(self.pending()), 1)
        self.assertTrue(self.store.flush_pending())
        self.assertEqual(self.pending(), [])

    def test_failed_write_survives_restart_and_reuses_identical_paths(self):
        self.http.patch.return_value.status_code = 503
        self.assertFalse(self.write())
        first_payload = self.http.patch.call_args.kwargs["json"]
        self.assertEqual(len(self.pending()), 1)
        self.http.patch.return_value.status_code = 200
        self.assertTrue(self.new_store().flush_pending())
        self.assertEqual(self.http.patch.call_args.kwargs["json"], first_payload)
        self.assertEqual(self.pending(), [])

    def test_lost_write_response_then_guardian_resolve_is_not_replayed(self):
        self.http.patch.side_effect = TimeoutError("write response lost")
        self.assertFalse(self.write())
        saved_payload = self.http.patch.call_args.kwargs["json"]
        accepted_event = dict(
            saved_payload["events/elder_001/pi_ai_conversation_message_message_1"]
        )
        accepted_event.update(status="resolved", resolvedAt=500)
        self.http.get.return_value.json.return_value = accepted_event

        self.assertTrue(self.new_store().flush_pending())
        # A matching event proves the original atomic message/event PATCH was
        # accepted. No second write can reset status, resolvedAt or updatedAt.
        self.http.patch.assert_called_once()
        self.assertEqual(accepted_event["status"], "resolved")
        self.assertEqual(accepted_event["resolvedAt"], 500)
        self.assertEqual(self.pending(), [])

    def test_existing_event_identity_conflicts_are_retained_without_overwrite(self):
        self.assertTrue(self.write(flush=False))
        saved_payload = json.loads(self.pending()[0].read_text(encoding="utf-8"))
        expected = saved_payload["events/elder_001/pi_ai_conversation_message_message_1"]
        for key in (
            "runId", "messageId", "sessionId", "elderId", "deviceId", "role",
            "createdAt", "type",
        ):
            with self.subTest(key=key):
                conflicting = dict(expected)
                conflicting[key] = "different"
                self.http.get.return_value.json.return_value = conflicting
                self.assertFalse(self.store.flush_pending())
                self.assertEqual(len(self.pending()), 1)
        self.http.patch.assert_not_called()

    def test_failed_delivery_check_retains_queue_without_patch(self):
        self.assertTrue(self.write("first", flush=False))
        self.assertTrue(self.write("second", flush=False))
        self.http.get.return_value.status_code = 503
        self.assertFalse(self.store.flush_pending())
        self.http.get.assert_called_once()
        self.http.patch.assert_not_called()
        self.assertEqual(len(self.pending()), 2)

    def test_malformed_or_failed_delivery_check_preserves_message(self):
        self.assertTrue(self.write(flush=False))
        for malformed in ([], "invalid", 0, False, {}):
            with self.subTest(malformed=malformed):
                self.http.get.return_value.json.return_value = malformed
                self.assertFalse(self.store.flush_pending())
        self.http.get.return_value.json.side_effect = ValueError("invalid JSON")
        self.assertFalse(self.store.flush_pending())
        self.http.get.side_effect = TimeoutError("read response lost")
        self.assertFalse(self.store.flush_pending())
        self.assertEqual(len(self.pending()), 1)
        self.http.patch.assert_not_called()

    def test_offline_retry_keeps_message_time_but_refreshes_device_health(self):
        self.http.patch.return_value.status_code = 503
        self.assertFalse(self.write(created_at=120))
        self.http.patch.return_value.status_code = 200
        self.assertTrue(self.new_store().flush_pending())

        # Both attempts ask Firebase to resolve device activity at delivery,
        # so delayed recovery never reuses the old transcript timestamp.
        for call in self.http.patch.call_args_list:
            data = call.kwargs["json"]
            self.assertEqual(
                data["conversations/elder_001/session_1/messages/message_1"]["createdAt"],
                120,
            )
            self.assertEqual(
                data["events/elder_001/pi_ai_conversation_message_message_1"]["createdAt"],
                120,
            )
            self.assertEqual(data["devices/pi_001/lastSeen"], {".sv": "timestamp"})
            self.assertEqual(data["devices/pi_001/lastSignal"], {".sv": "timestamp"})

    def test_stops_after_first_failure_without_sending_later_messages(self):
        self.write("first", flush=False)
        self.write("second", flush=False)
        self.http.patch.return_value.status_code = 500
        self.assertFalse(self.store.flush_pending())
        self.http.patch.assert_called_once()
        self.assertEqual(len(self.pending()), 2)
        self.assertIn(
            "conversations/elder_001/session_1/messages/first",
            self.http.patch.call_args.kwargs["json"],
        )

    def test_fifo_survives_clock_rollback_and_restart(self):
        with patch("conversation_store.time.time_ns", return_value=1000):
            self.assertTrue(self.write("first", flush=False))
        self.store = self.new_store()
        with patch("conversation_store.time.time_ns", return_value=10):
            self.assertTrue(self.write("second", flush=False))
            self.assertTrue(self.write("third", flush=False))
        self.assertTrue(self.store.flush_pending())
        delivered = [
            next(value["messageId"] for key, value in call.kwargs["json"].items()
                 if key.startswith("events/"))
            for call in self.http.patch.call_args_list
        ]
        self.assertEqual(delivered, ["first", "second", "third"])

    def test_flush_has_finite_batch_and_reports_remaining_messages(self):
        for number in range(7):
            self.assertTrue(self.write(f"message_{number}", flush=False))
        self.assertFalse(self.store.flush_pending())
        self.assertEqual(self.http.patch.call_count, 5)
        self.assertEqual(self.http.get.call_count, 5)
        self.assertEqual(len(self.pending()), 2)
        self.assertTrue(self.store.flush_pending())
        self.assertEqual(self.http.patch.call_count, 7)

    def test_no_firebase_url_preserves_pending_messages(self):
        self.store = self.new_store(base_url="")
        self.assertFalse(self.write())
        self.http.patch.assert_not_called()
        self.assertEqual(len(self.pending()), 1)

    def test_empty_outbox_is_successful_without_network(self):
        self.assertTrue(self.store.flush_pending())
        self.http.patch.assert_not_called()

    def test_network_exception_is_contained_and_sensitive_text_is_not_logged(self):
        self.http.patch.side_effect = RuntimeError("secret-token private-transcript")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertFalse(self.write(text="private-transcript"))
        self.assertNotIn("secret-token", output.getvalue())
        self.assertNotIn("private-transcript", output.getvalue())
        self.assertEqual(len(self.pending()), 1)

    def test_credentials_and_database_url_are_not_saved(self):
        self.store = self.new_store(base_url="https://secret-token@example.com")
        self.assertTrue(self.write(flush=False))
        serialized = self.pending()[0].read_text(encoding="utf-8")
        self.assertNotIn("secret-token", serialized)
        self.assertNotIn("example.com", serialized)

    def test_storage_failure_is_contained_without_sending_unsaved_data(self):
        with patch("conversation_store.os.replace", side_effect=OSError("full disk")):
            self.assertFalse(self.write())
        self.http.patch.assert_not_called()
        self.assertEqual(self.pending(), [])
        self.assertEqual(list(self.outbox.glob("*.tmp")), [])

    def test_corrupt_pending_file_is_retained(self):
        self.assertTrue(self.write(flush=False))
        self.pending()[0].write_text("{invalid json", encoding="utf-8")
        self.assertFalse(self.store.flush_pending())
        self.http.patch.assert_not_called()
        self.assertEqual(len(self.pending()), 1)

    def test_failed_removal_verifies_delivery_without_replaying_payload(self):
        self.assertTrue(self.write(flush=False))
        with patch.object(Path, "unlink", side_effect=OSError("busy")):
            self.assertFalse(self.store.flush_pending())
        first_payload = self.http.patch.call_args.kwargs["json"]
        self.assertEqual(len(self.pending()), 1)
        self.http.get.return_value.json.return_value = first_payload[
            "events/elder_001/pi_ai_conversation_message_message_1"
        ]
        self.assertTrue(self.store.flush_pending())
        self.http.patch.assert_called_once()
        self.assertEqual(self.pending(), [])

    def test_invalid_firebase_key_does_not_write_or_escape_message_path(self):
        for identifier in ("../escape", "bad.key", "bad#key", "bad\nkey", ""):
            with self.subTest(identifier=identifier):
                self.assertFalse(self.write(identifier))
        self.http.patch.assert_not_called()
        self.assertEqual(self.pending(), [])

    def test_enqueue_remains_available_while_flush_request_is_running(self):
        self.assertTrue(self.write("first", flush=False))
        entered = threading.Event()
        release = threading.Event()
        result = []

        def slow_request(*args, **kwargs):
            entered.set()
            if not release.wait(timeout=3):
                raise TimeoutError("test flush was not released")
            return Mock(status_code=200)

        self.http.patch.side_effect = slow_request
        worker = threading.Thread(target=lambda: result.append(self.store.flush_pending()))
        worker.start()
        try:
            self.assertTrue(entered.wait(timeout=2))
            self.assertFalse(self.store.flush_pending())
            self.assertTrue(self.write("second", flush=False))
            self.assertEqual(len(self.pending()), 2)
        finally:
            release.set()
            worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [False])
        self.assertEqual(len(self.pending()), 1)
        self.assertTrue(self.store.flush_pending())


if __name__ == "__main__":
    unittest.main()
