"""Real session/controller paths with mocked microphone, network, and player edges."""

import io
import os
import subprocess
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
import voice_ai
from display_state import DisplayState
from test_main_regression import load_main


class VoiceDisplayTests(unittest.TestCase):
    def setUp(self):
        quiet = redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.state = DisplayState()
        state_patch = patch.object(voice_ai, "DISPLAY", self.state)
        state_patch.start()
        self.addCleanup(state_patch.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.client = Mock()
        self.store = Mock()
        self.voice = voice_ai.VoiceConversation(
            self.client, replace(voice_ai.VoiceSettings(), session_max_turns=1),
            self.store, threading.Lock(), temporary.name,
        )
        self.observed = []

        def record(operation, expected, audio_locked=False):
            state = self.state.snapshot()
            self.assertEqual(state["state"], expected, operation)
            self.assertTrue(state["busy"])
            self.assertEqual(self.voice.audio_lock.locked(), audio_locked, operation)
            self.observed.append(operation)

        def synthesize(text, path):
            record("tts", "thinking")
            path.write_bytes(b"mp3")

        def playback(*args):
            record("playback", "speaking", True)

        def capture(path):
            record("capture", "listening", True)
            path.write_bytes(b"wav")
            return True

        def transcribe(**kwargs):
            record("stt", "thinking")
            return Mock(text="괜찮아요")

        def respond(**kwargs):
            record("llm", "thinking")
            return Mock(output_text="다행이에요.", status="completed")

        self.voice.synthesize_speech = Mock(side_effect=synthesize)
        self.voice.run_audio = Mock(side_effect=playback)
        self.voice.capture_utterance = Mock(side_effect=capture)
        self.voice.has_meaningful_speech = Mock(return_value=True)
        self.client.audio.transcriptions.create.side_effect = transcribe
        self.client.responses.create.side_effect = respond

    def assert_idle(self):
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["state"], "idle")
        self.assertFalse(snapshot["busy"])
        self.assertIsNone(snapshot["alert"])
        self.assertFalse(self.voice.audio_lock.locked())

    def test_session_states_surround_real_operations_and_preserve_outbox(self):
        self.assert_idle()
        self.assertTrue(self.voice.run_session())
        self.assertEqual(self.observed,
                         ["tts", "playback", "capture", "stt", "llm", "tts", "playback"])
        records = [call.kwargs for call in self.store.write_message.call_args_list]
        self.assertEqual([record["role"] for record in records], ["user", "assistant"])
        self.assertEqual({record["session_id"] for record in records}, {self.voice.session_id})
        self.assertEqual([record["audio_status"] for record in records], ["recorded", "played"])
        self.assertTrue(all(record["flush"] is False for record in records))
        self.assert_idle()

    def test_each_operation_error_restores_idle_and_audio_lock(self):
        for operation in (self.voice.synthesize_speech, self.voice.run_audio,
                          self.voice.capture_utterance,
                          self.client.audio.transcriptions.create, self.client.responses.create):
            with self.subTest(operation=operation):
                previous = operation.side_effect
                operation.side_effect = RuntimeError("private detail")
                try:
                    with self.assertRaises(RuntimeError):
                        self.voice.run_session()
                    self.assert_idle()
                finally:
                    operation.side_effect = previous

    def test_cancellation_during_capture_restores_idle(self):
        self.voice.capture_utterance.side_effect = voice_ai.ConversationStopped()
        with self.assertRaises(voice_ai.ConversationStopped):
            self.voice.run_session()
        self.assert_idle()

    def test_no_speech_restores_idle_without_llm(self):
        self.voice.capture_utterance.side_effect = None
        self.voice.capture_utterance.return_value = False
        self.assertFalse(self.voice.run_session())
        self.client.responses.create.assert_not_called()
        self.assert_idle()

    def test_stop_phrase_returns_idle_without_new_reply(self):
        self.client.audio.transcriptions.create.side_effect = None
        self.client.audio.transcriptions.create.return_value = Mock(text="그만")
        self.assertTrue(self.voice.run_session())
        self.client.responses.create.assert_not_called()
        self.assert_idle()

    def test_care_card_lives_through_prompt_capture_and_stt(self):
        for operation in (self.voice.synthesize_speech, self.voice.run_audio,
                          self.voice.capture_utterance, self.client.audio.transcriptions.create):
            original = operation.side_effect

            def checked(*args, _original=original, **kwargs):
                self.assertEqual(self.state.snapshot()["alert"]["kind"], "inactivity")
                return _original(*args, **kwargs)

            operation.side_effect = checked
        self.assertEqual(self.voice.run_inactivity_check("source"), "okay")
        self.assertEqual(self.observed, ["tts", "playback", "capture", "stt"])
        self.client.responses.create.assert_not_called()
        self.assert_idle()

    def test_care_failure_clears_card(self):
        self.voice.capture_utterance.side_effect = RuntimeError("mic failed")
        with self.assertRaises(RuntimeError):
            self.voice.run_inactivity_check("source")
        self.assert_idle()


