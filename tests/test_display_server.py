from http.client import HTTPConnection
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock

HUB_DIR = Path(__file__).resolve().parents[1] / "raspberry-pi-voice-hub"
sys.path.insert(0, str(HUB_DIR))

from display_server import DisplayServer
from display_state import DisplayState


class DisplayServerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.assets = Path(directory.name)
        for name, content in (("index.html", "<h1>도란도란</h1>"),
                              ("app.js", "window.test = true;"),
                              ("memory_game.js", "window.memoryGameTest = true;"),
                              ("styles.css", "body { color: white; }")):
            (self.assets / name).write_text(content, encoding="utf-8")
        self.state = DisplayState()
        self.controller = Mock()
        self.controller.touch_available.return_value = True
        self.controller.request_local_session.return_value = "accepted"
        self.controller.request_conversation_reset.return_value = "accepted"
        self.controller.play_game_cue.return_value = "played"
        self.controller.cancel_game_voice.return_value = "accepted"
        self.server = DisplayServer(
            state=self.state, controller=self.controller,
            port=0, assets_dir=self.assets,
        ).start()
        self.addCleanup(self.server.stop)
        self.origin = f"http://127.0.0.1:{self.server.port}"

    def request(self, method="GET", path="/api/state", body=None, headers=None):
        connection = HTTPConnection(*self.server.address, timeout=4)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def touch_headers(self, **overrides):
        headers = {
            "Origin": self.origin,
            "Content-Type": "application/json",
            "X-Doran-Action": "conversation",
        }
        headers.update(overrides)
        return headers

    def touch(self):
        return self.request("POST", "/api/conversation", "{}", self.touch_headers())

    def game_headers(self, **overrides):
        headers = {
            "Origin": self.origin,
            "Content-Type": "application/json",
            "X-Doran-Action": "game",
        }
        headers.update(overrides)
        return headers

    def game_voice(self, payload):
        return self.request("POST", "/api/game-voice", json.dumps(payload),
                            self.game_headers())

    def test_state_is_live_and_never_exposes_transcript_or_secrets(self):
        with self.state.activity("inactivity"), self.state.phase("listening"):
            code, headers, data = self.request()
            payload = json.loads(data)
            self.assertEqual(code, 200)
            self.assertEqual(set(payload), {
                "state", "busy", "activity", "alert", "can_start", "audio_focus",
            })
            self.assertEqual(payload["state"], "listening")
            self.assertEqual(payload["alert"]["kind"], "inactivity")
            self.assertFalse(payload["can_start"])
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertNotIn("Access-Control-Allow-Origin", headers)
        payload = json.loads(self.request()[2])
        self.assertEqual(payload["state"], "idle")
        self.assertTrue(payload["can_start"])

    def test_static_allowlist_is_served_with_content_types_and_security_headers(self):
        for path, mime in (("/", "text/html"), ("/index.html", "text/html"),
                           ("/app.js", "text/javascript"),
                           ("/memory_game.js", "text/javascript"),
                           ("/styles.css", "text/css")):
            with self.subTest(path=path):
                code, headers, data = self.request(path=path)
                self.assertEqual(code, 200)
                self.assertTrue(headers["Content-Type"].startswith(mime))
                self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
                self.assertGreater(len(data), 0)

    def test_secret_files_directories_traversal_and_absolute_urls_are_not_served(self):
        (self.assets / ".env").write_text("SECRET=never", encoding="utf-8")
        for path in ("/.env", "/../.env", "/%2e%2e/.env", "/main.py",
                     "/display/", "http://localhost/api/state", "//outside.invalid/api/state"):
            with self.subTest(path=path):
                code, _, data = self.request(path=path, headers={
                    "Host": f"127.0.0.1:{self.server.port}",
                })
                self.assertEqual(code, 404)
                self.assertNotIn(b"SECRET", data)

    def test_memory_game_asset_is_fixed_and_missing_asset_is_contained(self):
        code, headers, data = self.request(path="/memory_game.js")
        self.assertEqual(code, 200)
        self.assertEqual(data, (self.assets / "memory_game.js").read_bytes())
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        for path in ("/display/memory_game.js", "/memory_game.test.js", "/api/game-input"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path=path)[0], 404)
        self.assertEqual(self.request("POST", "/api/game-input", "{}", self.touch_headers())[0], 404)
        self.controller.request_local_session.assert_not_called()
        (self.assets / "memory_game.js").unlink()
        self.assertEqual(self.request(path="/memory_game.js")[0], 503)
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.touch()[0], 202)

    def test_missing_assets_does_not_prevent_state_or_touch_api(self):
        (self.assets / "index.html").unlink()
        self.assertEqual(self.request(path="/")[0], 503)
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.touch()[0], 202)

    def test_touch_forwards_once_to_existing_controller(self):
        code, _, data = self.touch()
        self.assertEqual(code, 202)
        self.assertEqual(json.loads(data), {"status": "accepted"})
        self.controller.request_local_session.assert_called_once_with()

    def test_game_voice_forwards_only_allowlisted_cues_and_scoped_cancel(self):
        code, _, data = self.game_voice({
            "action": "play", "cue": "start", "session": "game-abc_1",
        })
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(data)["status"], "played")
        self.controller.play_game_cue.assert_called_once_with("start", "game-abc_1")
        code, _, data = self.game_voice({"action": "cancel", "session": "game-abc_1"})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(data)["status"], "accepted")
        self.controller.cancel_game_voice.assert_called_once_with("game-abc_1")

    def test_game_voice_rejects_text_filenames_unknown_cues_and_invalid_sessions(self):
        invalid = (
            {"action": "play", "cue": "unknown", "session": "game-1"},
            {"action": "play", "cue": "start", "session": "game-1", "text": "말해줘"},
            {"action": "play", "cue": "start", "session": "game-1", "filename": ".env"},
            {"action": "cancel", "session": "game-1", "cue": "start"},
            {"action": "cancel", "session": ""},
            {"action": "cancel", "session": "공백 없음"},
            {"action": "cancel", "session": "x" * 65},
            {"action": "speak", "session": "game-1"},
            [],
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertEqual(self.game_voice(payload)[0], 400)
        self.controller.play_game_cue.assert_not_called()
        self.controller.cancel_game_voice.assert_not_called()

    def test_game_voice_uses_dedicated_origin_action_and_status_contract(self):
        payload = json.dumps({"action": "play", "cue": "input", "session": "game-2"})
        for headers in (
                self.game_headers(Origin="null"),
                self.game_headers(**{"X-Doran-Action": "conversation"}),
                self.game_headers(Host="evil.invalid")):
            with self.subTest(headers=headers):
                self.assertEqual(self.request(
                    "POST", "/api/game-voice", payload, headers,
                )[0], 403)
        for result, expected in (("game_busy", 409), ("interrupted", 409),
                                 ("cancelled", 409), ("unavailable", 503),
                                 ("failed", 503)):
            with self.subTest(result=result):
                self.controller.play_game_cue.return_value = result
                self.assertEqual(self.game_voice({
                    "action": "play", "cue": "input", "session": "game-2",
                })[0], expected)
        self.controller.play_game_cue.side_effect = RuntimeError("SECRET")
        code, _, data = self.game_voice({
            "action": "play", "cue": "input", "session": "game-2",
        })
        self.assertEqual(code, 503)
        self.assertNotIn(b"SECRET", data)
        self.assertEqual(self.request(path="/api/game-voice")[0], 404)

    def test_busy_and_unavailable_controller_results(self):
        for result, code in (("busy", 409), ("unavailable", 503)):
            with self.subTest(result=result):
                self.controller.request_local_session.return_value = result
                actual, _, data = self.touch()
                self.assertEqual(actual, code)
                self.assertEqual(json.loads(data)["status"], result)

    def test_conversation_reset_forwards_without_starting_or_stopping_audio(self):
        with self.state.activity("conversation"), self.state.phase("speaking"):
            code, _, data = self.request("POST", "/api/conversation-reset", "{}",
                self.touch_headers(**{"X-Doran-Action": "conversation-reset"}))
            self.assertEqual((code, json.loads(data)), (202, {"status": "accepted"}))
            self.assertEqual(self.state.snapshot()["state"], "speaking")
        self.controller.request_conversation_reset.assert_called_once_with()
        self.controller.request_local_session.assert_not_called()
        self.controller.stop.assert_not_called()

    def test_conversation_reset_requires_separate_action_origin_and_empty_body(self):
        valid = self.touch_headers(**{"X-Doran-Action": "conversation-reset"})
        cases = [
            ("{}", self.touch_headers(), 403),
            ("{}", {**valid, "Origin": "https://outside.invalid"}, 403),
            ("{}", {**valid, "Host": "outside.invalid"}, 403),
            ('{"clearMemories":true}', valid, 400),
            ("[]", valid, 400),
            ("x" * 1025, valid, 413),
            ("{}", {**valid, "Content-Type": "text/plain"}, 415),
            ("{}", {**valid, "Transfer-Encoding": "chunked"}, 415),
        ]
        for body, headers, expected in cases:
            with self.subTest(body=body[:30], headers=headers):
                self.assertEqual(self.request("POST", "/api/conversation-reset",
                                              body, headers)[0], expected)
        self.assertEqual(self.request(path="/api/conversation-reset")[0], 404)
        self.controller.request_conversation_reset.assert_not_called()

    def test_conversation_reset_unavailable_and_errors_are_contained(self):
        headers = self.touch_headers(**{"X-Doran-Action": "conversation-reset"})
        for result in ("unavailable", None, {"unexpected": "result"}):
            self.controller.request_conversation_reset.return_value = result
            self.assertEqual(self.request("POST", "/api/conversation-reset", "{}", headers)[0], 503)
        self.controller.request_conversation_reset.side_effect = RuntimeError("SECRET")
        code, _, data = self.request("POST", "/api/conversation-reset", "{}", headers)
        self.assertEqual(code, 503)
        self.assertNotIn(b"SECRET", data)
        self.server.controller = None
        self.assertEqual(self.request("POST", "/api/conversation-reset", "{}", headers)[0], 503)

    def test_no_controller_disables_touch_and_returns_unavailable(self):
        self.server.controller = None
        self.assertFalse(json.loads(self.request()[2])["can_start"])
        self.assertEqual(self.touch()[0], 503)

    def test_controller_failures_are_contained_and_do_not_leak_errors(self):
        self.controller.touch_available.side_effect = RuntimeError("SECRET")
        self.controller.request_local_session.side_effect = RuntimeError("SECRET")
        self.assertFalse(json.loads(self.request()[2])["can_start"])
        code, _, data = self.touch()
        self.assertEqual(code, 503)
        self.assertNotIn(b"SECRET", data)
        self.controller.request_local_session.side_effect = None
        self.controller.request_local_session.return_value = {"unexpected": "result"}
        self.assertEqual(self.touch()[0], 503)

    def test_duplicate_concurrent_touches_use_controller_atomic_acceptance(self):
        lock = threading.Lock()
        accepted = []

        def accept_once():
            with lock:
                if accepted:
                    return "busy"
                accepted.append(True)
                return "accepted"

        self.controller.request_local_session.side_effect = accept_once
        codes = []
        workers = [threading.Thread(target=lambda: codes.append(self.touch()[0])) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=4)
            self.assertFalse(worker.is_alive())
        self.assertEqual(sorted(codes), [202, 409, 409, 409])

    def test_get_and_post_reject_nonlocal_host_and_wrong_port(self):
        for host in ("evil.example", f"evil.example:{self.server.port}",
                     "127.0.0.1:1", f"127.0.0.1.evil.example:{self.server.port}"):
            with self.subTest(host=host):
                self.assertEqual(self.request(headers={"Host": host})[0], 403)
                self.assertEqual(self.request("POST", "/api/conversation", "{}",
                    self.touch_headers(Host=host, Origin=f"http://{host}"))[0], 403)
        self.controller.request_local_session.assert_not_called()

    def test_localhost_is_accepted_only_with_matching_origin(self):
        host = f"localhost:{self.server.port}"
        self.assertEqual(self.request(headers={"Host": host})[0], 200)
        headers = self.touch_headers(Host=host, Origin=f"http://{host}")
        self.assertEqual(self.request("POST", "/api/conversation", "{}", headers)[0], 202)

    def test_touch_requires_exact_origin_and_custom_header(self):
        for origin in (None, "null", "https://evil.example", self.origin + ".evil.example",
                       self.origin + "/", f"http://localhost:{self.server.port}"):
            headers = self.touch_headers()
            if origin is None:
                del headers["Origin"]
            else:
                headers["Origin"] = origin
            with self.subTest(origin=origin):
                self.assertEqual(self.request("POST", "/api/conversation", "{}", headers)[0], 403)
        for action in (None, "anything"):
            headers = self.touch_headers()
            if action is None:
                del headers["X-Doran-Action"]
            else:
                headers["X-Doran-Action"] = action
            self.assertEqual(self.request("POST", "/api/conversation", "{}", headers)[0], 403)
        self.controller.request_local_session.assert_not_called()

    def test_duplicate_origin_and_host_are_rejected(self):
        for duplicate in ("Origin", "Host", "X-Doran-Action"):
            connection = HTTPConnection(*self.server.address, timeout=4)
            try:
                connection.putrequest("POST", "/api/conversation", skip_host=True)
                headers = self.touch_headers(Host=f"127.0.0.1:{self.server.port}")
                headers["Content-Length"] = "2"
                for key, value in headers.items():
                    connection.putheader(key, value)
                    if key == duplicate:
                        connection.putheader(key, value)
                connection.endheaders(b"{}")
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
            finally:
                connection.close()
        self.controller.request_local_session.assert_not_called()

    def test_touch_rejects_arbitrary_payload_large_body_and_wrong_content_type(self):
        cases = (("not json", {}, 400), ('{"command":"something"}', {}, 400),
                 ("[]", {}, 400), ("x" * 1025, {}, 413),
                 ("{}", {"Content-Type": "text/plain"}, 415),
                 ("{}", {"Content-Length": "-1"}, 400),
                 ("{}", {"Transfer-Encoding": "chunked"}, 415))
        for body, overrides, expected in cases:
            with self.subTest(body=body[:30], overrides=overrides):
                code, _, _ = self.request("POST", "/api/conversation", body,
                                          self.touch_headers(**overrides))
                self.assertEqual(code, expected)
        self.controller.request_local_session.assert_not_called()

    def test_get_cannot_start_conversation_and_unknown_post_is_rejected(self):
        self.assertEqual(self.request(path="/api/conversation")[0], 404)
        self.assertEqual(self.request("POST", "/api/state", "{}", self.touch_headers())[0], 404)
        self.controller.request_local_session.assert_not_called()

    def test_server_refuses_external_bind_and_stops_idempotently(self):
        for host in ("0.0.0.0", "192.168.1.2", "example.com", "::"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                DisplayServer(host=host)
        self.assertEqual(self.server.address[0], "127.0.0.1")
        thread = self.server._thread
        self.assertIs(self.server.start(), self.server)
        self.server.stop()
        self.server.stop()
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
