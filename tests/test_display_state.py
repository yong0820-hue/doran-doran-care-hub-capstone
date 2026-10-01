from pathlib import Path
import sys
import threading
import unittest

HUB_DIR = Path(__file__).resolve().parents[1] / "raspberry-pi-voice-hub"
sys.path.insert(0, str(HUB_DIR))

from display_state import DisplayState


class DisplayStateTests(unittest.TestCase):
    def setUp(self):
        self.state = DisplayState()

    def test_idle_has_no_alert_or_busy_work(self):
        self.assertEqual(self.state.snapshot(), {
            "state": "idle", "busy": False, "activity": None, "alert": None,
            "audio_focus": {
                "blocked": False, "revision": 0, "non_game_revision": 0,
            },
        })

    def test_conversation_tracks_capture_network_playback_then_idle(self):
        states = []
        with self.state.activity("conversation"):
            self.assertTrue(self.state.snapshot()["busy"])
            for phase in ("listening", "thinking", "speaking"):
                with self.state.phase(phase):
                    states.append(self.state.snapshot()["state"])
                self.assertEqual(self.state.snapshot()["state"], "idle")
        self.assertEqual(states, ["listening", "thinking", "speaking"])
        self.assertFalse(self.state.snapshot()["busy"])

    def test_exception_clears_phase_and_activity(self):
        with self.assertRaises(RuntimeError):
            with self.state.activity("inactivity"), self.state.phase("speaking"):
                raise RuntimeError("playback failed")
        self.assertEqual(self.state.snapshot(), {
            "state": "idle", "busy": False, "activity": None, "alert": None,
            "audio_focus": {
                "blocked": False, "revision": 2, "non_game_revision": 1,
            },
        })

    def test_physical_audio_outranks_network_work_and_restores_it(self):
        with self.state.phase("thinking"):
            with self.state.phase("speaking"):
                with self.state.phase("thinking"):
                    self.assertEqual(self.state.snapshot()["state"], "speaking")
            self.assertEqual(self.state.snapshot()["state"], "thinking")
        self.assertEqual(self.state.snapshot()["state"], "idle")

    def test_latest_physical_phase_wins_then_restores_previous_scope(self):
        with self.state.phase("listening"):
            with self.state.phase("speaking"):
                self.assertEqual(self.state.snapshot()["state"], "speaking")
            self.assertEqual(self.state.snapshot()["state"], "listening")

    def test_care_card_persists_between_phases(self):
        with self.state.activity("inactivity"):
            for phase in ("speaking", "listening", "thinking"):
                with self.state.phase(phase):
                    self.assertEqual(self.state.snapshot()["alert"]["kind"], "inactivity")
            snapshot = self.state.snapshot()
            self.assertEqual(snapshot["state"], "idle")
            self.assertEqual(snapshot["alert"]["kind"], "inactivity")
        self.assertIsNone(self.state.snapshot()["alert"])

    def test_inactivity_card_has_priority_then_medication_returns(self):
        with self.state.activity("medication"):
            self.assertEqual(self.state.snapshot()["alert"]["kind"], "medication")
            with self.state.activity("inactivity"):
                self.assertEqual(self.state.snapshot()["alert"]["kind"], "inactivity")
            self.assertEqual(self.state.snapshot()["alert"]["kind"], "medication")

    def test_concurrent_scopes_can_finish_out_of_order(self):
        entered = threading.Event()
        release = threading.Event()

        def care_worker():
            with self.state.activity("inactivity"), self.state.phase("thinking"):
                entered.set()
                release.wait(timeout=2)

        worker = threading.Thread(target=care_worker)
        worker.start()
        try:
            self.assertTrue(entered.wait(timeout=2))
            with self.state.activity("medication"), self.state.phase("speaking"):
                release.set()
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
                snapshot = self.state.snapshot()
                self.assertEqual(snapshot["state"], "speaking")
                self.assertEqual(snapshot["alert"]["kind"], "medication")
                self.assertTrue(snapshot["busy"])
        finally:
            release.set()
            worker.join(timeout=2)
        self.assertFalse(self.state.snapshot()["busy"])

    def test_repeated_kind_has_independent_scope_lifetime(self):
        first = self.state.activity("inactivity")
        second = self.state.activity("inactivity")
        first.__enter__()
        second.__enter__()
        first.__exit__(None, None, None)
        try:
            self.assertEqual(self.state.snapshot()["alert"]["kind"], "inactivity")
        finally:
            second.__exit__(None, None, None)
        self.assertIsNone(self.state.snapshot()["alert"])

    def test_snapshot_is_detached_from_internal_alert_data(self):
        with self.state.activity("medication"):
            snapshot = self.state.snapshot()
            snapshot["alert"]["title"] = "changed"
            self.assertNotEqual(self.state.snapshot()["alert"]["title"], "changed")

    def test_game_activity_is_identified_without_showing_a_care_alert(self):
        before = self.state.snapshot()["audio_focus"]["non_game_revision"]
        with self.state.activity("game"):
            snapshot = self.state.snapshot()
            self.assertEqual(snapshot["activity"], "game")
            self.assertTrue(snapshot["audio_focus"]["blocked"])
            self.assertEqual(snapshot["audio_focus"]["non_game_revision"], before)
            self.assertIsNone(snapshot["alert"])
        self.assertIsNone(self.state.snapshot()["activity"])

    def test_unsupported_states_and_radio_activity_are_rejected(self):
        for method, value in ((self.state.activity, "radio"), (self.state.phase, "idle")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                with method(value):
                    pass
        self.assertFalse(self.state.snapshot()["busy"])


if __name__ == "__main__":
    unittest.main()