class GameVoiceTests(unittest.TestCase):
    def setUp(self):
        quiet = redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.state = DisplayState()
        state_patch = patch.object(voice_ai, "DISPLAY", self.state)
        state_patch.start()
        self.addCleanup(state_patch.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.client = Mock()
        self.voice = voice_ai.VoiceConversation(
            self.client, voice_ai.VoiceSettings(), Mock(), threading.Lock(), temporary.name,
        )
        self.controller = voice_ai.ConversationController(self.voice)

    def assert_idle(self):
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["state"], "idle")
        self.assertIsNone(snapshot["activity"])
        self.assertFalse(snapshot["busy"])
        self.assertFalse(self.voice.audio_lock.locked())

    def test_static_cue_reuses_tts_cache_and_shared_audio_focus_path(self):
        observed = []

        def synthesize(text, path):
            snapshot = self.state.snapshot()
            self.assertIsNone(snapshot["activity"])
            self.assertEqual(snapshot["state"], "idle")
            self.assertFalse(snapshot["busy"])
            acquired = self.voice.interaction_lock.acquire(blocking=False)
            self.assertTrue(acquired)
            if acquired:
                self.voice.interaction_lock.release()
            self.assertFalse(self.voice.audio_lock.locked())
            observed.append(("tts", text))
            path.write_bytes(b"mp3")

        def playback(command, timeout, **kwargs):
            snapshot = self.state.snapshot()
            self.assertEqual(snapshot["activity"], "game")
            self.assertEqual(snapshot["state"], "speaking")
            self.assertTrue(self.voice.audio_lock.locked())
            self.assertEqual(command[:5], ["mpg123", "-o", "alsa", "-a",
                                           self.voice.settings.playback_device])
            self.assertFalse(kwargs["log_timing"])
            self.assertIsNotNone(kwargs["cancel_event"])
            observed.append(("play", Path(command[-1]).name, timeout))

        self.voice.synthesize_speech = Mock(side_effect=synthesize)
        self.voice.run_audio = Mock(side_effect=playback)
        self.assertEqual(self.controller.play_game_cue("start", "game-1"), "played")
        self.assertEqual(self.controller.play_game_cue("start", "game-1"), "played")
        self.assertEqual(self.voice.synthesize_speech.call_count, 1)
        self.assertEqual(self.voice.run_audio.call_count, 2)
        self.assertEqual(observed[0], ("tts", voice_ai.GAME_VOICE_CUES["start"]))
        self.assert_idle()

    def test_radio_pause_ack_precedes_only_physical_game_playback(self):
        synthesized = threading.Event()
        played = threading.Event()
        self.voice.synthesize_speech = Mock(side_effect=lambda _text, path: (
            synthesized.set(), path.write_bytes(b"mp3"),
        ))
        self.voice.run_audio = Mock(side_effect=lambda *_args, **_kwargs: played.set())
        self.state.RADIO_PAUSE_TIMEOUT_SECONDS = 2
        self.assertTrue(self.state.request_radio(True))
        result = []
        worker = threading.Thread(
            target=lambda: result.append(self.controller.play_game_cue("input", "game-2")),
        )
        worker.start()
        try:
            self.assertTrue(synthesized.wait(1))
            deadline = time.monotonic() + 1
            while not self.state.snapshot()["audio_focus"]["blocked"]:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.01)
            self.assertFalse(played.is_set())
            self.assertTrue(self.state.request_radio(False))
            self.assertTrue(played.wait(1))
        finally:
            self.state.request_radio(False)
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, ["played"])
        self.assert_idle()

    def test_matching_page_cancel_stops_playback_and_wrong_session_cannot(self):
        playing = threading.Event()

        def synthesize(_text, path):
            path.write_bytes(b"mp3")

        def playback(_command, _timeout, *, cancel_event, **_kwargs):
            playing.set()
            while not cancel_event.wait(.01):
                pass
            self.voice.check_stopped(cancel_event)

        self.voice.synthesize_speech = Mock(side_effect=synthesize)
        self.voice.run_audio = Mock(side_effect=playback)
        result = []
        worker = threading.Thread(
            target=lambda: result.append(self.controller.play_game_cue("correct", "game-3")),
        )
        worker.start()
        self.assertTrue(playing.wait(1))
        self.assertEqual(self.controller.cancel_game_voice("other-game"), "idle")
        self.assertTrue(worker.is_alive())
        self.assertEqual(self.controller.cancel_game_voice("game-3"), "accepted")
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, ["cancelled"])
        self.assert_idle()

    def test_cancel_during_cold_tts_never_holds_care_or_audio_focus(self):
        synthesizing = threading.Event()
        finish = threading.Event()

        def synthesize(_text, path):
            synthesizing.set()
            finish.wait(2)
            path.write_bytes(b"mp3")

        self.voice.synthesize_speech = Mock(side_effect=synthesize)
        self.voice.run_audio = Mock()
        result = []
        worker = threading.Thread(
            target=lambda: result.append(
                self.controller.play_game_cue("start", "cold-game")
            ),
        )
        worker.start()
        self.assertTrue(synthesizing.wait(1))
        self.assert_idle()
        acquired = self.voice.interaction_lock.acquire(blocking=False)
        self.assertTrue(acquired)
        if acquired:
            self.voice.interaction_lock.release()
        self.assertEqual(self.controller.cancel_game_voice("cold-game"), "accepted")
        self.assert_idle()
        finish.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, ["cancelled"])
        self.voice.run_audio.assert_not_called()
        self.assert_idle()

    def test_non_game_focus_during_cold_tts_prevents_late_playback(self):
        synthesizing = threading.Event()
        finish = threading.Event()

        def synthesize(_text, path):
            synthesizing.set()
            finish.wait(2)
            path.write_bytes(b"mp3")

        self.voice.synthesize_speech = Mock(side_effect=synthesize)
        self.voice.run_audio = Mock()
        result = []
        worker = threading.Thread(
            target=lambda: result.append(
                self.controller.play_game_cue("input", "care-race")
            ),
        )
        worker.start()
        self.assertTrue(synthesizing.wait(1))
        with self.state.activity("medication"):
            pass
        finish.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, ["interrupted"])
        self.voice.run_audio.assert_not_called()
        self.assert_idle()

    def test_new_game_session_cancels_stale_voice_before_returning_busy(self):
        playing = threading.Event()

        def synthesize(_text, path):
            path.write_bytes(b"mp3")

        def playback(_command, _timeout, *, cancel_event, **_kwargs):
            playing.set()
            cancel_event.wait(1)
            self.voice.check_stopped(cancel_event)

        self.voice.synthesize_speech = Mock(side_effect=synthesize)
        self.voice.run_audio = Mock(side_effect=playback)
        result = []
        worker = threading.Thread(
            target=lambda: result.append(self.controller.play_game_cue("start", "old-game")),
        )
        worker.start()
        self.assertTrue(playing.wait(1))
        self.assertEqual(self.controller.play_game_cue("start", "new-game"), "game_busy")
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, ["cancelled"])
        self.assert_idle()

    def test_busy_or_invalid_cue_never_synthesizes_and_failure_restores_state(self):
        self.voice.synthesize_speech = Mock()
        self.voice.run_audio = Mock()
        with self.state.activity("conversation"):
            self.assertEqual(self.controller.play_game_cue("input", "game-4"),
                             "interrupted")
        self.assertEqual(self.controller.play_game_cue("not-allowed", "game-4"), "unavailable")
        self.voice.synthesize_speech.side_effect = RuntimeError("private detail")
        self.assertEqual(self.controller.play_game_cue("incorrect", "game-4"), "failed")
        self.voice.run_audio.assert_not_called()
        self.assert_idle()

    def test_broken_nonempty_cache_is_removed_for_one_clean_regeneration(self):
        def synthesize(_text, path):
            path.write_bytes(b"mp3")

        self.voice.synthesize_speech = Mock(side_effect=synthesize)
        self.voice.run_audio = Mock(side_effect=subprocess.CalledProcessError(
            1, ["mpg123"],
        ))
        cache = self.controller._game_cache_path(
            "complete", voice_ai.GAME_VOICE_CUES["complete"],
        )
        self.assertEqual(self.controller.play_game_cue("complete", "broken-1"),
                         "failed")
        self.assertFalse(cache.exists())
        self.voice.run_audio.side_effect = None
        self.assertEqual(self.controller.play_game_cue("complete", "broken-2"),
                         "played")
        self.assertEqual(self.voice.synthesize_speech.call_count, 2)
        self.assertTrue(cache.is_file())
        self.assert_idle()


