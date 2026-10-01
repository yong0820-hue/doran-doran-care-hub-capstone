"""Firebase voice-controller integration without terminals, devices, or network."""

import io
import os
from contextlib import redirect_stdout
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "raspberry-pi-voice-hub"))
from voice_ai import ConversationController, VoiceSettings, create_firebase_controller


class FirebaseControllerTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.quiet = redirect_stdout(self.output)
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)
        self.conversation = Mock()
        self.conversation.stop_event = threading.Event()
        self.conversation.settings = VoiceSettings()
        self.command_client = Mock(path="commands/elder/device", poll_seconds=5)

    def test_factory_works_without_terminal_and_keeps_wake_word_disabled(self):
        for wake_setting in (None, "false"):
            with self.subTest(wake_setting=wake_setting):
                env = {} if wake_setting is None else {"AI_WAKE_WORD_ENABLED": wake_setting}
                fake_http = Mock()
                wake_factory = Mock()
                wake_module = SimpleNamespace(create_wake_word_detector=wake_factory)
                audio_lock = threading.Lock()
                with patch.dict(os.environ, env, clear=True), \
                     patch.dict(sys.modules, {"requests": fake_http, "wake_word": wake_module}), \
                     patch("voice_ai.sys.stdin", None), \
                     patch("voice_ai._create_conversation", return_value=self.conversation) as create:
                    controller = create_firebase_controller(
                        "https://example.invalid", "elder", "device", "room", audio_lock, "audio"
                    )
                create.assert_called_once_with(
                    "https://example.invalid", "elder", "device", "room", audio_lock, "audio"
                )
                self.assertIs(controller.conversation, self.conversation)
                self.assertIsNone(controller.wake_detector)
                self.assertEqual(controller.command_client.path, "commands/elder/device")
                self.assertEqual(controller.command_client.poll_seconds, 5)
                wake_factory.assert_not_called()
                fake_http.get.assert_not_called()
                self.assertFalse(controller._voice_thread.is_alive())

    def test_factory_rejects_invalid_command_poll_before_creating_conversation(self):
        for value in ("not-a-number", "nan", "inf", "0", "61"):
            with self.subTest(value=value), \
                 patch.dict(os.environ, {"AI_COMMAND_POLL_SECONDS": value}, clear=True), \
                 patch("voice_ai._create_conversation") as create:
                with self.assertRaisesRegex(ValueError, "AI_COMMAND_POLL_SECONDS"):
                    create_firebase_controller(
                        "https://example.invalid", "elder", "device", "room", threading.Lock(), "audio"
                    )
                create.assert_not_called()

    def test_firebase_worker_passes_voice_session_to_command_client_without_terminal_input(self):
        controller = ConversationController(self.conversation, self.command_client)

        def process_command(run_turn):
            self.assertEqual(run_turn, self.conversation.run_session)
            run_turn()
            self.conversation.stop_event.set()
            return True

        self.command_client.run_once.side_effect = process_command
        with patch("builtins.input", side_effect=AssertionError("Terminal input is not available")):
            controller._run_firebase()
        self.command_client.run_once.assert_called_once()
        self.conversation.run_session.assert_called_once()
        self.conversation.run_turn.assert_not_called()
        self.assertTrue(controller.finished.is_set())

    def test_firebase_worker_retries_poll_failure_and_sanitizes_error(self):
        controller = ConversationController(self.conversation, self.command_client)
        now = [0.0]

        def wait(seconds):
            now[0] += seconds
            return self.conversation.stop_event.is_set()

        def finish(run_turn):
            run_turn()
            self.conversation.stop_event.set()
            return True

        outcomes = iter([TimeoutError("private-request-details"), None])

        def poll(run_turn):
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return finish(run_turn)

        self.command_client.run_once.side_effect = poll
        with patch("voice_ai.time.monotonic", side_effect=lambda: now[0]), \
             patch.object(self.conversation.stop_event, "wait", side_effect=wait) as delay:
            controller._run_firebase()
        self.assertEqual(self.command_client.run_once.call_count, 2)
        delay.assert_called_once_with(5.0)
        self.conversation.run_session.assert_called_once()
        self.conversation.run_turn.assert_not_called()
        self.assertTrue(controller.finished.is_set())
        self.assertIn("TimeoutError", self.output.getvalue())
        self.assertNotIn("private-request-details", self.output.getvalue())

    def test_firebase_poll_wait_is_interruptible_on_shutdown(self):
        controller = ConversationController(self.conversation, self.command_client)
        self.command_client.run_once.return_value = False

        def stop_while_waiting(seconds):
            self.assertGreater(seconds, 0)
            self.assertLessEqual(seconds, self.command_client.poll_seconds)
            self.conversation.stop_event.set()
            return True

        with patch.object(self.conversation.stop_event, "wait", side_effect=stop_while_waiting):
            controller._run_firebase()
        self.command_client.run_once.assert_called_once()
        self.conversation.run_session.assert_not_called()
        self.assertTrue(controller.finished.is_set())

    def test_controller_start_and_stop_join_voice_and_log_workers(self):
        polled = threading.Event()
        flushed = threading.Event()
        self.command_client.run_once.side_effect = lambda run_turn: polled.set()
        self.conversation.store.flush_pending.side_effect = flushed.set
        controller = ConversationController(self.conversation, self.command_client)
        controller.start()
        try:
            self.assertTrue(polled.wait(timeout=1), "Firebase worker did not start")
            self.assertTrue(flushed.wait(timeout=1), "Conversation log worker did not start")
        finally:
            controller.stop()
        self.assertTrue(self.conversation.stop_event.is_set())
        self.assertTrue(controller.finished.is_set())
        self.assertFalse(controller._voice_thread.is_alive())
        self.assertFalse(controller._log_thread.is_alive())


if __name__ == "__main__":
    unittest.main()
