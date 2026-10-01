"""Offline regression checks for the established Voice Hub modes.

No microphone, player, API key, network, or third-party test runner is needed.
The imported main module gets isolated I/O adapters; all effects are mocked.
"""

import importlib.util
import builtins
import io
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch


HUB_DIR = Path(__file__).resolve().parents[1] / "raspberry-pi-voice-hub"


def load_main():
    requests = types.ModuleType("requests")
    requests.RequestException = type("RequestException", (Exception,), {})
    requests.get = Mock()
    requests.patch = Mock()
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = Mock()
    gtts = types.ModuleType("gtts")
    gtts.gTTS = Mock()
    spec = importlib.util.spec_from_file_location("voice_hub_regression_main", HUB_DIR / "main.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"requests": requests, "dotenv": dotenv, "gtts": gtts}), \
            patch.dict("os.environ", {"OPENAI_API_KEY": "", "AI_CONVERSATION_ENABLED": "false"}), \
            patch.object(sys, "path", [str(HUB_DIR), *sys.path]):
        spec.loader.exec_module(module)
    return module


hub = load_main()


class MainRegressionTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.addCleanup(self.output.close)
        self.quiet = redirect_stdout(self.output)
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)

    def test_existing_modes_route_without_starting_ai(self):
        routes = {
            "demo": "run_demo_mode",
            "schedule": "run_schedule_mode",
            "watch": "run_watch_mode",
            "hub": "run_hub_mode",
            "unknown": "run_demo_mode",
        }
        for mode, expected in routes.items():
            with self.subTest(mode=mode), patch.object(hub, "REMINDER_MODE", mode), \
                    patch.multiple(hub, run_demo_mode=Mock(), run_schedule_mode=Mock(),
                                   run_watch_mode=Mock(), run_hub_mode=Mock()), \
                    patch.object(hub, "start_ai_conversation", create=True) as start_ai:
                hub.main()
                for name in set(routes.values()):
                    self.assertEqual(getattr(hub, name).call_count, int(name == expected))
                start_ai.assert_not_called()

    def test_chat_mode_routes_to_explicit_conversation_entrypoint(self):
        with patch.object(hub, "REMINDER_MODE", "chat"), \
                patch.object(hub, "run_chat_mode") as chat, \
                patch.object(hub, "run_demo_mode") as demo:
            hub.main()
        chat.assert_called_once_with()
        demo.assert_not_called()

    def test_disabled_ai_does_not_import_optional_voice_or_openai_modules(self):
        original_import = builtins.__import__

        def import_without_ai(name, *args, **kwargs):
            self.assertNotIn(name.split(".")[0], {"voice_ai", "openai"},
                             "Disabled AI must not need any optional AI dependency")
            return original_import(name, *args, **kwargs)

        with patch.dict("os.environ", {"AI_CONVERSATION_ENABLED": "false"}), \
                patch.object(builtins, "__import__", side_effect=import_without_ai):
            self.assertIsNone(hub.start_ai_conversation())

    def test_missing_ai_configuration_leaves_hub_checks_running_and_exits_cleanly(self):
        optional_voice = types.ModuleType("voice_ai")
        optional_voice.create_controller = Mock()
        optional_voice.create_firebase_controller = Mock(
            side_effect=ValueError("OPENAI_API_KEY is missing"))
        with patch.dict(sys.modules, {"voice_ai": optional_voice}), \
                patch.dict("os.environ", {"AI_CONVERSATION_ENABLED": "true", "OPENAI_API_KEY": "",
                                          "AI_CONVERSATION_TRIGGER": "auto"}, clear=True), \
                patch.object(hub.sys, "stdin", None):
            self.assertIsNone(hub.start_ai_conversation())
            optional_voice.create_firebase_controller.assert_called_once()
            optional_voice.create_firebase_controller.reset_mock()
            with patch.object(hub, "check_medication_schedule_once", return_value="2026-09-08") as schedule, \
                    patch.object(hub, "check_inactivity_events_once") as watch, \
                    patch.object(hub.time, "sleep", side_effect=KeyboardInterrupt):
                # None from a failed AI startup must not break hub startup or cleanup.
                with self.assertRaises(KeyboardInterrupt):
                    hub.run_hub_mode()
            optional_voice.create_firebase_controller.assert_called_once()
            optional_voice.create_controller.assert_not_called()
            schedule.assert_called_once()
            watch.assert_not_called()

    def test_trigger_selection_starts_only_the_selected_controller(self):
        cases = [
            (None, True, "terminal"), (None, False, "firebase"),
            ("auto", True, "terminal"), ("auto", False, "firebase"),
            ("terminal", False, "terminal"), ("firebase", True, "firebase"),
            (" FIREBASE ", True, "firebase"),
        ]
        for trigger, is_tty, expected in cases:
            with self.subTest(trigger=trigger, is_tty=is_tty):
                optional_voice = types.ModuleType("voice_ai")
                optional_voice.create_controller = Mock()
                optional_voice.create_firebase_controller = Mock()
                env = {"AI_CONVERSATION_ENABLED": "true"}
                if trigger is not None:
                    env["AI_CONVERSATION_TRIGGER"] = trigger
                with patch.dict(sys.modules, {"voice_ai": optional_voice}), \
                        patch.dict("os.environ", env, clear=True), \
                        patch.object(hub.sys, "stdin", Mock(isatty=Mock(return_value=is_tty))):
                    result = hub.start_ai_conversation()
                selected = (optional_voice.create_controller if expected == "terminal"
                            else optional_voice.create_firebase_controller)
                other = (optional_voice.create_firebase_controller if expected == "terminal"
                         else optional_voice.create_controller)
                selected.assert_called_once_with(
                    hub.FIREBASE_DATABASE_URL, hub.ELDER_ID, hub.DEVICE_ID,
                    hub.LOCATION_NAME, hub.AUDIO_LOCK, hub.AUDIO_DIR)
                selected.return_value.start.assert_called_once_with()
                other.assert_not_called()
                self.assertIs(result, selected.return_value.start.return_value)

    def test_auto_falls_back_to_firebase_when_stdin_is_unavailable(self):
        for stdin in (None, object(), Mock(isatty=Mock(side_effect=OSError("closed")))):
            with self.subTest(stdin=type(stdin).__name__):
                optional_voice = types.ModuleType("voice_ai")
                optional_voice.create_firebase_controller = Mock()
                with patch.dict(sys.modules, {"voice_ai": optional_voice}), \
                        patch.dict("os.environ", {"AI_CONVERSATION_ENABLED": "true"}, clear=True), \
                        patch.object(hub.sys, "stdin", stdin):
                    result = hub.start_ai_conversation()
                optional_voice.create_firebase_controller.assert_called_once()
                self.assertIs(result, optional_voice.create_firebase_controller.return_value.start.return_value)

    def test_invalid_trigger_fails_without_creating_a_controller(self):
        optional_voice = types.ModuleType("voice_ai")
        optional_voice.create_controller = Mock()
        optional_voice.create_firebase_controller = Mock()
        with patch.dict(sys.modules, {"voice_ai": optional_voice}), \
                patch.dict("os.environ", {"AI_CONVERSATION_ENABLED": "true",
                                          "AI_CONVERSATION_TRIGGER": "invalid"}, clear=True):
            self.assertIsNone(hub.start_ai_conversation())
        optional_voice.create_controller.assert_not_called()
        optional_voice.create_firebase_controller.assert_not_called()

    def test_chat_forces_terminal_even_when_hub_trigger_is_firebase_and_ai_disabled(self):
        optional_voice = types.ModuleType("voice_ai")
        optional_voice.create_controller = Mock()
        optional_voice.create_firebase_controller = Mock()
        controller = optional_voice.create_controller.return_value.start.return_value
        controller.finished.wait.return_value = True
        with patch.dict(sys.modules, {"voice_ai": optional_voice}), \
                patch.dict("os.environ", {"AI_CONVERSATION_ENABLED": "false",
                                          "AI_CONVERSATION_TRIGGER": "firebase"}, clear=True):
            hub.run_chat_mode()
        optional_voice.create_controller.assert_called_once()
        optional_voice.create_firebase_controller.assert_not_called()
        controller.finished.wait.assert_called_once_with(0.2)
        controller.stop.assert_called_once_with()

    def test_demo_still_waits_then_sends_one_reminder(self):
        with patch.object(hub.time, "sleep") as sleep, \
                patch.object(hub, "trigger_medication_reminder") as remind:
            hub.run_demo_mode()
        sleep.assert_called_once_with(30)
        remind.assert_called_once_with()

    def test_medication_event_is_still_written_after_audio_failure(self):
        with patch.object(hub, "play_tts_message", return_value=False), \
                patch.object(hub, "write_medication_event") as write:
            hub.trigger_medication_reminder()
        write.assert_called_once_with()

    def test_inactivity_event_retains_source_after_audio_failure(self):
        with patch.object(hub, "play_tts_message", return_value=False), \
                patch.object(hub, "write_inactivity_voice_check_event") as write:
            hub.trigger_inactivity_voice_check("sensor_event_42")
        write.assert_called_once_with("sensor_event_42")

    def test_medication_schedule_deduplicates_and_resets_on_next_day(self):
        triggered = {}
        current_day = "2026-09-08"
        dates = [datetime(2026, 9, 8, 8, 0), datetime(2026, 9, 8, 8, 0, 30),
                 datetime(2026, 9, 9, 8, 0)]
        with patch.object(hub, "datetime") as clock, \
                patch.object(hub, "trigger_medication_reminder") as remind:
            clock.now.side_effect = dates
            for _ in dates:
                current_day = hub.check_medication_schedule_once(["08:00"], triggered, current_day)
        self.assertEqual(remind.call_count, 2)
        self.assertEqual(current_day, "2026-09-09")
        self.assertEqual(triggered, {"2026-09-09_08:00": True})

    def test_schedule_mode_polls_remote_provider_without_legacy_unclaimed_audio(self):
        provider, dispatcher, cursor = Mock(), Mock(), Mock()
        with patch.object(hub, "start_medication_schedule_provider", return_value=provider), \
                patch.object(hub, "start_medication_announcement_care", return_value=dispatcher), \
                patch.object(hub, "MedicationScheduleCursor", return_value=cursor), \
                patch.object(hub, "dispatch_medication_schedule_once") as dispatch, \
                patch.object(hub, "trigger_medication_reminder") as legacy, \
                patch.object(hub.time, "sleep", side_effect=[None, KeyboardInterrupt]) as sleep:
            with self.assertRaises(KeyboardInterrupt):
                hub.run_schedule_mode()
        self.assertEqual(dispatch.call_args_list, [
            unittest.mock.call(provider, cursor, dispatcher),
            unittest.mock.call(provider, cursor, dispatcher),
        ])
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 5])
        legacy.assert_not_called()
        dispatcher.stop.assert_called_once_with()
        provider.stop.assert_called_once_with()

    def test_schedule_ignores_unconfigured_minutes(self):
        with patch.object(hub, "datetime") as clock, \
                patch.object(hub, "trigger_medication_reminder") as remind:
            clock.now.return_value = datetime(2026, 9, 8, 8, 1)
            triggered = {}
            self.assertEqual(hub.check_medication_schedule_once(["08:00"], triggered, "2026-09-08"),
                             "2026-09-08")
        remind.assert_not_called()
        self.assertEqual(triggered, {})

    def test_watch_filter_keeps_only_new_open_unprocessed_inactivity_events(self):
        def event(timestamp, **extra):
            return {"type": "long_inactivity", "createdAt": timestamp, **extra}

        events = {
            "new": event(1_000_020),
            "boundary": event(990_000),
            "old": event(989_999),
            "resolved": event(1_000_001, status="resolved"),
            "duplicate": event(1_000_002),
            "wrong_timestamp": event("1000003"),
            "float_timestamp": event(1_000_004.0),
            "ai_message": event(1_000_005, type="ai_conversation_message"),
            "invalid": None,
        }
        found = hub.find_new_open_long_inactivity_events(events, {"duplicate"}, 1_000_000)
        self.assertEqual([key for key, _ in found], ["boundary", "new"])

    def test_inactivity_poll_marks_source_once_before_triggering(self):
        processed = set()
        events = {"sensor_1": {"type": "long_inactivity", "createdAt": 1_000_000}}

        def check_marked(source):
            self.assertIn(source, processed)

        with patch.object(hub, "fetch_events", return_value=events), \
                patch.object(hub, "trigger_inactivity_voice_check", side_effect=check_marked) as trigger:
            hub.check_inactivity_events_once(processed, 1_000_000)
            hub.check_inactivity_events_once(processed, 1_000_000)
        trigger.assert_called_once_with("sensor_1")

    def test_watch_mode_deduplicates_events_across_polls(self):
        events = {"sensor_1": {"type": "long_inactivity", "createdAt": 1_000_000}}
        with patch.object(hub, "now_ms", return_value=1_000_000), \
                patch.object(hub, "fetch_events", return_value=events), \
                patch.object(hub, "trigger_inactivity_voice_check") as trigger, \
                patch.object(hub.time, "sleep", side_effect=[None, KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                hub.run_watch_mode()
        trigger.assert_called_once_with("sensor_1")

    def test_inactivity_event_schema_keeps_link_to_original_sensor_event(self):
        with patch.object(hub, "now_ms", return_value=1_000_000), \
                patch.object(hub, "firebase_patch_and_verify", return_value=True) as write:
            self.assertTrue(hub.write_inactivity_voice_check_event("sensor_1"))
        event_id, event = write.call_args.args
        self.assertEqual(event_id, "pi_inactivity_voice_check_sent_1000000")
        self.assertEqual(event["type"], "inactivity_voice_check_sent")
        self.assertEqual(event["sourceEventId"], "sensor_1")
        self.assertEqual(event["createdAt"], 1_000_000)
        self.assertEqual(event["severity"], "warning")
        self.assertEqual(event["status"], "open")

    def test_reminder_locks_playback_but_leaves_synthesis_outside_audio_lock(self):
        lock = threading.Lock()

        def assert_locked(*args, **kwargs):
            self.assertTrue(lock.locked(), "Reminder audio must exclude concurrent recording/playback")

        def assert_unlocked(*args, **kwargs):
            self.assertFalse(lock.locked(), "Network synthesis must not reserve the microphone")

        speech = Mock()
        speech.save.side_effect = assert_unlocked
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(hub, "AUDIO_DIR", Path(directory)), \
                patch.object(hub, "AUDIO_LOCK", lock), \
                patch.object(hub, "gTTS", return_value=speech), \
                patch.object(hub.subprocess, "run", side_effect=assert_locked):
            self.assertTrue(hub.play_tts_message("reminder", "reminder.mp3"))
        self.assertFalse(lock.locked())

    def test_direct_reminder_uses_configured_playback_device(self):
        speech = Mock()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(hub, "AUDIO_DIR", Path(directory)), \
                patch.object(hub, "gTTS", return_value=speech), \
                patch.dict(hub.os.environ, {"AI_PLAYBACK_DEVICE": "plughw:CARD=Speaker,DEV=1"}), \
                patch.object(hub.subprocess, "run") as run:
            self.assertTrue(hub.play_tts_message("reminder", "reminder.mp3"))
        command = run.call_args.args[0]
        self.assertEqual(command[:5], ["mpg123", "-o", "alsa", "-a",
                                       "plughw:CARD=Speaker,DEV=1"])
        self.assertEqual(run.call_args.kwargs, {"check": True})

    def test_hub_keeps_both_checks_running_while_ai_worker_is_busy_and_stops_it(self):
        release = threading.Event()
        busy = threading.Event()

        def background_work():
            busy.set()
            release.wait(5)

        worker = threading.Thread(target=background_work, daemon=True)

        def stop():
            release.set()
            worker.join(timeout=2)

        controller = Mock()
        controller.stop.side_effect = stop

        def start():
            worker.start()
            self.assertTrue(busy.wait(2))
            return controller

        def check_schedule(*args):
            self.assertTrue(worker.is_alive(), "Test requires AI work still in progress")
            return args[-1]

        try:
            with patch.object(hub, "start_ai_conversation", side_effect=start) as start_ai, \
                    patch.object(hub, "start_inactivity_care", return_value=None), \
                    patch.object(hub, "check_medication_schedule_once", side_effect=check_schedule) as schedule, \
                    patch.object(hub, "check_inactivity_events_once") as watch, \
                    patch.object(hub.time, "sleep", side_effect=[None, KeyboardInterrupt]):
                with self.assertRaises(KeyboardInterrupt):
                    hub.run_hub_mode()
            start_ai.assert_called_once()
            self.assertEqual(schedule.call_count, 2)
            self.assertEqual(watch.call_count, 2)
            controller.stop.assert_called_once_with()
            self.assertFalse(worker.is_alive())
        finally:
            release.set()
            if worker.ident is not None:
                worker.join(timeout=2)

    def test_care_disabled_without_ai_does_not_import_voice_modules(self):
        with patch.object(builtins, "__import__", side_effect=AssertionError("unexpected import")):
            self.assertIsNone(hub.start_inactivity_care(None))

    def test_care_starts_with_existing_voice_adapters(self):
        optional_voice = types.ModuleType("voice_ai")
        optional_voice.create_inactivity_controller = Mock()
        active = Mock()
        with patch.dict(sys.modules, {"voice_ai": optional_voice}):
            result = hub.start_inactivity_care(active)
        optional_voice.create_inactivity_controller.assert_called_once_with(
            hub.FIREBASE_DATABASE_URL, hub.ELDER_ID, hub.DEVICE_ID, active.conversation,
        )
        self.assertIs(result, optional_voice.create_inactivity_controller.return_value.start.return_value)

    def test_care_start_failure_keeps_events_open(self):
        optional_voice = types.ModuleType("voice_ai")
        optional_voice.create_inactivity_controller = Mock(side_effect=ValueError("private details"))
        with patch.dict(sys.modules, {"voice_ai": optional_voice}):
            self.assertIsNone(hub.start_inactivity_care(Mock()))
        self.assertNotIn("private details", self.output.getvalue())

    def test_care_worker_replaces_legacy_watcher_and_leaves_schedule_running(self):
        active, care = Mock(), Mock()
        with patch.object(hub, "start_ai_conversation", return_value=active), \
                patch.object(hub, "start_inactivity_care", return_value=care) as start, \
                patch.object(hub, "check_medication_schedule_once") as schedule, \
                patch.object(hub, "check_inactivity_events_once") as legacy, \
                patch.object(hub.time, "sleep", side_effect=[None, KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                hub.run_hub_mode()
        start.assert_called_once_with(active)
        self.assertEqual(schedule.call_count, 2)
        legacy.assert_not_called()
        care.stop.assert_called_once_with()
        active.stop.assert_called_once_with()

    def test_requested_care_start_failure_never_falls_back_to_unclaimed_audio(self):
        active = Mock()
        with patch.dict("os.environ", {"AI_CONVERSATION_ENABLED": "true"}), \
                patch.object(hub, "start_ai_conversation", return_value=active), \
                patch.object(hub, "start_inactivity_care", return_value=None), \
                patch.object(hub, "check_medication_schedule_once") as schedule, \
                patch.object(hub, "check_inactivity_events_once") as legacy, \
                patch.object(hub.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                hub.run_hub_mode()
        schedule.assert_called_once()
        legacy.assert_not_called()
        active.stop.assert_called_once_with()

    def test_legacy_watcher_skips_durable_care_attempt_and_prior_announcement(self):
        source = {"type": "long_inactivity", "status": "open", "createdAt": 1000}
        events = {
            "care": dict(source, meta={"voiceCheck": {"status": "processing"}}),
            "legacy": source,
            "announcement": {"type": "inactivity_voice_check_sent", "sourceEventId": "legacy"},
            "fresh": source,
        }
        self.assertEqual([key for key, _ in hub.find_new_open_long_inactivity_events(events, set(), 1000)],
                         ["fresh"])


if __name__ == "__main__":
    unittest.main()