class LocalTouchTests(unittest.TestCase):
    def setUp(self):
        self.state = DisplayState()
        state_patch = patch.object(voice_ai, "DISPLAY", self.state)
        state_patch.start()
        self.addCleanup(state_patch.stop)
        self.voice = Mock(stop_event=threading.Event())
        self.commands = Mock(path="commands/test", poll_seconds=5)
        self.commands.run_once.return_value = False
        self.controller = voice_ai.ConversationController(self.voice, self.commands)
        self.controller.enable_local_touch()

    def test_touch_disabled_without_display_and_in_terminal_mode(self):
        for commands in (None, self.commands):
            controller = voice_ai.ConversationController(self.voice, commands)
            self.assertFalse(controller.touch_available())
            self.assertEqual(controller.request_local_session(), "unavailable")
        terminal = voice_ai.ConversationController(self.voice)
        terminal.enable_local_touch()
        self.assertEqual(terminal.request_local_session(), "unavailable")

    def test_simultaneous_touches_reserve_exactly_one_request(self):
        barrier = threading.Barrier(9)
        results = []

        def touch():
            barrier.wait(timeout=2)
            results.append(self.controller.request_local_session())

        threads = [threading.Thread(target=touch) for _ in range(8)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(results.count("accepted"), 1)
        self.assertEqual(results.count("busy"), 7)
        self.assertFalse(self.controller.touch_available())

    def test_busy_care_medication_and_chat_reject_touch(self):
        for kind in ("inactivity", "medication", "conversation"):
            with self.state.activity(kind):
                self.assertFalse(self.controller.touch_available())
                self.assertEqual(self.controller.request_local_session(), "busy")
        self.assertTrue(self.controller.touch_available())

    def test_same_worker_handles_touch_then_preserves_firebase_callback(self):
        self.assertEqual(self.controller.request_local_session(), "accepted")
        calls = []

        def session():
            calls.append(threading.current_thread().name)
            self.assertEqual(self.controller.request_local_session(), "busy")

        def firebase(callback):
            self.assertEqual(callback, self.voice.run_session)
            self.assertEqual(len(calls), 1)
            self.voice.stop_event.set()
            return False

        self.voice.run_session.side_effect = session
        self.commands.run_once.side_effect = firebase
        self.controller.start()
        try:
            self.assertTrue(self.controller.finished.wait(2))
        finally:
            self.controller.stop()
        self.assertEqual(calls, ["firebase-voice-conversation"])
        self.voice.run_turn.assert_not_called()
        self.commands.run_once.assert_called_once()
        self.assertEqual(self.controller.request_local_session(), "unavailable")

    def test_live_worker_notices_touch_between_firebase_polls(self):
        polled = threading.Event()
        started = threading.Event()
        self.commands.run_once.side_effect = lambda callback: polled.set() and False
        self.voice.run_session.side_effect = started.set
        self.controller.start()
        try:
            self.assertTrue(polled.wait(2))
            self.assertEqual(self.controller.request_local_session(), "accepted")
            self.assertTrue(started.wait(2), "Touch waited for the five-second Firebase interval")
        finally:
            self.controller.stop()

    def test_error_releases_touch_slot_and_sanitizes_details(self):
        self.voice.run_session.side_effect = RuntimeError("private-account-detail")
        self.assertEqual(self.controller.request_local_session(), "accepted")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertTrue(self.controller._run_local_session())
        self.assertNotIn("private-account-detail", output.getvalue())
        self.assertTrue(self.controller.touch_available())
        self.assertEqual(self.controller.request_local_session(), "accepted")

    def test_shutdown_cancels_pending_touch(self):
        self.assertEqual(self.controller.request_local_session(), "accepted")
        self.voice.stop_event.set()
        self.controller._run_firebase()
        self.voice.run_session.assert_not_called()
        self.assertFalse(self.controller._local_pending)
        self.assertEqual(self.controller.request_local_session(), "unavailable")


class MainDisplayTests(unittest.TestCase):
    def setUp(self):
        self.hub = load_main()
        self.hub.DISPLAY = DisplayState()

    def test_default_does_not_start_server(self):
        with patch.dict(os.environ, {}, clear=True), patch("display_server.DisplayServer") as server:
            self.assertIsNone(self.hub.start_display())
        server.assert_not_called()

    def test_server_start_enables_touch_only_after_success(self):
        controller = Mock()
        with patch.dict(os.environ, {"DISPLAY_ENABLED": "true"}, clear=True), \
                patch("display_server.DisplayServer") as server:
            result = self.hub.start_display(controller)
        server.assert_called_once_with(state=self.hub.DISPLAY, controller=controller,
                                       host="127.0.0.1", port=8765)
        self.assertIs(result, server.return_value.start.return_value)
        controller.enable_local_touch.assert_called_once()

    def test_invalid_configured_port_does_not_start_server_or_touch(self):
        for port in ("0", "-1", "65536", "invalid"):
            controller = Mock()
            with self.subTest(port=port), \
                    patch.dict(os.environ, {"DISPLAY_ENABLED": "true", "DISPLAY_PORT": port}), \
                    patch("display_server.DisplayServer") as server:
                self.assertIsNone(self.hub.start_display(controller))
            server.assert_not_called()
            controller.enable_local_touch.assert_not_called()

    def test_bind_failure_leaves_hub_care_and_scheduler_running(self):
        controller = Mock()
        care = Mock()
        with patch.dict(os.environ, {"DISPLAY_ENABLED": "true"}, clear=True), \
                patch("display_server.DisplayServer", side_effect=OSError("occupied")), \
                patch.object(self.hub, "start_ai_conversation", return_value=controller), \
                patch.object(self.hub, "start_inactivity_care", return_value=care), \
                patch.object(self.hub, "check_medication_schedule_once") as schedule, \
                patch.object(self.hub.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.hub.run_hub_mode()
        schedule.assert_called_once()
        controller.enable_local_touch.assert_not_called()
        controller.stop.assert_called_once()
        care.stop.assert_called_once()

    def test_display_stops_on_hub_shutdown(self):
        server = Mock()
        with patch.object(self.hub, "start_display", return_value=server), \
                patch.object(self.hub, "start_ai_conversation", return_value=None), \
                patch.object(self.hub, "start_inactivity_care", return_value=None), \
                patch.object(self.hub, "check_medication_schedule_once"), \
                patch.object(self.hub, "check_inactivity_events_once"), \
                patch.object(self.hub.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.hub.run_hub_mode()
        server.stop.assert_called_once()

    def test_medication_card_and_actual_generation_playback_phases(self):
        state = self.hub.DISPLAY

        def synthesize(path):
            self.assertEqual(state.snapshot()["alert"]["kind"], "medication")
            self.assertEqual(state.snapshot()["state"], "thinking")
            self.assertFalse(self.hub.AUDIO_LOCK.locked())

        def play(*args, **kwargs):
            self.assertEqual(state.snapshot()["alert"]["kind"], "medication")
            self.assertEqual(state.snapshot()["state"], "speaking")
            self.assertTrue(self.hub.AUDIO_LOCK.locked())

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(self.hub, "AUDIO_DIR", Path(directory)), \
                patch.object(self.hub, "gTTS", return_value=Mock(save=Mock(side_effect=synthesize))), \
                patch.object(self.hub.subprocess, "run", side_effect=play), \
                patch.object(self.hub, "write_medication_event") as write:
            self.hub.trigger_medication_reminder()
        write.assert_called_once()
        self.assertIsNone(state.snapshot()["alert"])
        self.assertEqual(state.snapshot()["state"], "idle")
        self.assertFalse(state.snapshot()["busy"])

    def test_legacy_alert_audio_failure_keeps_event_and_clears_display(self):
        for trigger, args, event_method, kind in (
            (self.hub.trigger_medication_reminder, (), "write_medication_event", "medication"),
            (self.hub.trigger_inactivity_voice_check, ("source",),
             "write_inactivity_voice_check_event", "inactivity"),
        ):
            def failed(*args):
                self.assertEqual(self.hub.DISPLAY.snapshot()["alert"]["kind"], kind)
                return False

            with patch.object(self.hub, "play_tts_message", side_effect=failed), \
                    patch.object(self.hub, event_method) as write:
                trigger(*args)
            write.assert_called_once_with(*args)
            self.assertFalse(self.hub.DISPLAY.snapshot()["busy"])
            self.assertIsNone(self.hub.DISPLAY.snapshot()["alert"])


if __name__ == "__main__":
    unittest.main()
