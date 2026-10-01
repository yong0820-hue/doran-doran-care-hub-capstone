"""Conservative voice-only medication replies and the shared audio lifecycle."""

import io
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import wave
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "raspberry-pi-voice-hub"))
from display_state import DisplayState
from medication_care import (
    MEDICATION_DISPLAY_MESSAGE, MEDICATION_PROMPT, MedicationAnnouncementCare, MedicationCare,
    classify_medication_reply,
)
from voice_ai import (
    ConversationStopped, VoiceConversation, VoiceSettings, create_medication_care,
)


class MedicationReplyTests(unittest.TestCase):
    def test_only_explicit_completed_medication_phrases_confirm(self):
        for text in (
            "먹었어", "먹었어요?", "먹었어요.", "먹었어요!", "먹었습니다!",
            "복용했어", "복용했어요",
            "복용했습니다", "네, 약을 먹었어요.", "예. 저는 이미 약 복용했어요",
            "나는 약 먹었어", "응 약은 먹었지", "이미 먹었어요", "벌써 먹었죠",
            '  "먹었어요?"  ', "‘ 복용했어요. ’",
        ):
            with self.subTest(text=text):
                self.assertEqual(classify_medication_reply(text), "confirmed")

    def test_negations_and_future_plans_are_defer(self):
        for text in (
            "조금 있다 먹을게", "조금 이따 먹을게요", "아직 안 먹었어",
            "아직 안 먹었어요.", "약 안 먹었어요", "약은 아직 못 먹었어요",
            "먹지 않았습니다", "복용하지 않았어요", "아직 복용하지 못했어요",
            "약을 나중에 먹을게요", "이따 약 먹을게", "먹을 거예요",
            "조금 후에 복용할게요", "아직이에요", "네, 아직 안 먹었어요",
        ):
            with self.subTest(text=text):
                self.assertEqual(classify_medication_reply(text), "defer")

    def test_uncertainty_questions_and_non_medication_objects_never_confirm(self):
        for text in (
            "먹었나?", "먹었나요?", "먹었는지 모르겠어요",
            "먹은 것 같아요", "아마 먹었어요", "먹었겠지", "먹었을걸",
            "밥 먹었어", "저는 밥 먹었어요", "간식 먹었어", "물 먹었어",
            "네", "응", "그만", "안 먹을게", "먹어야지", "먹었나요",
        ):
            with self.subTest(text=text):
                self.assertEqual(classify_medication_reply(text), "unclear")

    def test_quoted_reported_conditional_and_mixed_statements_are_unclear(self):
        for text in (
            '할머니가 "먹었어요"', "엄마 약 먹었어요", '"먹었어요',
            "먹었어요라고 말했어요", "먹었으면 좋겠어요", "먹었다고 하네요",
            "먹었어요. 아니 아직 안 먹었어요", "안 먹었어, 조금 있다 먹을게",
            "조금 있다 먹을게. 먹었어요", "먹었어요, 그런데 잘 모르겠어요",
            "먹었어요. 밥을요", "먹었어요라고 말씀해 주세요",
        ):
            with self.subTest(text=text):
                self.assertEqual(classify_medication_reply(text), "unclear")

    def test_negative_replies_never_confirm_after_transcription_normalization(self):
        for text in ("안 먹었어요?", "아직 안 먹었어요!", '"못 먹었어요."'):
            with self.subTest(text=text):
                self.assertNotEqual(classify_medication_reply(text), "confirmed")
                self.assertEqual(classify_medication_reply(text), "defer")

    def test_whitespace_is_no_speech_but_punctuation_is_unclear(self):
        for text in ("", " ", "\n\t"):
            with self.subTest(text=text):
                self.assertEqual(classify_medication_reply(text), "no_speech")
        self.assertEqual(classify_medication_reply("..."), "unclear")


class MedicationVoiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = io.StringIO()
        quiet = redirect_stdout(self.output)
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.lock = threading.Lock()
        self.order = []
        self.display = DisplayState()
        replacement = patch("voice_ai.DISPLAY", self.display)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.client = Mock()
        self.client.audio.transcriptions.create.side_effect = self.transcribe
        self.transcript = "먹었어요"
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
        replacement = patch.object(self.voice, "new_vad", return_value=self.vad)
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
        self.assertEqual(self.display.snapshot()["state"], "thinking")
        path.write_bytes(b"mock mp3")

    def capture_audio(self, path):
        self.order.append("capture")
        self.assertTrue(self.lock.locked())
        self.voice.check_stopped()
        state = self.display.snapshot()
        self.assertEqual(state["state"], "listening")
        self.assertEqual(state["alert"]["kind"], "medication")
        self.assertEqual(state["alert"]["message"], MEDICATION_DISPLAY_MESSAGE)
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
        self.assertEqual(self.display.snapshot()["state"], "speaking")
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

    def assert_finished(self):
        self.assertFalse(self.lock.locked())
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])
        snapshot = self.display.snapshot()
        self.assertEqual({key: snapshot[key] for key in ("state", "busy", "alert")},
                         {"state": "idle", "busy": False, "alert": None})
        self.assertFalse(snapshot["audio_focus"]["blocked"])
        self.client.responses.create.assert_not_called()
        self.assertEqual(self.voice.history, [])

    def test_one_prompt_one_vad_validated_reply_preserves_existing_voice_defaults(self):
        self.assertEqual(self.voice.run_medication_check("event_1"), "confirmed")
        self.assertEqual(self.order, ["tts", "playback", "capture", "stt"])
        speech = self.client.audio.speech.with_streaming_response.create
        speech.assert_called_once()
        self.assertEqual(speech.call_args.kwargs["input"], MEDICATION_PROMPT)
        self.assertEqual(speech.call_args.kwargs["voice"], "marin")
        self.assertEqual(speech.call_args.kwargs["speed"], 1.15)
        self.assertEqual(self.voice.settings.max_utterance_seconds, 20)
        self.assert_finished()

    def test_contextual_prompt_after_conversation_keeps_one_capture_and_classifier(self):
        contextual = (
            "아, 그리고 어르신. 지금 약 드실 시간이니 먼저 챙겨 드실까요?"
        )
        self.client.responses.create.return_value = Mock(
            output_text=contextual, status="completed",
        )
        with self.voice.use_interaction(kind="conversation"):
            pass

        self.assertEqual(self.voice.run_medication_check("event_1"), "confirmed")
        self.assertEqual(self.order, ["tts", "playback", "capture", "stt"])
        self.assertEqual(self.capture_utterance.call_count, 1)
        self.assertEqual(
            self.client.audio.speech.with_streaming_response.create.call_args.kwargs["input"],
            contextual,
        )
        self.client.responses.create.assert_called_once()
        self.store.write_message.assert_called_once()
        self.assertEqual(self.voice.history, [])

    def test_prompt_and_capture_hold_one_unbroken_audio_lock(self):
        events = []

        def acquire(**kwargs):
            events.append("acquire")
            return self.lock.acquire(**kwargs)

        def release():
            events.append("release")
            self.lock.release()

        self.voice.audio_lock = Mock(acquire=acquire, release=release)
        self.assertEqual(self.voice.run_medication_check("event_1"), "confirmed")
        self.assertEqual(events, ["acquire", "release"])
        self.assert_finished()

    def test_all_recognized_outcomes_use_current_outbox_without_chat_history(self):
        for transcript, expected in (
            ("복용했어요", "confirmed"), ("조금 있다 먹을게", "defer"),
            ("아직 안 먹었어", "defer"), ("잘 모르겠어요", "unclear"),
        ):
            with self.subTest(transcript=transcript):
                self.transcript = transcript
                self.store.reset_mock()
                self.assertEqual(self.voice.run_medication_check("event_1"), expected)
                self.store.write_message.assert_called_once()
                record = self.store.write_message.call_args.kwargs
                self.assertEqual(record["role"], "user")
                self.assertEqual(record["text"], transcript)
                self.assertEqual(record["audio_status"], "recorded")
                self.assertEqual(record["turn_index"], 1)
                self.assertEqual(record["session_id"], self.voice.session_id)
                self.assertIs(record["flush"], False)
                self.assert_finished()

    def test_no_speech_skips_stt_outbox_and_any_second_prompt(self):
        self.capture_utterance.side_effect = lambda path: False
        with patch.object(self.voice, "has_meaningful_speech") as validate:
            self.assertEqual(self.voice.run_medication_check("event_1"), "no_speech")
            validate.assert_not_called()
        self.client.audio.transcriptions.create.assert_not_called()
        self.store.write_message.assert_not_called()
        self.assertEqual(self.run_audio.call_count, 1)
        self.assert_finished()

    def test_schedule_change_after_tts_cancels_before_physical_playback(self):
        def changed():
            self.assertTrue(self.lock.locked())
            return False

        before_audio = Mock(side_effect=changed)
        self.assertEqual(
            self.voice.run_medication_check("event_1", before_audio),
            "cancelled",
        )
        self.assertEqual(self.order, ["tts"])
        before_audio.assert_called_once_with()
        self.run_audio.assert_not_called()
        self.capture_utterance.assert_not_called()
        self.client.audio.transcriptions.create.assert_not_called()
        self.store.write_message.assert_not_called()
        self.assert_finished()

    def test_silent_short_and_vad_rejected_recordings_never_reach_stt(self):
        for recording, is_speech in (
            (b"\x00\x00" * 8000, True), (self.recording[:1000], True),
            (self.recording, False),
        ):
            with self.subTest(size=len(recording), vad=is_speech):
                self.recording = recording
                self.vad.is_speech.return_value = is_speech
                self.assertEqual(self.voice.run_medication_check("event_1"), "no_speech")
                self.client.audio.transcriptions.create.assert_not_called()
                self.store.write_message.assert_not_called()
                self.assert_finished()

    def test_empty_stt_is_no_speech_and_does_not_create_outbox_entry(self):
        self.transcript = " \n "
        self.assertEqual(self.voice.run_medication_check("event_1"), "no_speech")
        self.client.audio.transcriptions.create.assert_called_once()
        self.store.write_message.assert_not_called()
        self.assert_finished()

    def test_playback_failure_never_records_and_restores_display(self):
        self.run_audio.side_effect = RuntimeError("private error")
        with self.assertRaises(RuntimeError):
            self.voice.run_medication_check("event_1")
        self.capture_utterance.assert_not_called()
        self.client.audio.transcriptions.create.assert_not_called()
        self.assertNotIn("private error", self.output.getvalue())
        self.assert_finished()

    def test_stop_after_capture_prevents_upload_and_restores_display(self):
        def cancel(path):
            self.capture_audio(path)
            self.voice.stop_event.set()
            return True

        self.capture_utterance.side_effect = cancel
        with self.assertRaises(ConversationStopped):
            self.voice.run_medication_check("event_1")
        self.client.audio.transcriptions.create.assert_not_called()
        self.store.write_message.assert_not_called()
        self.assert_finished()

    def test_stt_failure_cleans_audio_and_logs_no_private_text(self):
        self.client.audio.transcriptions.create.side_effect = RuntimeError("private error")
        with self.assertRaises(RuntimeError):
            self.voice.run_medication_check("event_1")
        self.store.write_message.assert_not_called()
        self.assertNotIn("private error", self.output.getvalue())
        self.assert_finished()

    def test_logs_correlate_event_and_outcome_without_repeating_transcript(self):
        self.transcript = "잘 모르겠어요"
        self.assertEqual(self.voice.run_medication_check("event_unique"), "unclear")
        self.assertIn("event=event_unique stage=start", self.output.getvalue())
        self.assertIn("event=event_unique stage=end outcome=unclear", self.output.getvalue())
        self.assertNotIn(self.transcript, self.output.getvalue())
        self.assert_finished()

    def test_factory_shares_audio_and_outbox_but_preserves_active_chat(self):
        self.voice.history = [{"role": "user", "content": "공원에 갔어요"}]
        old_session = self.voice.session_id
        old_history = list(self.voice.history)
        event_factory = Mock(return_value={"type": "medication"})
        with patch("firebase_medication.FirebaseMedicationClient") as client_type:
            care = create_medication_care(
                "https://example.invalid", "elder_001", "pi_001", self.voice, event_factory,
            )
            client_type.assert_called_once_with("https://example.invalid", "elder_001", "pi_001")
        for name in ("client", "settings", "store", "audio_lock", "interaction_lock",
                     "interaction_manager", "care_prompt_service"):
            self.assertIs(getattr(care.conversation, name), getattr(self.voice, name))
        self.assertEqual(care.conversation.audio_dir, self.voice.audio_dir)
        self.assertIsNot(care.conversation.stop_event, self.voice.stop_event)
        with patch.object(care.conversation, "capture_utterance", side_effect=self.capture_audio), \
                patch.object(care.conversation, "run_audio", side_effect=self.play_audio), \
                patch.object(care.conversation, "new_vad", return_value=self.vad):
            self.assertEqual(care.conversation.run_medication_check("event_1"), "confirmed")
        self.assertEqual(self.voice.session_id, old_session)
        self.assertEqual(self.voice.history, old_history)
        self.assertEqual(care.conversation.history, [])
        self.assertNotEqual(care.conversation.session_id, old_session)
        care.stop()
        self.assertFalse(self.voice.stop_event.is_set())
        self.assertFalse(self.lock.locked())
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])


class MedicationCareTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        quiet = redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.voice = VoiceConversation(
            Mock(), VoiceSettings(), Mock(), threading.Lock(), self.temporary.name,
        )
        self.client = Mock()
        self.event = {"type": "medication", "status": "open"}
        self.factory = Mock(return_value=self.event)
        self.care = MedicationCare(self.client, self.voice, self.factory)
        self.addCleanup(self.care.stop)

    def test_enqueue_returns_without_network_or_audio_then_worker_processes_once(self):
        handled = threading.Event()
        self.client.run_scheduled.side_effect = lambda *args: handled.set()
        self.assertIs(self.care.run_scheduled("2026-09-15_14:02"), True)
        self.assertIs(self.care.run_scheduled("2026-09-15_14:02"), False)
        self.factory.assert_called_once_with()
        self.client.run_scheduled.assert_not_called()
        self.assertIs(self.care.start(), self.care)
        self.assertIs(self.care.start(), self.care)
        self.assertTrue(handled.wait(5), "medication worker did not process slot")
        self.care.stop()
        self.client.run_scheduled.assert_called_once_with(
            "2026-09-15_14:02", self.event, self.care._check,
        )
        self.assertFalse(self.care._thread.is_alive())

    def test_adjacent_slots_enqueue_while_chat_holds_lock_and_process_in_order(self):
        first_waiting = threading.Event()
        finished = threading.Event()
        handled = []
        results = []
        original_lock = self.voice.interaction_lock

        class ObservedLock:
            def acquire(inner, timeout):
                first_waiting.set()
                return original_lock.acquire(timeout=timeout)

            def release(inner):
                original_lock.release()

        self.voice.interaction_lock = ObservedLock()
        self.client.validate_before_audio.return_value = True

        def run(slot, event, callback):
            handled.append(slot)
            results.append(callback(slot))
            if len(results) == 2:
                finished.set()

        self.client.run_scheduled.side_effect = run
        voice_patch = patch.object(self.voice, "_run_care_check", return_value="confirmed")
        audio = voice_patch.start()
        self.addCleanup(voice_patch.stop)
        with original_lock:
            self.care.start()
            self.assertTrue(self.care.run_scheduled("2026-09-15_14:02"))
            self.assertTrue(first_waiting.wait(5), "worker did not wait for chat")
            self.assertTrue(self.care.run_scheduled("2026-09-15_14:03"))
            self.assertFalse(self.care.run_scheduled("2026-09-15_14:03"))
            audio.assert_not_called()
            self.client.validate_before_audio.assert_not_called()
            self.assertEqual(handled, ["2026-09-15_14:02"])
        self.assertTrue(finished.wait(5), "queued medication slots were not processed")
        self.care.stop()
        self.assertEqual(handled, ["2026-09-15_14:02", "2026-09-15_14:03"])
        self.assertEqual(results, ["confirmed", "confirmed"])
        self.assertEqual(self.client.validate_before_audio.call_count, 2)
        self.factory.assert_has_calls([unittest.mock.call(), unittest.mock.call()])

    def test_idle_retry_runs_on_same_worker_and_never_replays_audio(self):
        handled = threading.Event()
        retried = threading.Event()
        identities = []

        def run(*args):
            identities.append(threading.get_ident())
            handled.set()

        def retry():
            identities.append(threading.get_ident())
            retried.set()

        self.client.run_scheduled.side_effect = run
        self.client.retry_pending.side_effect = retry
        self.care.RETRY_SECONDS = 0.01
        self.care.run_scheduled("2026-09-15_14:02")
        self.care.start()
        self.assertTrue(handled.wait(5))
        self.assertTrue(retried.wait(5))
        self.care.stop()
        self.client.run_scheduled.assert_called_once()
        self.assertEqual(len(set(identities)), 1)
        self.assertNotEqual(identities[0], threading.get_ident())

    def test_worker_failure_does_not_drop_next_slot_or_repeat_failed_slot(self):
        handled = threading.Event()
        calls = []

        def run(slot, event, callback):
            calls.append(slot)
            if len(calls) == 1:
                raise RuntimeError("private failure")
            handled.set()

        self.client.run_scheduled.side_effect = run
        self.care.run_scheduled("2026-09-15_14:02")
        self.care.run_scheduled("2026-09-15_14:03")
        self.care.start()
        self.assertTrue(handled.wait(5))
        self.assertFalse(self.care.run_scheduled("2026-09-15_14:02"))
        self.care.stop()
        self.assertEqual(calls, ["2026-09-15_14:02", "2026-09-15_14:03"])

    def test_resolved_while_queued_event_cancels_without_audio(self):
        self.client.validate_before_audio.return_value = False
        def run(_event_id, _validity_check=None, *, before_start=None):
            self.assertIsNotNone(before_start)
            return "confirmed" if before_start() else "cancelled"

        with patch.object(self.voice, "run_medication_check", side_effect=run) as audio:
            self.assertEqual(self.care._check("event_1"), "cancelled")
        self.client.validate_before_audio.assert_called_once_with("event_1")
        audio.assert_called_once()

    def test_failed_pre_audio_validation_never_speaks(self):
        self.client.validate_before_audio.side_effect = RuntimeError("unavailable")
        def run(_event_id, _validity_check=None, *, before_start=None):
            return "confirmed" if before_start() else "cancelled"

        with patch.object(self.voice, "run_medication_check", side_effect=run) as audio:
            with self.assertRaises(RuntimeError):
                self.care._check("event_1")
        audio.assert_called_once()

    def test_schedule_validity_is_rechecked_inside_voice_audio_lock(self):
        self.client.validate_before_audio.return_value = True
        validity = Mock(return_value=True)
        def run(_event_id, before_audio=None, *, before_start=None):
            self.assertIs(before_audio, validity)
            self.assertTrue(before_start())
            return "confirmed"

        with patch.object(self.voice, "run_medication_check", side_effect=run) as audio:
            self.assertEqual(self.care._check("event_1", validity), "confirmed")
        audio.assert_called_once()
        self.assertEqual(validity.call_count, 2)

    def test_stop_before_start_discards_queue_and_never_contacts_firebase(self):
        self.care.run_scheduled("2026-09-15_14:02")
        self.care.stop()
        self.assertIs(self.care.start(), self.care)
        self.assertFalse(self.care.run_scheduled("2026-09-15_14:03"))
        self.client.run_scheduled.assert_not_called()
        self.client.retry_pending.assert_not_called()
        self.factory.assert_called_once()
        self.assertFalse(self.care._thread.is_alive())
        with self.assertRaises(ConversationStopped):
            self.care._check("event_1")
        self.client.validate_before_audio.assert_not_called()

    def test_stop_cancels_waiting_check_and_discards_other_queued_slots(self):
        waiting = threading.Event()
        original_lock = self.voice.interaction_lock

        class ObservedLock:
            def acquire(inner, timeout):
                waiting.set()
                return original_lock.acquire(timeout=timeout)

            def release(inner):
                original_lock.release()

        self.voice.interaction_lock = ObservedLock()

        def run(slot, event, callback):
            return callback(slot)

        self.client.run_scheduled.side_effect = run
        with original_lock, patch.object(self.voice, "_run_care_check") as audio:
            self.care.start()
            self.care.run_scheduled("2026-09-15_14:02")
            self.assertTrue(waiting.wait(5))
            self.care.run_scheduled("2026-09-15_14:03")
            self.care.stop()
            self.assertFalse(self.care._thread.is_alive())
            audio.assert_not_called()
        self.client.run_scheduled.assert_called_once()
        self.client.validate_before_audio.assert_not_called()
        self.client.retry_pending.assert_not_called()


