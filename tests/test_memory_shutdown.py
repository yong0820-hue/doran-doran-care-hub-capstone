"""Normal shutdown drains extraction or leaves a durable, replayable job."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import test_voice_sessions
from test_memory_voice_integration import FakeMemoryRequest
from persistent_memory import PersistentMemoryService
from voice_ai import ConversationController


class MemoryShutdownTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.store = Mock(elder_id="elder-test")
        self.store.read_generation.return_value = 0
        self.store.save.return_value = True
        self.extractor = Mock()
        self.extractor.extract.return_value = [{"content": "산책을 좋아해요"}]
        self.messages = [{"text": "산책을 좋아해요", "createdAt": 1}]
        self.output = io.StringIO()
        capture = contextlib.redirect_stdout(self.output)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)

    def service(self):
        service = PersistentMemoryService(self.store, self.extractor, outbox_dir=self.path)
        self.addCleanup(lambda: service.stop(drain=True, timeout=1))
        return service

    def jobs(self):
        return list(self.path.rglob("*.json"))

    def test_default_stop_finishes_already_queued_job_without_explicit_worker_start(self):
        service = self.service()
        self.assertTrue(service.submit("session", self.messages, 0))
        self.assertTrue(service.stop())
        self.store.save.assert_called_once()
        self.assertFalse(service._thread.is_alive())
        self.assertEqual(self.jobs(), [])

    def test_normal_shutdown_joins_inflight_extraction_before_reporting_complete(self):
        service = self.service()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def extract(messages):
            entered.set()
            release.wait(2)
            return [{"content": "산책을 좋아해요"}]

        self.extractor.extract.side_effect = extract
        service.start()
        self.assertTrue(service.submit("session", self.messages, 0))
        self.assertTrue(entered.wait(1))
        completed = threading.Event()
        result = []
        stopper = threading.Thread(target=lambda: (result.append(service.stop(drain=True, timeout=1)), completed.set()))
        stopper.start()
        self.assertFalse(completed.wait(0.03))
        release.set()
        stopper.join(2)
        self.assertEqual(result, [True])
        self.assertFalse(service._thread.is_alive())
        self.store.save.assert_called_once()
        self.assertEqual(self.jobs(), [])

    def test_shutdown_timeout_is_bounded_and_pending_job_replays_after_restart(self):
        service = self.service()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def extract(messages):
            entered.set()
            release.wait(2)
            raise TimeoutError("private transcript")

        self.extractor.extract.side_effect = extract
        service.start()
        self.assertTrue(service.submit("session", self.messages, 0))
        self.assertTrue(entered.wait(1))
        before = time.monotonic()
        self.assertFalse(service.stop(drain=True, timeout=0.02))
        self.assertLess(time.monotonic() - before, 0.3)
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(json.loads(self.jobs()[0].read_text(encoding="utf-8"))["messages"], self.messages)
        self.assertIn("recover", self.output.getvalue().lower())
        self.assertNotIn("private transcript", self.output.getvalue())
        release.set()
        service._thread.join(2)
        self.extractor.extract.side_effect = None
        restarted = self.service()
        restarted.start()
        self.assertTrue(restarted.stop(drain=True, timeout=1))
        self.store.save.assert_called_once()
        self.assertEqual(self.jobs(), [])

    def test_failed_write_retains_job_and_reset_cancels_old_generation_replay(self):
        service = self.service()
        self.store.save.return_value = False
        self.assertTrue(service.submit("session", self.messages, 0))
        service.start()
        self.assertFalse(service.stop(drain=True, timeout=1))
        self.assertEqual(len(self.jobs()), 1)
        self.extractor.extract.reset_mock()
        self.store.read_generation.return_value = 1
        restarted = self.service()
        restarted.start()
        self.assertTrue(restarted.stop(drain=True, timeout=1))
        self.extractor.extract.assert_not_called()
        self.assertEqual(self.jobs(), [])

    def test_late_voice_submission_after_stop_remains_recoverable(self):
        service = self.service()
        self.assertTrue(service.stop(drain=True, timeout=0.1))
        self.assertTrue(service.submit("late-session", self.messages, 0))
        self.assertEqual(len(self.jobs()), 1)
        self.extractor.extract.assert_not_called()

    def test_durable_queue_overflow_is_drained_without_silent_drop(self):
        service = self.service()
        for index in range(12):
            self.assertTrue(service.submit(f"session-{index}", self.messages, 0))
        self.assertTrue(service.stop(drain=True, timeout=2))
        self.assertEqual(self.store.save.call_count, 12)
        self.assertEqual(self.jobs(), [])

    def test_outbox_refuses_new_jobs_at_capacity_without_deleting_accepted_jobs(self):
        service = self.service()
        for index in range(128):
            self.assertTrue(service.submit(f"pending-{index}", self.messages, 0))
        self.assertFalse(service.submit("over-capacity", self.messages, 0))
        self.assertEqual(len(self.jobs()), 128)
        self.assertIn("capacity", self.output.getvalue().lower())

    def test_local_persistence_failure_is_explicitly_rejected(self):
        service = self.service()
        with patch("persistent_memory.os.replace", side_effect=OSError("private path")):
            self.assertFalse(service.submit("session", self.messages, 0))
        self.assertEqual(service._queue.qsize(), 0)
        self.assertEqual(self.jobs(), [])
        self.assertNotIn("private path", self.output.getvalue())


class MemoryControllerShutdownTests(unittest.TestCase):
    def setUp(self):
        test_voice_sessions.VoiceSessionTests.setUp(self)
        self.memory = Mock()
        self.memory.begin_session.return_value = 0
        self.memory.session_status.return_value = {"state": "READY", "generation": 0}
        self.memory.request_context.side_effect = lambda *_args: FakeMemoryRequest()
        self.voice.memory_service = self.memory

    def test_terminal_q_explicitly_drains_before_finished(self):
        controller = ConversationController(self.voice)

        def stop(*, drain, timeout):
            self.assertTrue(drain)
            self.assertGreater(timeout, 0)
            self.assertFalse(controller.finished.is_set())
            return True

        self.memory.stop.side_effect = stop
        with patch("builtins.input", side_effect=["", "q"]):
            controller._run()
        self.memory.submit.assert_called_once()
        self.memory.stop.assert_called_once()
        self.assertTrue(controller.finished.is_set())

    def test_shutdown_checkpoints_recognized_facts_while_voice_worker_is_blocked(self):
        controller = ConversationController(self.voice)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.voice._memory_generation = 0
        self.voice._memory_messages = [{"text": "산책을 좋아해요", "createdAt": 1}]

        def blocked_response():
            with self.voice.interaction_lock:
                entered.set()
                release.wait(2)

        controller._voice_thread = threading.Thread(target=blocked_response, daemon=True)
        controller._voice_thread.start()
        self.assertTrue(entered.wait(1))
        before = time.monotonic()
        controller.stop(timeout=0.05)
        self.assertLess(time.monotonic() - before, 0.3)
        self.memory.submit.assert_called_once()
        self.assertTrue(self.memory.stop.call_args.kwargs["drain"])
        self.assertEqual(self.voice._memory_messages, [])
        release.set()
        controller._voice_thread.join(1)


if __name__ == "__main__":
    unittest.main()
