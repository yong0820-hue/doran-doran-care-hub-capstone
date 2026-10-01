"""Offline scheduler/worker checks with real interaction locks and event storage."""

from contextlib import redirect_stdout
from datetime import datetime
import io
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "raspberry-pi-voice-hub"))

from firebase_medication import FirebaseMedicationClient
from medication_care import MedicationCare
from test_firebase_medication import FakeFirebase
from test_main_regression import load_main
from voice_ai import VoiceConversation, VoiceSettings


class MedicationSchedulerWorkerTests(unittest.TestCase):
    def setUp(self):
        self.hub = load_main()
        self.hub.DEVICE_ID = "pi_001"
        self.hub.ELDER_ID = "elder_001"
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = io.StringIO()
        quiet = redirect_stdout(self.output)
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def make_voice(self):
        return VoiceConversation(
            Mock(), VoiceSettings(), Mock(), threading.Lock(), self.temporary.name,
        )

    def test_chat_wait_does_not_lose_next_minute_or_duplicate_either_slot(self):
        voice = self.make_voice()
        audio = patch.object(voice, "_run_care_check", return_value="confirmed")
        run_audio = audio.start()
        self.addCleanup(audio.stop)
        client = Mock()
        client.validate_before_audio.return_value = True
        first_claimed = threading.Event()
        both_completed = threading.Event()
        scheduling_done = threading.Event()
        completed = []
        errors = []

        def run_check(slot_key, event, callback):
            first_claimed.set()
            result = callback("event_" + slot_key)
            completed.append((slot_key, event, result))
            if len(completed) == 2:
                both_completed.set()
            return True

        client.run_scheduled.side_effect = run_check
        factory = Mock(wraps=self.hub.build_medication_event)
        care = MedicationCare(client, voice, factory).start()
        self.addCleanup(care.stop)
        triggered = {}

        def check_three_passes():
            try:
                with patch.object(self.hub, "datetime") as clock, \
                        patch.object(self.hub, "now_ms", side_effect=[100, 200]):
                    clock.now.side_effect = [
                        datetime(2026, 9, 15, 8, 0),
                        datetime(2026, 9, 15, 8, 1),
                        datetime(2026, 9, 15, 8, 1, 5),
                    ]
                    day = self.hub.check_medication_schedule_once(
                        ["08:00", "08:01"], triggered, "2026-09-15", care,
                    )
                    if not first_claimed.wait(2):
                        raise AssertionError("Worker did not reach the first voice check")
                    for _ in range(2):
                        day = self.hub.check_medication_schedule_once(
                            ["08:00", "08:01"], triggered, day, care,
                        )
            except BaseException as exc:
                errors.append(exc)
            finally:
                scheduling_done.set()

        # The main test thread owns the real RLock, like an active chat session.
        # A separate scheduler probe lets a synchronous regression fail promptly.
        voice.interaction_lock.acquire()
        scheduler = threading.Thread(target=check_three_passes, daemon=True)
        try:
            scheduler.start()
            self.assertTrue(scheduling_done.wait(2), "Chat blocked the medication scheduler")
            self.assertEqual(errors, [])
            self.assertTrue(first_claimed.is_set())
            self.assertEqual(factory.call_count, 2)
            self.assertEqual(client.run_scheduled.call_count, 1)
            run_audio.assert_not_called()
        finally:
            voice.interaction_lock.release()
            scheduler.join(timeout=3)

        self.assertTrue(both_completed.wait(2), "Queued medication slots were not both processed")
        self.assertFalse(scheduler.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual([slot for slot, _event, _result in completed],
                         ["2026-09-15_08:00", "2026-09-15_08:01"])
        self.assertEqual([event["createdAt"] for _slot, event, _result in completed], [100, 200])
        self.assertEqual([result for _slot, _event, result in completed], ["confirmed", "confirmed"])
        self.assertEqual(run_audio.call_count, 2)
        self.assertEqual(client.validate_before_audio.call_count, 2)
        self.assertEqual(triggered, {"2026-09-15_08:00": True, "2026-09-15_08:01": True})

    def test_scheduler_worker_persists_all_outcomes_and_restart_never_repeats_audio(self):
        http = FakeFirebase()
        times = ["08:00", "08:01", "08:02", "08:03"]
        outcomes = ["confirmed", "defer", "no_speech", "unclear"]

        def run_worker_pass(restarted):
            client = FirebaseMedicationClient(
                "https://example.firebaseio.com", "elder_001", "pi_001", http=http,
            )
            voice = self.make_voice()
            voice.run_medication_check = Mock(side_effect=outcomes)
            all_processed = threading.Event()
            results = []
            real_run_scheduled = client.run_scheduled

            def observe_completion(slot_key, event, callback):
                result = real_run_scheduled(slot_key, event, callback)
                results.append(result)
                if len(results) == len(times):
                    all_processed.set()
                return result

            with patch.object(client, "run_scheduled", side_effect=observe_completion):
                care = MedicationCare(client, voice, self.hub.build_medication_event).start()
                try:
                    triggered, day = {}, "2026-09-15"
                    with patch.object(self.hub, "datetime") as clock, \
                            patch.object(self.hub, "now_ms", return_value=200 if restarted else 100):
                        clock.now.side_effect = [datetime(2026, 9, 15, 8, minute)
                                                 for minute in range(len(times))]
                        for _ in times:
                            day = self.hub.check_medication_schedule_once(times, triggered, day, care)
                    self.assertTrue(all_processed.wait(3), "Worker did not finish each scheduled slot")
                    self.assertEqual(results, [not restarted] * len(times))
                finally:
                    care.stop()
            return voice

        first_voice = run_worker_pass(restarted=False)
        self.assertEqual(first_voice.run_medication_check.call_count, len(times))
        self.assertEqual(len(http.records), len(times))
        for hm, outcome in zip(times, outcomes):
            slot = "2026-09-15_" + hm
            event = http.records["pi_medication_reminder_sent_pi_001_" + slot]
            self.assertEqual(event["type"], "medication_reminder_sent")
            self.assertEqual(event["status"], "resolved" if outcome == "confirmed" else "open")
            self.assertEqual(event["meta"]["scheduledFor"], slot)
            self.assertEqual(event["meta"]["voiceCheck"]["status"], "completed")
            self.assertEqual(event["meta"]["voiceCheck"]["outcome"], outcome)
            self.assertIn("attemptedAt", event["meta"]["voiceCheck"])
            self.assertEqual("resolvedAt" in event, outcome == "confirmed")
        writes_before_restart = len(http.put_calls)
        restarted_voice = run_worker_pass(restarted=True)
        restarted_voice.run_medication_check.assert_not_called()
        self.assertEqual(len(http.put_calls), writes_before_restart)
        self.assertEqual(len(http.records), len(times))


if __name__ == "__main__":
    unittest.main()