class MedicationAnnouncementCareTests(unittest.TestCase):
    def test_announcement_waits_for_whole_interaction_and_revalidates_after_wait(self):
        for resolved_while_waiting in (False, True):
            with self.subTest(resolved_while_waiting=resolved_while_waiting):
                lock = threading.RLock()
                waiting = threading.Event()

                class ObservedLock:
                    def acquire(inner, timeout):
                        waiting.set()
                        return lock.acquire(timeout=timeout)

                    def release(inner):
                        lock.release()

                client = Mock()
                client.validate_before_audio.return_value = True
                played = Mock()
                outcomes = []

                def announce(before_audio):
                    if not before_audio():
                        return "cancelled"
                    played()
                    return "announced"

                care = MedicationAnnouncementCare(client, announce, ObservedLock())
                worker = threading.Thread(
                    target=lambda: outcomes.append(care._announce("event_1", None)),
                )
                with lock:
                    worker.start()
                    self.assertTrue(waiting.wait(2))
                    played.assert_not_called()
                    client.validate_before_audio.assert_not_called()
                    client.validate_before_audio.return_value = not resolved_while_waiting
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(outcomes, ["cancelled" if resolved_while_waiting else "announced"])
                self.assertEqual(played.call_count, int(not resolved_while_waiting))
                client.validate_before_audio.assert_called_once_with("event_1")

    def test_shutdown_cancels_announcement_waiting_for_chat_without_audio(self):
        lock = threading.RLock()
        waiting = threading.Event()

        class ObservedLock:
            def acquire(inner, timeout):
                waiting.set()
                return lock.acquire(timeout=timeout)

            def release(inner):
                lock.release()

        client = Mock()
        announce = Mock()
        outcomes = []

        def run_announcement(_slot, _event, callback):
            outcomes.append(callback("event_1"))
            return True

        client.run_announcement.side_effect = run_announcement
        care = MedicationAnnouncementCare(client, announce, ObservedLock()).start()
        try:
            with lock:
                care.run_scheduled("2026-09-16_08:00", {})
                self.assertTrue(waiting.wait(2))
                care.stop()
                self.assertFalse(care._thread.is_alive())
                announce.assert_not_called()
                client.validate_before_audio.assert_not_called()
        finally:
            care.stop()
        self.assertEqual(outcomes, ["cancelled"])

    def test_announcement_error_releases_shared_interaction_lock(self):
        lock = threading.Lock()
        care = MedicationAnnouncementCare(
            Mock(), Mock(side_effect=RuntimeError("playback failed")), lock,
        )
        with self.assertRaises(RuntimeError):
            care._announce("event_1", None)
        self.assertFalse(lock.locked())

    def test_claimed_event_is_revalidated_by_the_pre_play_predicate(self):
        client = Mock()
        client.validate_before_audio.return_value = False
        completed = threading.Event()
        predicate_results = []

        def announce(before_audio):
            predicate_results.append(before_audio())
            return "cancelled"

        def run_announcement(_slot, _event, callback):
            callback("event_1")
            completed.set()
            return True

        client.run_announcement.side_effect = run_announcement
        care = MedicationAnnouncementCare(client, announce).start()
        try:
            self.assertTrue(care.run_scheduled(
                "2026-09-16_08:00",
                {"type": "medication_reminder_sent", "meta": {"scheduleId": "schedule_a"}},
                Mock(return_value=True),
            ))
            self.assertTrue(completed.wait(2))
        finally:
            care.stop()
        self.assertEqual(predicate_results, [False])
        client.validate_before_audio.assert_called_once_with("event_1")


if __name__ == "__main__":
    unittest.main()
