"""Bounded dashboard sessions with real turn flow and mocked audio/API edges."""

import io
import os
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "raspberry-pi-voice-hub"))
from voice_ai import ConversationStopped, LISTEN_PROMPT, VoiceConversation, VoiceSettings


class VoiceSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = io.StringIO()
        quiet = redirect_stdout(self.output)
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.lock = threading.Lock()
        self.client = Mock()
        self.client.audio.transcriptions.create.return_value = Mock(text="오늘 산책했어요")
        self.client.responses.create.return_value = Mock(
            output_text="어디로 다녀오셨어요?", status="completed"
        )
        self.speech = Mock()
        self.speech.stream_to_file.side_effect = lambda path: path.write_bytes(b"mock mp3")
        self.client.audio.speech.with_streaming_response.create.return_value = Mock(
            __enter__=Mock(return_value=self.speech), __exit__=Mock(return_value=False)
        )
        self.store = Mock()
        self.store.write_message.return_value = True
        self.voice = VoiceConversation(
            self.client, VoiceSettings(), self.store, self.lock, self.temporary.name
        )

        def capture(path):
            self.voice.check_stopped()
            self.assertTrue(self.lock.locked())
            path.write_bytes(b"mock recording")
            return True

        def playback(command, timeout):
            self.voice.check_stopped()
            self.assertTrue(self.lock.locked())
            self.assertTrue(Path(command[-1]).is_file())

        # VAD and endpointing are covered separately. Exercise the complete turn
        # lifecycle here without native sound libraries, hardware, or requests.
        for name, arguments in (
            ("capture_utterance", {"side_effect": capture}),
            ("run_audio", {"side_effect": playback}),
            ("has_meaningful_speech", {"return_value": True}),
        ):
            replacement = patch.object(self.voice, name, **arguments)
            setattr(self, name, replacement.start())
            self.addCleanup(replacement.stop)

    def transcriptions(self, *texts):
        self.client.audio.transcriptions.create.side_effect = [Mock(text=text) for text in texts]

    def session_events(self):
        return [
            dict(part.split("=", 1) for part in line.split()[2:])
            for line in self.output.getvalue().splitlines()
            if line.startswith("[Voice Session] ")
        ]

    def assert_session_end(self, reason):
        events = self.session_events()
        self.assertEqual(len([e for e in events if e.get("event") == "start"]), 1)
        ends = [e for e in events if e.get("event") == "end"]
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0]["reason"], reason)
        self.assertEqual({event["session"] for event in events}, {self.voice.session_id})

    def assert_audio_clean(self):
        self.assertFalse(self.lock.locked())
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])

    def test_multiple_replies_listen_again_with_only_one_prompt_and_shared_history(self):
        self.transcriptions("오늘 산책했어요", "동네 공원에 갔어요", "그만")
        self.client.responses.create.side_effect = [
            Mock(output_text="어디로 다녀오셨어요?", status="completed"),
            Mock(output_text="공원에서 무엇을 보셨어요?", status="completed"),
        ]
        self.assertTrue(self.voice.run_session())
        self.assertEqual(self.capture_utterance.call_count, 3)
        self.assertEqual([
            call.kwargs["input"]
            for call in self.client.audio.speech.with_streaming_response.create.call_args_list
        ], [LISTEN_PROMPT, "어디로 다녀오셨어요?", "공원에서 무엇을 보셨어요?"])
        calls = self.client.responses.create.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].kwargs["input"], [
            {"role": "user", "content": "오늘 산책했어요"},
            {"role": "assistant", "content": "어디로 다녀오셨어요?"},
            {"role": "user", "content": "동네 공원에 갔어요"},
        ])
        self.assertEqual(self.voice.history, calls[1].kwargs["input"] + [
            {"role": "assistant", "content": "공원에서 무엇을 보셨어요?"},
        ])
        records = [call.kwargs for call in self.store.write_message.call_args_list]
        self.assertEqual([record["role"] for record in records], ["user", "assistant"] * 2)
        self.assertEqual([record["turn_index"] for record in records], [1, 1, 2, 2])
        self.assertEqual([record["audio_status"] for record in records], ["recorded", "played"] * 2)
        self.assertEqual({record["session_id"] for record in records}, {self.voice.session_id})
        self.assertTrue(all(record["flush"] is False for record in records))
        turns = [event for event in self.session_events() if event.get("event") == "turn"]
        self.assertEqual([event["turn"] for event in turns], ["1", "2", "3"])
        self.assertEqual([event["attempt"] for event in turns], ["1", "2", "3"])
        self.assert_session_end("stop_phrase")
        self.assert_audio_clean()

    def test_two_care_requests_wait_for_safe_boundary_then_chat_resumes(self):
        self.transcriptions("오늘 산책했어요", "대화 종료해줘")
        events = []
        results = []
        care_sessions = {}
        care_threads = []

        def make_care(name, outcome):
            care = VoiceConversation(
                self.client, self.voice.settings, self.store, self.lock,
                self.temporary.name,
                interaction_manager=self.voice.interaction_manager,
                care_prompt_service=self.voice.care_prompt_service,
            )

            def run_isolated(*_args, **_kwargs):
                care.reset()
                care_sessions[name] = care.session_id
                events.extend([f"{name}-start", f"{name}-capture", f"{name}-end"])
                return outcome

            patcher = patch.object(care, "_run_care_check", side_effect=run_isolated)
            patcher.start()
            self.addCleanup(patcher.stop)
            return care

        medication = make_care("medication", "confirmed")
        inactivity = make_care("inactivity", "okay")

        def start_care(target):
            thread = threading.Thread(target=target, daemon=True)
            care_threads.append(thread)
            thread.start()

        def wait_for_pending(count):
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with self.voice.interaction_manager._state_lock:
                    if len(self.voice.interaction_manager._pending) >= count:
                        return
                time.sleep(0.005)
            self.fail(f"{count} care requests were not queued")

        original_capture = self.capture_utterance.side_effect

        def capture(path):
            capture_number = self.capture_utterance.call_count
            events.append(f"chat-capture-{capture_number}")
            if capture_number == 1:
                start_care(lambda: results.append(
                    medication.run_medication_check("medication-event")
                ))
                wait_for_pending(1)
                self.assertNotIn("medication-start", events)
            return original_capture(path)

        self.capture_utterance.side_effect = capture

        def stream_to_file(path):
            if str(path).endswith("_response.mp3"):
                events.append("ai-response-tts")
                self.assertNotIn("medication-start", events)
            path.write_bytes(b"mock mp3")

        self.speech.stream_to_file.side_effect = stream_to_file
        original_playback = self.run_audio.side_effect

        def playback(command, timeout):
            original_playback(command, timeout)
            if str(command[-1]).endswith("_response.mp3"):
                events.append("ai-response-playback")
                start_care(lambda: results.append(
                    inactivity.run_inactivity_check("inactivity-event")
                ))
                wait_for_pending(2)
                self.assertNotIn("medication-start", events)
                self.assertNotIn("inactivity-start", events)

        self.run_audio.side_effect = playback

        self.assertTrue(self.voice.run_session())
        for thread in care_threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())

        self.assertCountEqual(results, ["confirmed", "okay"])
        first_care_start = min(
            events.index("medication-start"), events.index("inactivity-start"),
        )
        second_chat_capture = events.index("chat-capture-2")
        self.assertGreater(first_care_start, events.index("ai-response-playback"))
        self.assertLess(events.index("medication-end"), second_chat_capture)
        self.assertLess(events.index("inactivity-end"), second_chat_capture)
        for name in ("medication", "inactivity"):
            start = events.index(f"{name}-start")
            capture_index = events.index(f"{name}-capture")
            end = events.index(f"{name}-end")
            self.assertEqual((capture_index - start, end - capture_index), (1, 1))
            self.assertEqual(events.count(f"{name}-capture"), 1)
        first_name = events[first_care_start].removesuffix("-start")
        other_name = "inactivity" if first_name == "medication" else "medication"
        self.assertLess(events.index(f"{first_name}-end"),
                        events.index(f"{other_name}-start"))
        self.assertEqual(len(set(care_sessions.values())), 2)
        self.assertNotIn(self.voice.session_id, care_sessions.values())
        self.assertEqual(self.voice.turn_index, 1)
        self.assertEqual(len(self.voice.history), 2)
        self.assert_session_end("stop_phrase")
        self.assert_audio_clean()

    def test_stop_phrase_boundary_drains_two_pending_care_requests(self):
        self.transcriptions("대화 종료해줘")
        order = []
        results = []
        threads = []

        def make_care(name, outcome):
            care = VoiceConversation(
                self.client, self.voice.settings, self.store, self.lock,
                self.temporary.name,
                interaction_manager=self.voice.interaction_manager,
                care_prompt_service=self.voice.care_prompt_service,
            )

            def run_isolated(*_args, **_kwargs):
                order.extend([f"{name}-start", f"{name}-capture", f"{name}-end"])
                return outcome

            patcher = patch.object(care, "_run_care_check", side_effect=run_isolated)
            patcher.start()
            self.addCleanup(patcher.stop)
            return care

        medication = make_care("medication", "confirmed")
        inactivity = make_care("inactivity", "help")
        original_capture = self.capture_utterance.side_effect

        def capture(path):
            for target in (
                lambda: results.append(medication.run_medication_check("medication-event")),
                lambda: results.append(inactivity.run_inactivity_check("inactivity-event")),
            ):
                thread = threading.Thread(target=target, daemon=True)
                threads.append(thread)
                thread.start()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with self.voice.interaction_manager._state_lock:
                    if len(self.voice.interaction_manager._pending) == 2:
                        break
                time.sleep(0.005)
            else:
                self.fail("both care requests were not queued during capture")
            self.assertEqual(order, [])
            return original_capture(path)

        self.capture_utterance.side_effect = capture
        self.assertTrue(self.voice.run_session())
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())

        self.assertCountEqual(results, ["confirmed", "help"])
        self.assertEqual(len(order), 6)
        for name in ("medication", "inactivity"):
            self.assertEqual(order.count(f"{name}-capture"), 1)
            self.assertLess(order.index(f"{name}-start"), order.index(f"{name}-end"))
        first_name = order[0].removesuffix("-start")
        other_name = "inactivity" if first_name == "medication" else "medication"
        self.assertLess(order.index(f"{first_name}-end"),
                        order.index(f"{other_name}-start"))
        self.client.responses.create.assert_not_called()
        self.assertEqual(self.voice.history, [])
        self.assertEqual(self.voice.turn_index, 0)
        self.assert_session_end("stop_phrase")
        self.assert_audio_clean()

    def test_inactivity_help_ends_chat_without_opening_another_capture(self):
        self.transcriptions("오늘 산책했어요")
        care = VoiceConversation(
            self.client, self.voice.settings, self.store, self.lock,
            self.temporary.name,
            interaction_manager=self.voice.interaction_manager,
            care_prompt_service=self.voice.care_prompt_service,
        )
        result = []
        thread = [None]
        original_playback = self.run_audio.side_effect

        def playback(command, timeout):
            original_playback(command, timeout)
            if str(command[-1]).endswith("_response.mp3"):
                thread[0] = threading.Thread(
                    target=lambda: result.append(
                        care.run_inactivity_check("help-event")
                    ),
                    daemon=True,
                )
                thread[0].start()
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    with self.voice.interaction_manager._state_lock:
                        if self.voice.interaction_manager._pending:
                            return
                    time.sleep(0.005)
                self.fail("help care request was not queued")

        self.run_audio.side_effect = playback
        with patch.object(care, "_run_care_check", return_value="help"):
            self.assertTrue(self.voice.run_session())
        thread[0].join(2)
        self.assertFalse(thread[0].is_alive())
        self.assertEqual(result, ["help"])
        self.assertEqual(self.capture_utterance.call_count, 1)
        self.assertEqual(self.voice.turn_index, 1)
        self.assertEqual(len(self.voice.history), 2)
        self.assert_session_end("care_help")
        self.assert_audio_clean()

    def test_standalone_stop_intents_never_reach_llm_or_history(self):
        for text in ("그만", "대화 종료", "이제 됐어", "여기까지"):
            with self.subTest(text=text):
                self.client.audio.transcriptions.create.return_value.text = text
                self.assertTrue(self.voice.run_session())
                self.assertEqual(self.voice.history, [])
                self.assertEqual(self.voice.turn_index, 0)
                self.assert_audio_clean()
        self.assertEqual(self.capture_utterance.call_count, 4)
        self.client.responses.create.assert_not_called()
        self.store.write_message.assert_not_called()

    def test_polite_stop_variants_accept_punctuation_and_spacing(self):
        for text in (
            "  도란아, 이제 그만할게요.  ", "대화 종료해 주세요.",
            "이제 됐어요!", "여기까지 할게요.",
        ):
            with self.subTest(text=text):
                self.client.audio.transcriptions.create.return_value.text = text
                self.assertTrue(self.voice.run_session())
                self.assertEqual(self.voice.history, [])
                self.assert_audio_clean()
        self.assertEqual(self.capture_utterance.call_count, 4)
        self.client.responses.create.assert_not_called()
        self.store.write_message.assert_not_called()

    def test_stop_words_inside_other_sentences_do_not_end_session(self):
        self.voice.settings = replace(self.voice.settings, session_max_turns=1)
        for text in (
            "대화 종료 버튼은 어디 있나요?", "이제 됐어라는 말은 무슨 뜻이야?",
            "여기까지 걸어왔어요", "그만두고 싶다는 친구가 있어요",
            "그만하지 말고 계속 이야기해요",
        ):
            with self.subTest(text=text):
                self.client.audio.transcriptions.create.return_value.text = text
                self.assertTrue(self.voice.run_session())
                self.assertEqual(self.voice.history[0], {"role": "user", "content": text})
                self.assertEqual(self.client.responses.create.call_args.kwargs["input"], [
                    {"role": "user", "content": text},
                ])
                self.assert_audio_clean()
        self.assertEqual(self.client.responses.create.call_count, 5)

    def test_initial_consecutive_no_speech_returns_false_after_configured_limit(self):
        self.voice.settings = replace(self.voice.settings, session_max_no_speech=3)
        self.capture_utterance.side_effect = None
        self.capture_utterance.return_value = False
        self.assertFalse(self.voice.run_session())
        self.assertEqual(self.capture_utterance.call_count, 3)
        self.client.audio.transcriptions.create.assert_not_called()
        self.client.responses.create.assert_not_called()
        self.store.write_message.assert_not_called()
        self.client.audio.speech.with_streaming_response.create.assert_called_once()
        self.assert_session_end("no_speech")
        self.assert_audio_clean()

    def test_rejected_recordings_are_bounded_without_stt(self):
        self.has_meaningful_speech.return_value = False
        self.assertFalse(self.voice.run_session())
        self.assertEqual(self.capture_utterance.call_count, 2)
        self.client.audio.transcriptions.create.assert_not_called()
        self.client.responses.create.assert_not_called()
        self.store.write_message.assert_not_called()
        self.assert_session_end("no_speech")
        self.assert_audio_clean()

    def test_empty_transcriptions_are_bounded_without_llm(self):
        self.client.audio.transcriptions.create.return_value.text = " \n "
        self.assertFalse(self.voice.run_session())
        self.assertEqual(self.client.audio.transcriptions.create.call_count, 2)
        self.client.responses.create.assert_not_called()
        self.store.write_message.assert_not_called()
        self.assertEqual(self.voice.history, [])
        self.assert_session_end("no_speech")
        self.assert_audio_clean()

    def test_success_resets_no_speech_count_and_later_silence_returns_true(self):
        capture = self.capture_utterance.side_effect
        attempts = iter((False, True, False, True, False, False))
        self.capture_utterance.side_effect = lambda path: capture(path) if next(attempts) else False
        self.assertTrue(self.voice.run_session())
        self.assertEqual(self.capture_utterance.call_count, 6)
        self.assertEqual(self.client.responses.create.call_count, 2)
        self.assertEqual(len(self.voice.history), 4)
        self.assert_session_end("no_speech")
        self.assert_audio_clean()

    def test_maximum_turns_exits_without_an_extra_recording(self):
        self.voice.settings = replace(self.voice.settings, session_max_turns=3)
        self.assertTrue(self.voice.run_session())
        self.assertEqual(self.capture_utterance.call_count, 3)
        self.assertEqual(self.client.responses.create.call_count, 3)
        self.assertEqual(self.voice.turn_index, 3)
        self.assertEqual(self.store.write_message.call_count, 6)
        self.assert_session_end("max_turns")
        self.assert_audio_clean()

    def test_each_dashboard_session_starts_with_fresh_id_and_history(self):
        self.voice.settings = replace(self.voice.settings, session_max_turns=1)
        self.assertTrue(self.voice.run_session())
        first_session = self.voice.session_id
        self.assertTrue(self.voice.run_session())
        self.assertNotEqual(first_session, self.voice.session_id)
        self.assertEqual(self.voice.turn_index, 1)
        self.assertEqual(len(self.voice.history), 2)
        self.assertEqual([
            len(call.kwargs["input"]) for call in self.client.responses.create.call_args_list
        ], [1, 1])
        records = [call.kwargs for call in self.store.write_message.call_args_list]
        self.assertEqual([record["session_id"] for record in records],
                         [first_session] * 2 + [self.voice.session_id] * 2)
        self.assert_audio_clean()

    def test_active_session_keeps_history_even_after_idle_threshold(self):
        self.voice.settings = replace(self.voice.settings, session_max_turns=2,
                                      session_idle_seconds=1)
        real_turn = self.voice.run_turn

        def delayed_turn(**kwargs):
            self.voice.last_activity -= 301
            return real_turn(**kwargs)

        with patch.object(self.voice, "run_turn", side_effect=delayed_turn):
            self.assertTrue(self.voice.run_session())
        self.assertEqual(len(self.client.responses.create.call_args.kwargs["input"]), 3)
        self.assertEqual(len(self.voice.history), 4)
        self.assertEqual(len({call.kwargs["session_id"]
                              for call in self.store.write_message.call_args_list}), 1)
        self.assert_session_end("max_turns")

    def test_error_on_later_turn_propagates_without_another_attempt(self):
        self.client.responses.create.side_effect = [
            Mock(output_text="어디로 다녀오셨어요?", status="completed"),
            TimeoutError("private API details"),
        ]
        with self.assertRaises(TimeoutError):
            self.voice.run_session()
        self.assertEqual(self.capture_utterance.call_count, 2)
        self.assertEqual(self.client.responses.create.call_count, 2)
        self.assertEqual(len(self.voice.history), 2)
        self.assert_session_end("error")
        self.assertNotIn("private API details", self.output.getvalue())
        self.assert_audio_clean()

    def test_cancellation_during_followup_capture_releases_lock_and_cleans_audio(self):
        capture = self.capture_utterance.side_effect

        def stop_second_capture(path):
            result = capture(path)
            if self.capture_utterance.call_count == 2:
                self.voice.stop_event.set()
                self.voice.check_stopped()
            return result

        self.capture_utterance.side_effect = stop_second_capture
        with self.assertRaises(ConversationStopped):
            self.voice.run_session()
        self.assertEqual(self.capture_utterance.call_count, 2)
        self.client.responses.create.assert_called_once()
        self.assertEqual(len(self.voice.history), 2)
        self.assert_session_end("cancelled")
        self.assert_audio_clean()


