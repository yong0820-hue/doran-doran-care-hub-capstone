"""Offline care reply, audio lifecycle, and conversation isolation checks."""

import io
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import wave
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "raspberry-pi-voice-hub"))
from inactivity_care import CHECK_IN_PROMPT, classify_inactivity_reply
from voice_ai import (
    ConversationStopped, VoiceConversation, VoiceSettings, create_inactivity_controller,
)


class InactivityReplyTests(unittest.TestCase):
    def test_explicit_normal_replies_accept_spacing_and_politeness(self):
        for text in (
            "괜찮아", "괜찮아요.", "괜찮습니다", "네, 저는 괜찮아요!",
            "응 난 괜찮아", "문제 없어요", "별일 없습니다", "네. 괜찮아요.",
        ):
            with self.subTest(text=text):
                self.assertEqual(classify_inactivity_reply(text), "okay")

    def test_terminal_punctuation_does_not_change_explicit_reply(self):
        for text, expected in (
            ("괜찮아요?", "okay"),
            ("괜찮아요.", "okay"),
            ("괜찮아요!", "okay"),
            ("도와주세요?", "help"),
        ):
            with self.subTest(text=text):
                self.assertEqual(classify_inactivity_reply(text), expected)

    def test_explicit_help_and_emergency_phrases(self):
        for text in (
            "도와줘", "도와주세요", "도란아 도와줘요", "제발 살려주세요!",
            "도움이 필요해요", "숨이 안 쉬어져요", "숨쉬기가 힘들어요",
            "가슴이 너무 아파요", "넘어졌어요", "못 일어나겠어요",
            "119 불러주세요", "구급차를 불러줘", "도와주세요?",
        ):
            with self.subTest(text=text):
                self.assertEqual(classify_inactivity_reply(text), "help")

    def test_help_has_priority_over_normal_acknowledgment_or_other_clauses(self):
        for text in (
            "괜찮아요, 도와주세요", "도와줘요. 괜찮아요.",
            "네 도와주세요", "네, 도움 필요해요", "잘 모르겠어. 119 불러줘.",
        ):
            with self.subTest(text=text):
                self.assertEqual(classify_inactivity_reply(text), "help")

    def test_negations_and_uncertain_answers_do_not_close_alert(self):
        for text in (
            "안 괜찮아요", "괜찮지 않아요", "문제 없지는 않아요",
            "도와주지 마세요", "도움이 필요하지 않아요", "괜찮은 것 같아요",
            "아마 괜찮아요", "괜찮아요. 그런데 어지러워요.", "네", "응", "그만",
        ):
            with self.subTest(text=text):
                self.assertEqual(classify_inactivity_reply(text), "unclear")

    def test_questions_quoted_and_hypothetical_speech_remain_unclear(self):
        for text in (
            "괜찮은가요?", "괜찮을까요?", "도와줄 수 있어요?",
            "도움이 필요하면 도와주세요", "만약 넘어지면 도와줘",
            "친구가 괜찮아요라고 말했어요", '"괜찮아요"',
            '"괜찮아요, 도와주세요"', "‘도와줘’라는 말은 무슨 뜻이야?",
            "넘어졌어요라는 말을 연습했어요",
        ):
            with self.subTest(text=text):
                self.assertEqual(classify_inactivity_reply(text), "unclear")

    def test_empty_text_is_no_speech_and_punctuation_is_unclear(self):
        for text in ("", " ", "\n\t"):
            with self.subTest(text=text):
                self.assertEqual(classify_inactivity_reply(text), "no_speech")
        self.assertEqual(classify_inactivity_reply("..."), "unclear")


class InactivityVoiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = io.StringIO()
        quiet = redirect_stdout(self.output)
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.order = []
        self.lock = threading.Lock()
        self.client = Mock()
        self.client.responses.create.return_value = Mock(
            output_text="공원에서 무엇을 보셨어요?", status="completed",
        )
        self.client.audio.transcriptions.create.side_effect = self.transcribe
        self.transcript = "괜찮아요"
        self.speech = Mock()
        self.speech.stream_to_file.side_effect = self.synthesize
        self.client.audio.speech.with_streaming_response.create.return_value = Mock(
            __enter__=Mock(return_value=self.speech), __exit__=Mock(return_value=False),
        )
        self.store = Mock()
        self.store.write_message.return_value = True
        self.voice = VoiceConversation(
            self.client, VoiceSettings(), self.store, self.lock, self.temporary.name,
        )
        self.vad = Mock()
        self.vad.is_speech.return_value = True
        vad_module = Mock()
        vad_module.Vad.return_value = self.vad
        replacement = patch.dict(sys.modules, {"webrtcvad": vad_module})
        replacement.start()
        self.addCleanup(replacement.stop)
        self.recording = b"\xb0\x04\x50\xfb" * 4000
        for name, implementation in (
            ("capture_utterance", self.capture_audio), ("run_audio", self.play_audio),
        ):
            replacement = patch.object(self.voice, name, side_effect=implementation)
            setattr(self, name, replacement.start())
            self.addCleanup(replacement.stop)

    def synthesize(self, path):
        self.order.append("tts")
        path.write_bytes(b"mock mp3")

    def capture_audio(self, path):
        self.order.append("capture")
        self.assertTrue(self.lock.locked())
        self.voice.check_stopped()
        with wave.open(str(path), "wb") as recording:
            recording.setnchannels(1)
            recording.setsampwidth(2)
            recording.setframerate(16000)
            recording.writeframes(self.recording)
        return True

    def play_audio(self, command, timeout):
        self.order.append("playback")
        self.assertTrue(self.lock.locked())
        self.voice.check_stopped()
        self.assertEqual(command[:6], ["mpg123", "-o", "alsa", "-a",
                                       self.voice.settings.playback_device, "-q"])
        self.assertTrue(Path(command[-1]).is_file())

    def transcribe(self, **kwargs):
        self.order.append("stt")
        self.assertFalse(self.lock.locked())
        self.assertGreater(self.vad.is_speech.call_count, 0)
        self.assertEqual(kwargs["language"], "ko")
        self.assertEqual(kwargs["model"], self.voice.settings.stt_model)
        self.assertTrue(kwargs["file"].read())
        return Mock(text=self.transcript)

    def assert_audio_clean(self):
        self.assertFalse(self.lock.locked())
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])

    def assert_no_user_or_llm(self):
        self.client.responses.create.assert_not_called()
        self.store.write_message.assert_not_called()
        self.assertEqual(self.voice.history, [])

    def test_check_in_uses_existing_speech_validation_stt_and_tts_settings(self):
        self.assertEqual(self.voice.run_inactivity_check("event_1"), "okay")
        self.assertEqual(self.order, ["tts", "playback", "capture", "stt"])
        speech = self.client.audio.speech.with_streaming_response.create
        speech.assert_called_once()
        self.assertEqual(speech.call_args.kwargs["input"], CHECK_IN_PROMPT)
        self.assertEqual(speech.call_args.kwargs["voice"], "marin")
        self.assertEqual(speech.call_args.kwargs["speed"], 1.15)
        self.assertEqual(self.voice.settings.max_utterance_seconds, 20)
        self.client.responses.create.assert_not_called()
        self.assert_audio_clean()

    def test_contextual_prompt_after_conversation_keeps_one_capture_and_classifier(self):
        contextual = (
            "그런데 어르신, 한동안 움직임이 없으셨는데 몸은 괜찮으세요?"
        )
        self.client.responses.create.return_value = Mock(
            output_text=contextual, status="completed",
        )
        with self.voice.use_interaction(kind="conversation"):
            pass

        self.assertEqual(self.voice.run_inactivity_check("event_1"), "okay")
        self.assertEqual(self.order, ["tts", "playback", "capture", "stt"])
        self.assertEqual(self.capture_utterance.call_count, 1)
        self.assertEqual(
            self.client.audio.speech.with_streaming_response.create.call_args.kwargs["input"],
            contextual,
        )
        self.client.responses.create.assert_called_once()
        self.store.write_message.assert_called_once()
        self.assertEqual(self.voice.history, [])
        self.assert_audio_clean()

    def test_question_and_capture_keep_one_unbroken_physical_audio_lock(self):
        raw_lock = self.lock
        lock_events = []

        def acquire(**kwargs):
            lock_events.append("acquire")
            return raw_lock.acquire(**kwargs)

        def release():
            lock_events.append("release")
            raw_lock.release()

        self.voice.audio_lock = Mock(acquire=acquire, release=release)
        self.assertEqual(self.voice.run_inactivity_check("event_1"), "okay")
        self.assertEqual(lock_events, ["acquire", "release"])
        self.assert_audio_clean()

    def test_recognized_replies_use_existing_outbox_without_chat_history(self):
        for transcript, expected in (
            ("괜찮아요", "okay"), ("도와주세요", "help"), ("잘 모르겠어요", "unclear"),
        ):
            with self.subTest(transcript=transcript):
                self.store.reset_mock()
                self.transcript = transcript
                self.assertEqual(self.voice.run_inactivity_check("event_1"), expected)
                self.store.write_message.assert_called_once()
                record = self.store.write_message.call_args.kwargs
                self.assertEqual(record["role"], "user")
                self.assertEqual(record["text"], transcript)
                self.assertEqual(record["audio_status"], "recorded")
                self.assertEqual(record["turn_index"], 1)
                self.assertEqual(record["session_id"], self.voice.session_id)
                self.assertIs(record["flush"], False)
                self.assertEqual(self.voice.history, [])
                self.assert_audio_clean()
        self.client.responses.create.assert_not_called()

    def test_no_utterance_skips_validation_stt_and_outbox(self):
        self.capture_utterance.side_effect = lambda path: False
        with patch.object(self.voice, "has_meaningful_speech") as validate:
            self.assertEqual(self.voice.run_inactivity_check("event_1"), "no_speech")
            validate.assert_not_called()
        self.client.audio.transcriptions.create.assert_not_called()
        self.assert_no_user_or_llm()
        self.assert_audio_clean()

    def test_silent_recording_is_rejected_before_stt(self):
        self.recording = b"\x00\x00" * 8000
        self.assertEqual(self.voice.run_inactivity_check("event_1"), "no_speech")
        self.client.audio.transcriptions.create.assert_not_called()
        self.assert_no_user_or_llm()
        self.assert_audio_clean()

    def test_vad_rejected_audio_is_rejected_before_stt(self):
        self.vad.is_speech.return_value = False
        self.assertEqual(self.voice.run_inactivity_check("event_1"), "no_speech")
        self.client.audio.transcriptions.create.assert_not_called()
        self.assert_no_user_or_llm()
        self.assert_audio_clean()

    def test_short_noise_is_rejected_before_stt(self):
        self.recording = b"\xb0\x04\x50\xfb" * 500
        self.assertEqual(self.voice.run_inactivity_check("event_1"), "no_speech")
        self.client.audio.transcriptions.create.assert_not_called()
        self.assert_no_user_or_llm()
        self.assert_audio_clean()

    def test_empty_transcription_does_not_create_history_or_outbox_entry(self):
        self.transcript = " \n "
        self.assertEqual(self.voice.run_inactivity_check("event_1"), "no_speech")
        self.client.audio.transcriptions.create.assert_called_once()
        self.assert_no_user_or_llm()
        self.assert_audio_clean()

    def test_tts_failure_cleans_partial_audio_without_recording(self):
        def fail(path):
            path.write_bytes(b"partial mp3")
            raise RuntimeError("private detail")

        self.speech.stream_to_file.side_effect = fail
        with self.assertRaises(RuntimeError):
            self.voice.run_inactivity_check("event_1")
        self.capture_utterance.assert_not_called()
        self.assert_no_user_or_llm()
        self.assert_audio_clean()
        self.assertNotIn("private detail", self.output.getvalue())

    def test_playback_failure_does_not_start_microphone(self):
        self.run_audio.side_effect = RuntimeError("private detail")
        with self.assertRaises(RuntimeError):
            self.voice.run_inactivity_check("event_1")
        self.capture_utterance.assert_not_called()
        self.assert_no_user_or_llm()
        self.assert_audio_clean()

    def test_capture_failure_cleans_partial_recording(self):
        def fail(path):
            path.write_bytes(b"partial recording")
            raise OSError("mic failure")

        self.capture_utterance.side_effect = fail
        with self.assertRaises(OSError):
            self.voice.run_inactivity_check("event_1")
        self.client.audio.transcriptions.create.assert_not_called()
        self.assert_no_user_or_llm()
        self.assert_audio_clean()

    def test_stt_failure_cleans_audio_and_preserves_failure(self):
        self.client.audio.transcriptions.create.side_effect = RuntimeError("private detail")
        with self.assertRaises(RuntimeError):
            self.voice.run_inactivity_check("event_1")
        self.assert_no_user_or_llm()
        self.assert_audio_clean()
        self.assertNotIn("private detail", self.output.getvalue())

    def test_cancel_after_capture_does_not_send_recording(self):
        def cancel(path):
            self.capture_audio(path)
            self.voice.stop_event.set()
            return True

        self.capture_utterance.side_effect = cancel
        with self.assertRaises(ConversationStopped):
            self.voice.run_inactivity_check("event_1")
        self.client.audio.transcriptions.create.assert_not_called()
        self.assert_no_user_or_llm()
        self.assert_audio_clean()

    def test_cancel_during_playback_releases_audio_lock(self):
        self.run_audio.side_effect = ConversationStopped
        with self.assertRaises(ConversationStopped):
            self.voice.run_inactivity_check("event_1")
        self.capture_utterance.assert_not_called()
        self.assert_no_user_or_llm()
        self.assert_audio_clean()

    def test_already_cancelled_check_produces_no_audio(self):
        self.voice.stop_event.set()
        with self.assertRaises(ConversationStopped):
            self.voice.run_inactivity_check("event_1")
        self.client.audio.speech.with_streaming_response.create.assert_not_called()
        self.assert_no_user_or_llm()
        self.assert_audio_clean()

    def test_outbox_enqueue_failure_does_not_reclassify_an_explicit_help_request(self):
        self.transcript = "도와주세요"
        self.store.write_message.side_effect = OSError("disk full")
        self.assertEqual(self.voice.run_inactivity_check("event_1"), "help")
        self.client.responses.create.assert_not_called()
        self.assertIn("기록 보관 실패", self.output.getvalue())
        self.assert_audio_clean()

    def test_logs_link_source_event_and_outcome_without_repeating_transcript(self):
        self.transcript = "개인적인 말을 했어요"
        self.assertEqual(self.voice.run_inactivity_check("event_unique"), "unclear")
        output = self.output.getvalue()
        self.assertIn("event=event_unique stage=start", output)
        self.assertIn("event=event_unique stage=end outcome=unclear", output)
        self.assertNotIn(self.transcript, output)

    def make_care_controller(self):
        with patch("firebase_inactivity.FirebaseInactivityClient") as client_type:
            controller = create_inactivity_controller(
                "https://example.invalid", "elder_001", "device_001", self.voice,
            )
            client_type.assert_called_once_with(
                "https://example.invalid", "elder_001", "device_001",
            )
        return controller

    def test_queued_resolved_event_is_cancelled_before_audio(self):
        controller = self.make_care_controller()
        controller.client.validate_before_audio.return_value = False
        def run(_event_id, *, before_start=None):
            self.assertIsNotNone(before_start)
            return "okay" if before_start() else "cancelled"

        with patch.object(controller.conversation, "run_inactivity_check",
                          side_effect=run) as audio:
            self.assertEqual(controller._check("event_1"), "cancelled")
        controller.client.validate_before_audio.assert_called_once_with("event_1")
        audio.assert_called_once()

    def test_failed_pre_audio_validation_does_not_speak(self):
        controller = self.make_care_controller()
        controller.client.validate_before_audio.side_effect = RuntimeError("read unavailable")
        def run(_event_id, *, before_start=None):
            return "okay" if before_start() else "cancelled"

        with patch.object(controller.conversation, "run_inactivity_check",
                          side_effect=run) as audio:
            with self.assertRaises(RuntimeError):
                controller._check("event_1")
        audio.assert_called_once()

    def test_still_open_event_reaches_voice_callback(self):
        controller = self.make_care_controller()
        controller.client.validate_before_audio.return_value = True
        def run(_event_id, *, before_start=None):
            self.assertTrue(before_start())
            return "help"

        with patch.object(controller.conversation, "run_inactivity_check",
                          side_effect=run) as audio:
            self.assertEqual(controller._check("event_1"), "help")
        audio.assert_called_once()

    def test_real_care_worker_starts_polls_and_stops(self):
        from firebase_inactivity import FirebaseInactivityClient
        from inactivity_care import InactivityCareController

        polled = threading.Event()
        http = Mock()

        def get(*args, **kwargs):
            polled.set()
            return Mock(status_code=200, json=Mock(return_value={}))

        http.get.side_effect = get
        client = FirebaseInactivityClient("https://example.invalid", "elder_001", "pi_001", http=http)
        controller = InactivityCareController(client, self.voice)
        controller.start()
        try:
            self.assertTrue(polled.wait(5), "care worker failed before first poll")
        finally:
            controller.stop()
        self.assertFalse(controller._thread.is_alive())
        http.put.assert_not_called()
        self.client.audio.speech.with_streaming_response.create.assert_not_called()

    def test_factory_isolates_history_sessions_and_shutdown_but_shares_adapters(self):
        self.voice.history = [{"role": "user", "content": "학교에 갔어요"}]
        old_session = self.voice.session_id
        care = self.make_care_controller().conversation
        self.assertIsNot(care, self.voice)
        for name in ("client", "settings", "store", "audio_lock", "interaction_lock",
                     "interaction_manager", "care_prompt_service"):
            self.assertIs(getattr(care, name), getattr(self.voice, name))
        self.assertEqual(care.audio_dir, self.voice.audio_dir)
        self.assertIsNot(care.stop_event, self.voice.stop_event)
        with patch.object(care, "capture_utterance", side_effect=self.capture_audio), \
                patch.object(care, "run_audio", side_effect=self.play_audio):
            self.assertEqual(care.run_inactivity_check("event_1"), "okay")
        self.assertEqual(self.voice.session_id, old_session)
        self.assertEqual(self.voice.history, [{"role": "user", "content": "학교에 갔어요"}])
        self.assertEqual(care.history, [])
        self.assertNotEqual(care.session_id, old_session)
        care.stop_event.set()
        self.assertFalse(self.voice.stop_event.is_set())
        self.assert_audio_clean()

    def test_care_runs_between_turns_and_conversation_resumes(self):
        care = self.make_care_controller().conversation
        self.voice.settings = replace(self.voice.settings, session_max_turns=2)
        attempted = threading.Event()
        finished = threading.Event()
        observed = []
        results = []
        failures = []
        original_turn = self.voice.run_turn
        original_lock = self.voice.interaction_lock

        class ObservedCareLock:
            def acquire(inner, timeout):
                # Record contention at the exact boundary after turn one's
                # nested lock was released, before turn two is allowed to run.
                acquired = original_lock.acquire(blocking=False)
                observed.append(not acquired)
                attempted.set()
                return acquired or original_lock.acquire(timeout=timeout)

            def release(inner):
                original_lock.release()

        care.interaction_lock = ObservedCareLock()

        def run_care():
            try:
                results.append(care.run_inactivity_check("event_1"))
            except BaseException as exc:
                failures.append(exc)
            finally:
                finished.set()

        care_thread = threading.Thread(target=run_care, daemon=True)
        calls = [0]

        def session_turn(**kwargs):
            result = original_turn(**kwargs)
            calls[0] += 1
            if calls[0] == 1:
                care_thread.start()
                self.assertTrue(attempted.wait(5), "care worker did not attempt interaction")
                self.assertTrue(observed[0], "care did not queue behind the active turn")
                self.assertFalse(finished.is_set())
            elif calls[0] == 2:
                self.assertTrue(finished.wait(5), "care did not finish before chat resumed")
            return result

        with patch.object(self.voice, "run_turn", side_effect=session_turn), \
                patch.object(care, "capture_utterance", side_effect=self.capture_audio), \
                patch.object(care, "run_audio", side_effect=self.play_audio):
            try:
                self.assertTrue(self.voice.run_session())
            finally:
                care_thread.join(timeout=5)
        self.assertFalse(care_thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(results, ["okay"])
        # Two chat replies plus one isolated care wording request. The care
        # reply is still classified locally and is never sent to this model.
        self.assertEqual(self.client.responses.create.call_count, 3)
        self.assertEqual(self.client.audio.transcriptions.create.call_count, 3)
        self.assert_audio_clean()


if __name__ == "__main__":
    unittest.main()
