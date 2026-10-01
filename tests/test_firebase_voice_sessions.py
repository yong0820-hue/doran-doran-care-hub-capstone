"""Real bounded voice sessions inside the Firebase claim/acknowledgement boundary."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


HUB_DIR = Path(__file__).resolve().parents[1] / "raspberry-pi-voice-hub"
sys.path.insert(0, str(HUB_DIR))

from firebase_commands import FirebaseCommandClient
from test_firebase_commands import FakeFirebase, pending
from voice_ai import LISTEN_PROMPT, VoiceConversation, VoiceSettings


class FirebaseVoiceSessionTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.quiet = redirect_stdout(self.output)
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.http = FakeFirebase({"command_1": pending()})
        self.commands = FirebaseCommandClient(
            "https://example.invalid", "elder_001", "pi_001", http=self.http
        )
        self.client = Mock()
        self.store = Mock()
        self.store.write_message.return_value = True
        self.lock = threading.Lock()
        self.voice = VoiceConversation(
            self.client, VoiceSettings(), self.store, self.lock, self.temporary.name
        )
        self.transcripts = iter(["오늘 공원 다녀왔어요", "친구를 만났어요", "그만"])
        self.response_number = 0
        self.observed_stages = []

        def transcribe(**kwargs):
            self.assert_processing("stt")
            self.assertFalse(self.lock.locked())
            return SimpleNamespace(text=next(self.transcripts))

        def respond(**kwargs):
            self.assert_processing("llm")
            self.assertFalse(self.lock.locked())
            self.response_number += 1
            return SimpleNamespace(
                output_text=f"즐거운 이야기네요. {self.response_number}",
                status="completed",
            )

        def stream_to_file(path):
            self.assert_processing("tts")
            self.assertFalse(self.lock.locked())
            path.write_bytes(b"mock mp3")

        def capture(path):
            self.assert_processing("capture")
            self.assertTrue(self.lock.locked())
            path.write_bytes(b"mock microphone data")
            return True

        def playback(command, timeout):
            self.assert_processing("playback")
            self.assertTrue(self.lock.locked())
            self.assertTrue(Path(command[-1]).is_file())

        self.client.audio.transcriptions.create.side_effect = transcribe
        self.client.responses.create.side_effect = respond
        speech = Mock()
        speech.stream_to_file.side_effect = stream_to_file
        self.client.audio.speech.with_streaming_response.create.return_value = Mock(
            __enter__=Mock(return_value=speech),
            __exit__=Mock(return_value=False),
        )
        for name, options in (
            ("capture_utterance", {"side_effect": capture}),
            ("has_meaningful_speech", {"return_value": True}),
            ("run_audio", {"side_effect": playback}),
        ):
            patcher = patch.object(self.voice, name, **options)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        self.session = Mock(wraps=self.voice.run_session)

    def assert_processing(self, stage):
        self.observed_stages.append(stage)
        self.assertEqual(self.http.records["command_1"]["status"], "processing")
        # No internal turn may finalize or reclaim this dashboard command.
        self.assertEqual(len(self.http.put_calls), 1)

    def assert_terminal_once(self, status, outcome):
        command = self.http.records["command_1"]
        self.assertEqual((command["status"], command["outcome"]), (status, outcome))
        self.assertEqual(
            [call[1]["status"] for call in self.http.put_calls],
            ["processing", status],
        )
        self.session.assert_called_once_with()
        self.assertFalse(self.commands.run_once(self.session))
        self.session.assert_called_once_with()
        self.assertEqual(len(self.http.put_calls), 2)
        self.assertFalse(self.lock.locked())
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])

    def test_multiple_replies_share_one_claim_history_and_finalization(self):
        self.assertTrue(self.commands.run_once(self.session))
        self.assert_terminal_once("processed", "completed")
        self.assertEqual(self.capture_utterance.call_count, 3)
        self.assertEqual(self.client.audio.transcriptions.create.call_count, 3)
        self.assertEqual(self.client.responses.create.call_count, 2)
        calls = self.client.responses.create.call_args_list
        self.assertEqual(calls[1].kwargs["input"], [
            {"role": "user", "content": "오늘 공원 다녀왔어요"},
            {"role": "assistant", "content": "즐거운 이야기네요. 1"},
            {"role": "user", "content": "친구를 만났어요"},
        ])
        records = [call.kwargs for call in self.store.write_message.call_args_list]
        self.assertEqual(len(records), 4)
        self.assertEqual(len({record["session_id"] for record in records}), 1)
        self.assertEqual([record["turn_index"] for record in records], [1, 1, 2, 2])
        self.assertNotIn("그만", [record["text"] for record in records])
        spoken = [
            call.kwargs["input"]
            for call in self.client.audio.speech.with_streaming_response.create.call_args_list
        ]
        self.assertEqual(spoken.count(LISTEN_PROMPT), 1)
        self.assertIn("reason=stop_phrase", self.output.getvalue())

    def test_stop_before_first_reply_is_processed_once_without_llm(self):
        self.transcripts = iter(["이제 됐어요."])
        self.assertTrue(self.commands.run_once(self.session))
        self.assert_terminal_once("processed", "completed")
        self.assertEqual(self.capture_utterance.call_count, 1)
        self.client.responses.create.assert_not_called()
        self.store.write_message.assert_not_called()
        self.assertEqual(self.voice.history, [])

    def test_silence_only_session_is_processed_once_as_no_speech(self):
        def silence(path):
            self.assert_processing("silent_capture")
            self.assertTrue(self.lock.locked())
            return False

        self.capture_utterance.side_effect = silence
        self.assertTrue(self.commands.run_once(self.session))
        self.assert_terminal_once("processed", "no_speech")
        self.assertEqual(self.capture_utterance.call_count, 2)
        self.client.audio.transcriptions.create.assert_not_called()
        self.client.responses.create.assert_not_called()
        self.store.write_message.assert_not_called()
        self.assertIn("reason=no_speech", self.output.getvalue())

    def test_failed_acknowledgement_retries_without_replaying_any_session_turn(self):
        self.http.replace("command_2", pending(200))
        self.http.put_actions.extend(["success", "server_error", "success"])
        self.assertTrue(self.commands.run_once(self.session))
        self.assertEqual(self.http.records["command_1"]["status"], "processing")
        self.assertEqual(self.http.records["command_2"]["status"], "pending")
        captures = self.capture_utterance.call_count
        replies = self.client.responses.create.call_count
        self.assertEqual((captures, replies), (3, 2))

        self.assertTrue(self.commands.run_once(self.session))
        self.session.assert_called_once_with()
        self.assertEqual(self.capture_utterance.call_count, captures)
        self.assertEqual(self.client.responses.create.call_count, replies)
        self.assertEqual(self.http.records["command_1"]["status"], "processed")
        self.assertEqual(self.http.records["command_2"]["status"], "pending")
        self.assertEqual(
            [call[1]["status"] for call in self.http.put_calls],
            ["processing", "processed", "processed"],
        )

    def test_error_after_successful_reply_fails_entire_session_once(self):
        self.client.responses.create.side_effect = [
            SimpleNamespace(output_text="공원에 다녀오셨군요.", status="completed"),
            RuntimeError("private-error-details"),
        ]
        self.assertTrue(self.commands.run_once(self.session))
        self.assert_terminal_once("failed", "failed")
        self.assertEqual(self.capture_utterance.call_count, 2)
        self.assertEqual(self.client.responses.create.call_count, 2)
        self.assertEqual(self.http.records["command_1"]["errorType"], "RuntimeError")
        self.assertEqual(len(self.voice.history), 2)
        self.assertEqual(self.store.write_message.call_count, 3)
        self.assertIn("reason=error", self.output.getvalue())
        self.assertNotIn("private-error-details", self.output.getvalue())
        self.assertNotIn("private-error-details", str(self.http.records))


if __name__ == "__main__":
    unittest.main()
