"""Optional loopback-only touchscreen bridge using the Python standard library."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit

from display_state import DISPLAY
from radio_presets import load_radio_presets


class DisplayServer:
    """Serve fixed static assets and forward touch to the existing controller.

    This must run inside main.py: a separate Python process cannot observe the
    in-memory voice state. Startup errors are handled by the caller so display
    failures never prevent the voice runtime from starting.
    """

    _ASSETS = {
        "/": ("index.html", "text/html; charset=utf-8"),
        "/index.html": ("index.html", "text/html; charset=utf-8"),
        "/styles.css": ("styles.css", "text/css; charset=utf-8"),
        "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        "/memory_game.js": ("memory_game.js", "text/javascript; charset=utf-8"),
    }
    _GAME_CUES = {"start", "input", "correct", "incorrect", "complete"}
    _GAME_SESSION = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")

    def __init__(self, state=DISPLAY, controller=None, host="127.0.0.1",
                 port=8765, assets_dir=None):
        if host not in {"127.0.0.1", "localhost"}:
            raise ValueError("Display server must bind to IPv4 loopback")
        self.state = state
        self.controller = controller
        self.host = host
        self.port = int(port)
        self.assets_dir = (Path(assets_dir) if assets_dir is not None
                           else Path(__file__).resolve().parent / "display")
        self._server = None
        self._thread = None

    @property
    def address(self):
        return self._server.server_address if self._server else (self.host, self.port)

    def start(self):
        if self._server is not None:
            return self
        owner = self

        class Handler(BaseHTTPRequestHandler):
            # Bound slow/incomplete clients; workers cannot hold shutdown open.
            timeout = 3

            def log_message(self, format, *args):
                pass

            def _send(self, status, data, content_type="application/json; charset=utf-8"):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "connect-src 'self'; img-src 'self' data:; "
                    "media-src 'self' http: https:; "
                    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
                ))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _json(self, status, payload):
                self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

            def _host(self):
                hosts = self.headers.get_all("Host", [])
                if len(hosts) != 1:
                    return None
                host = hosts[0]
                allowed = {f"127.0.0.1:{owner.port}", f"localhost:{owner.port}"}
                # Port 80 is the sole legal omitted-port case.
                if owner.port == 80:
                    allowed.update({"127.0.0.1", "localhost"})
                return host if host in allowed else None

            def _path(self):
                try:
                    parsed = urlsplit(self.path)
                except ValueError:
                    return None
                if not self.path.startswith("/") or parsed.netloc or parsed.fragment:
                    return None
                return parsed.path

            def do_GET(self):
                if not self._host():
                    self._json(403, {"status": "forbidden"})
                    return
                path = self._path()
                if path == "/api/radio-presets":
                    self._json(200, load_radio_presets(owner.assets_dir / "radio_presets.json"))
                    return
                if path == "/api/state":
                    snapshot = owner.state.snapshot()
                    available = False
                    if owner.controller is not None:
                        try:
                            available = bool(owner.controller.touch_available())
                        except Exception:
                            pass
                    snapshot["can_start"] = available and not snapshot["busy"]
                    self._json(200, snapshot)
                    return
                asset = owner._ASSETS.get(path)
                if asset is None:
                    self._json(404, {"status": "not_found"})
                    return
                filename, content_type = asset
                try:
                    data = (owner.assets_dir / filename).read_bytes()
                except OSError:
                    self._json(503, {"status": "display_assets_unavailable"})
                    return
                self._send(200, data, content_type)

            def do_POST(self):
                host = self._host()
                path = self._path()
                origins = self.headers.get_all("Origin", [])
                actions = self.headers.get_all("X-Doran-Action", [])
                expected_action = (
                    "radio" if path == "/api/radio-focus" else
                    "game" if path == "/api/game-voice" else
                    "conversation-reset" if path == "/api/conversation-reset" else
                    "conversation"
                )
                if (host is None or origins != [f"http://{host}"]
                        or actions != [expected_action]):
                    self._json(403, {"status": "forbidden"})
                    return
                if path not in {
                        "/api/conversation", "/api/conversation-reset",
                        "/api/radio-focus", "/api/game-voice"}:
                    self._json(404, {"status": "not_found"})
                    return
                if (self.headers.get("Content-Type", "").split(";", 1)[0].strip()
                        != "application/json" or self.headers.get_all("Transfer-Encoding")):
                    self._json(415, {"status": "invalid_content_type"})
                    return
                lengths = self.headers.get_all("Content-Length", [])
                if (len(lengths) != 1 or not lengths[0].isdigit()
                        or len(lengths[0]) > 10):
                    self._json(400, {"status": "invalid_request"})
                    return
                length = int(lengths[0])
                if length > 1024:
                    self._json(413, {"status": "request_too_large"})
                    return
                try:
                    payload = json.loads(self.rfile.read(length))
                except (ValueError, OSError):
                    self._json(400, {"status": "invalid_request"})
                    return
                if path == "/api/radio-focus":
                    if (not isinstance(payload, dict) or set(payload) != {"playing"}
                            or type(payload["playing"]) is not bool):
                        self._json(400, {"status": "invalid_request"})
                        return
                    accepted = owner.state.request_radio(payload["playing"])
                    self._json(200 if accepted else 409, {
                        "status": "accepted" if accepted else "busy",
                        "audio_focus": owner.state.snapshot()["audio_focus"],
                    })
                    return
                if path == "/api/game-voice":
                    if not isinstance(payload, dict) or payload.get("action") not in {
                            "play", "cancel"}:
                        self._json(400, {"status": "invalid_request"})
                        return
                    session_id = payload.get("session")
                    if (not isinstance(session_id, str)
                            or owner._GAME_SESSION.fullmatch(session_id) is None):
                        self._json(400, {"status": "invalid_request"})
                        return
                    if payload["action"] == "play":
                        if (set(payload) != {"action", "cue", "session"}
                                or payload.get("cue") not in owner._GAME_CUES):
                            self._json(400, {"status": "invalid_request"})
                            return
                        method = "play_game_cue"
                        arguments = (payload["cue"], session_id)
                    else:
                        if set(payload) != {"action", "session"}:
                            self._json(400, {"status": "invalid_request"})
                            return
                        method = "cancel_game_voice"
                        arguments = (session_id,)
                    result = "unavailable"
                    if owner.controller is not None:
                        try:
                            result = getattr(owner.controller, method)(*arguments)
                        except Exception:
                            pass
                    statuses = {
                        "played": 200, "accepted": 200, "idle": 200,
                        "cancelled": 409, "game_busy": 409, "interrupted": 409,
                        "unavailable": 503, "failed": 503,
                    }
                    if not isinstance(result, str) or result not in statuses:
                        result = "unavailable"
                    self._json(statuses[result], {
                        "status": result,
                        "audio_focus": owner.state.snapshot()["audio_focus"],
                    })
                    return
                if payload != {}:
                    self._json(400, {"status": "invalid_request"})
                    return
                result = "unavailable"
                if owner.controller is not None:
                    try:
                        method = ("request_conversation_reset"
                                  if path == "/api/conversation-reset"
                                  else "request_local_session")
                        result = getattr(owner.controller, method)()
                    except Exception:
                        pass
                statuses = {"accepted": 202, "busy": 409, "unavailable": 503}
                if not isinstance(result, str) or result not in statuses:
                    result = "unavailable"
                self._json(statuses[result], {"status": result})

        server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        server.daemon_threads = True
        self.port = server.server_address[1]
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.1},
            daemon=True, name="touchscreen-display",
        )
        try:
            thread.start()
        except Exception:
            server.server_close()
            raise
        self._server = server
        self._thread = thread
        return self

    def stop(self):
        server = self._server
        if server is None:
            return
        if self.controller is not None:
            try:
                self.controller.cancel_game_voice()
            except Exception:
                pass
        self.state.request_radio(False)
        server.shutdown()
        server.server_close()
        self._thread.join(timeout=3)
        self._server = None
        self._thread = None
