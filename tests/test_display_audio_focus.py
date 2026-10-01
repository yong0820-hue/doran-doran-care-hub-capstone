"""Browser radio coordination through the existing state and local HTTP server."""

from http.client import HTTPConnection
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "raspberry-pi-voice-hub"))
from display_state import DisplayState
from display_server import DisplayServer


class AudioFocusTests(unittest.TestCase):
    def test_all_voice_phases_and_activities_block_play(self):
        state = DisplayState()
        for factory, kinds in ((state.phase, ("listening", "thinking", "speaking")),
                               (state.activity, ("conversation", "medication", "inactivity", "game"))):
            for kind in kinds:
                with self.subTest(kind=kind), factory(kind):
                    self.assertTrue(state.snapshot()["audio_focus"]["blocked"])
                    self.assertFalse(state.request_radio(True))
                self.assertFalse(state.snapshot()["audio_focus"]["blocked"])

    def test_whole_care_scope_keeps_focus_between_audio_phases(self):
        state = DisplayState()
        with state.activity("medication"):
            revision = state.snapshot()["audio_focus"]["revision"]
            for phase in ("speaking", "listening", "thinking"):
                with state.phase(phase):
                    self.assertFalse(state.request_radio(True))
                self.assertEqual(state.snapshot()["audio_focus"],
                                 {"blocked": True, "revision": revision,
                                  "non_game_revision": 1})

    def test_revision_records_care_that_completes_between_polls(self):
        state = DisplayState()
        before = state.snapshot()["audio_focus"]
        with state.activity("inactivity"):
            pass
        after = state.snapshot()["audio_focus"]
        self.assertFalse(after["blocked"])
        self.assertGreater(after["revision"], before["revision"])
        self.assertGreater(after["non_game_revision"],
                           before["non_game_revision"])

    def test_game_focus_does_not_advance_non_game_revision(self):
        state = DisplayState()
        before = state.snapshot()["audio_focus"]
        with state.activity("game"), state.phase("speaking"):
            pass
        after = state.snapshot()["audio_focus"]
        self.assertGreater(after["revision"], before["revision"])
        self.assertEqual(after["non_game_revision"],
                         before["non_game_revision"])

    def test_scope_waits_for_pause_ack_before_entering_real_work(self):
        for factory_name in ("activity", "phase"):
            with self.subTest(factory=factory_name):
                state = DisplayState()
                state.RADIO_PAUSE_TIMEOUT_SECONDS = 2
                self.assertTrue(state.request_radio(True))
                published = threading.Event()
                work = threading.Event()
                original = state._wait_for_radio_pause

                def wait():
                    published.set()
                    original()

                def run():
                    factory = getattr(state, factory_name)
                    with factory("medication" if factory_name == "activity" else "listening"):
                        work.set()

                with patch.object(state, "_wait_for_radio_pause", side_effect=wait):
                    worker = threading.Thread(target=run)
                    worker.start()
                    try:
                        self.assertTrue(published.wait(1))
                        self.assertTrue(state.snapshot()["audio_focus"]["blocked"])
                        self.assertFalse(work.is_set())
                        self.assertFalse(state.request_radio(True))
                        self.assertTrue(state.request_radio(False))
                        self.assertTrue(work.wait(1))
                    finally:
                        state.request_radio(False)
                        worker.join(2)
                self.assertFalse(worker.is_alive())

    def test_no_browser_never_waits_for_display(self):
        state = DisplayState()
        with patch.object(state._radio_condition, "wait") as wait:
            with state.activity("conversation"), state.phase("listening"):
                pass
        wait.assert_not_called()

    def test_missing_pause_ack_has_bounded_wait_and_no_repeated_delay(self):
        state = DisplayState()
        state.RADIO_PAUSE_TIMEOUT_SECONDS = .03
        state.request_radio(True)
        start = time.monotonic()
        with state.activity("inactivity"):
            self.assertLess(time.monotonic() - start, .5)
            with patch.object(state._radio_condition, "wait") as wait:
                with state.phase("speaking"):
                    pass
            wait.assert_not_called()
        self.assertTrue(state.request_radio(True))

    def test_exception_restores_focus_without_resuming_radio(self):
        state = DisplayState()
        with self.assertRaises(RuntimeError), state.activity("conversation"):
            raise RuntimeError("failed")
        self.assertFalse(state.snapshot()["audio_focus"]["blocked"])
        self.assertFalse(state._radio_playing)

    def test_intent_type_is_strict(self):
        state = DisplayState()
        for value in (1, 0, None, "false", [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                state.request_radio(value)


class RadioHTTPTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.assets = Path(directory.name)
        self.state = DisplayState()
        self.server = DisplayServer(self.state, port=0, assets_dir=self.assets).start()
        self.addCleanup(self.server.stop)
        self.origin = f"http://127.0.0.1:{self.server.port}"

    def request(self, path="/api/radio-focus", body='{"playing":false}', overrides=None):
        headers = {"Origin": self.origin, "X-Doran-Action": "radio",
                   "Content-Type": "application/json"}
        headers.update(overrides or {})
        connection = HTTPConnection(*self.server.address, timeout=2)
        try:
            connection.request("GET" if body is None else "POST", path,
                               body=body, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), json.loads(response.read())
        finally:
            connection.close()

    def test_focus_intent_accepts_idle_rejects_busy_and_ack_releases_worker(self):
        self.assertEqual(self.request(body='{"playing":true}')[0], 200)
        entered = threading.Event()
        published = threading.Event()
        original = self.state._wait_for_radio_pause

        def waiting():
            published.set()
            original()

        def run():
            with self.state.activity("medication"):
                entered.set()

        with patch.object(self.state, "_wait_for_radio_pause", side_effect=waiting):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(published.wait(1))
                snapshot = self.request("/api/state", None)[2]
                self.assertTrue(snapshot["audio_focus"]["blocked"])
                self.assertFalse(entered.is_set())
                self.assertEqual(self.request(body='{"playing":true}')[0], 409)
                self.assertEqual(self.request()[0], 200)
                self.assertTrue(entered.wait(1))
            finally:
                self.state.request_radio(False)
                worker.join(2)

    def test_radio_route_preserves_origin_host_and_action_validation(self):
        for overrides in ({"Origin": "null"}, {"Origin": "http://evil.invalid"},
                          {"Host": "evil.invalid"}, {"X-Doran-Action": "conversation"}):
            with self.subTest(overrides=overrides):
                self.assertEqual(self.request(overrides=overrides)[0], 403)

    def test_radio_payload_must_contain_only_boolean_playing(self):
        for body in ('{}', '[]', 'null', '{"playing":1}', '{"playing":"false"}',
                     '{"playing":true,"url":"https://example.invalid"}', 'broken'):
            with self.subTest(body=body):
                self.assertEqual(self.request(body=body)[0], 400)

    def test_radio_get_never_changes_playback_intent(self):
        self.assertEqual(self.request(body=None)[0], 404)
        self.assertFalse(self.state._radio_playing)

    def test_config_reload_empty_and_invalid_do_not_break_state(self):
        self.assertEqual(self.request("/api/radio-presets", None)[2]["status"], "missing")
        config = self.assets / "radio_presets.json"
        config.write_text('{"presets":[]}', encoding="utf-8")
        self.assertEqual(self.request("/api/radio-presets", None)[2]["status"], "empty")
        config.write_text('{"presets":[{"name":"시험","url":"http://127.0.0.1:8766/test.wav"}]}', encoding="utf-8")
        payload = self.request("/api/radio-presets", None)[2]
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["presets"][0]["name"], "시험")
        config.write_text('broken', encoding="utf-8")
        self.assertEqual(self.request("/api/radio-presets", None)[2]["status"], "invalid")
        self.assertEqual(self.request("/api/state", None)[0], 200)

    def test_http_media_is_allowed_without_widening_script_or_connect_policy(self):
        _, headers, _ = self.request("/api/state", None)
        policy = headers["Content-Security-Policy"]
        self.assertIn("media-src 'self' http: https:;", policy)
        self.assertIn("script-src 'self';", policy)
        self.assertIn("connect-src 'self';", policy)
        self.assertEqual(headers["Cache-Control"], "no-store")


if __name__ == "__main__":
    unittest.main()