class SessionSettingsTests(unittest.TestCase):
    def test_session_defaults_and_environment_overrides(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = VoiceSettings.from_env()
        self.assertEqual((settings.session_max_turns, settings.session_max_no_speech), (5, 2))
        for turns, no_speech in ((1, 1), (20, 5), (7, 3)):
            with self.subTest(turns=turns, no_speech=no_speech), patch.dict(os.environ, {
                "AI_SESSION_MAX_TURNS": str(turns),
                "AI_SESSION_MAX_NO_SPEECH": str(no_speech),
            }, clear=True):
                settings = VoiceSettings.from_env()
                self.assertEqual((settings.session_max_turns, settings.session_max_no_speech),
                                 (turns, no_speech))

    def test_session_limits_reject_invalid_bounds_and_nonintegers(self):
        for name, values in {
            "AI_SESSION_MAX_TURNS": ("0", "21", "-1", "1.5", "nan", "inf", "oops"),
            "AI_SESSION_MAX_NO_SPEECH": ("0", "6", "-1", "1.5", "nan", "inf", "oops"),
        }.items():
            for value in values:
                with self.subTest(name=name, value=value), \
                        patch.dict(os.environ, {name: value}, clear=True), \
                        self.assertRaises(ValueError):
                    VoiceSettings.from_env()


if __name__ == "__main__":
    unittest.main()
