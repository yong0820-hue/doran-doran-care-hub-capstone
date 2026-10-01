#!/usr/bin/env python3
"""User-run isolated HTTP smoke; never imports main.py or voice_ai.py.

Usage (the code directory is the deployed raspberry-pi-voice-hub directory):
    python -B isolated_http_smoke.py --code-dir /absolute/runtime/path --dry-run
    python -B isolated_http_smoke.py --code-dir /absolute/runtime/path

Exit 0: the explicitly labelled dry-run or live isolated checks passed.
Exit 1: live check, transport, boundary, or cleanup failure (no retry).
Exit 2: arguments, source validation, or source compilation failed.

This uses an independent DisplayState, controller=None, empty temporary assets,
and an ephemeral IPv4 loopback port. It does not test the running voice service,
browser playback, cloud state, memory, reset, SDKs, or physical audio.
"""

import argparse
from pathlib import Path
import sys


MODULE_NAMES = ("radio_presets", "display_state", "display_server")
MAX_SOURCE_BYTES = 512 * 1024
MAX_RESPONSE_BYTES = 16 * 1024


class SmokeFailure(Exception):
    """Only fixed diagnostic codes may be printed; never print HTTP bodies."""


class BoundaryViolation(SmokeFailure):
    pass


class BoundaryGuard:
    """Process-local defence in depth, not a sandbox for untrusted source.

    The parent deployment preflight must verify the source manifest first.
    An audit hook cannot be removed; this script runs in its own short process.
    """

    def __init__(self):
        self.port = None
        self.bound = False

    def audit(self, event, args):
        if (event.startswith("subprocess.") or event in {
                "os.system", "os.fork", "os.forkpty", "os.posix_spawn",
                "os.posix_spawnp", "os.spawn", "os.exec", "os.startfile",
                "os.startfile/2", "pty.spawn"}):
            raise BoundaryViolation("SUBPROCESS_BLOCKED")
        if event == "socket.__new__":
            # AF_INET=2, SOCK_STREAM=1. No UDP, IPv6, Unix, or raw sockets.
            if len(args) < 4 or args[1] != 2 or args[2] != 1 or args[3] not in (0, 6):
                raise BoundaryViolation("SOCKET_TYPE_BLOCKED")
        elif event == "socket.bind":
            if self.bound or len(args) < 2 or args[1] != ("127.0.0.1", 0):
                raise BoundaryViolation("BIND_BLOCKED")
            self.bound = True
        elif event == "socket.connect":
            if (self.port is None or len(args) < 2
                    or args[1] != ("127.0.0.1", self.port)):
                raise BoundaryViolation("CONNECT_BLOCKED")
        elif event == "socket.getaddrinfo":
            if (self.port is None or len(args) < 2
                    or args[0] != "127.0.0.1" or args[1] != self.port):
                raise BoundaryViolation("NAME_LOOKUP_BLOCKED")
        elif event in {
                "socket.gethostbyname", "socket.gethostbyaddr", "socket.getnameinfo",
                "socket.sendto", "socket.sendmsg"}:
            raise BoundaryViolation("NETWORK_OPERATION_BLOCKED")


def compile_sources(code_dir):
    """Read/compile only the three fixed files; no imports or file writes."""
    try:
        root = Path(code_dir).resolve(strict=True)
        if not root.is_dir():
            raise SmokeFailure("CODE_DIRECTORY_INVALID")
        sources = {}
        for name in MODULE_NAMES:
            source = (root / (name + ".py")).resolve(strict=True)
            if source.parent != root or not source.is_file():
                raise SmokeFailure("SOURCE_LOCATION_INVALID")
            with source.open("rb") as stream:
                raw = stream.read(MAX_SOURCE_BYTES + 1)
            if len(raw) > MAX_SOURCE_BYTES:
                raise SmokeFailure("SOURCE_TOO_LARGE")
            sources[name] = (source, compile(raw, str(source), "exec"))
        own_source = Path(__file__).resolve().read_bytes()
        compile(own_source, "isolated_http_smoke.py", "exec")
        return sources
    except SmokeFailure:
        raise
    except (OSError, RuntimeError, SyntaxError, ValueError, UnicodeError):
        raise SmokeFailure("SOURCE_VALIDATION_FAILED") from None


def require(condition, code):
    if not condition:
        raise SmokeFailure(code)


def transport_failure_code(error):
    """Keep numeric OS diagnostics without exception text, URLs, or payloads."""
    code = "TRANSPORT_FAILURE"
    for field in ("errno", "winerror"):
        value = getattr(error, field, None)
        if type(value) is int:
            code += " " + field + "=" + str(value)
    return code


def run_smoke(sources):
    # Dry-run never reaches these imports, the audit hook, temp directory,
    # runtime module execution, any thread, or any socket operation.
    import importlib.util
    from http.client import HTTPConnection, HTTPException
    import json
    import socket
    import tempfile

    guard = BoundaryGuard()
    sys.addaudithook(guard.audit)
    # HTTPServer.server_bind normally calls getfqdn(), which can do reverse DNS.
    # The isolated literal-loopback fixture needs no hostname resolution.
    original_getfqdn = socket.getfqdn
    socket.getfqdn = lambda _name="": "localhost"
    server = None
    assets_context = None
    phase = "runtime_import"
    primary_failure = None
    cleanup_failed = False
    try:
        modules = {}
        for name in MODULE_NAMES:
            require(name not in sys.modules, "RUNTIME_MODULE_ALREADY_IMPORTED")
            source, compiled = sources[name]
            spec = importlib.util.spec_from_file_location(name, source)
            require(spec is not None, "IMPORT_SPEC_FAILED")
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            exec(compiled, module.__dict__)
            modules[name] = module
        require("main" not in sys.modules and "voice_ai" not in sys.modules,
                "FORBIDDEN_RUNTIME_IMPORT")

        assets_context = tempfile.TemporaryDirectory(prefix="doran-isolated-http-")
        assets = assets_context.name
        state = modules["display_state"].DisplayState()
        server = modules["display_server"].DisplayServer(
            state=state, controller=None, host="127.0.0.1", port=0,
            assets_dir=assets)
        phase = "loopback_start"
        server.start()
        host, port = server.address
        require(host == "127.0.0.1" and type(port) is int and 0 < port < 65536,
                "LISTENER_ADDRESS_INVALID")
        guard.port = port
        authority = "127.0.0.1:" + str(port)
        origin = "http://" + authority

        def request(method, path, body=None, supplied_origin=None):
            headers = {"Host": authority}
            if method == "POST":
                headers.update({"Origin": supplied_origin or origin,
                                "X-Doran-Action": "radio",
                                "Content-Type": "application/json"})
            connection = HTTPConnection(host, port, timeout=2)
            try:
                # Keep the real HTTPConnection body send path, including for
                # rejected Origin. Transport resets must FAIL, not become 403.
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                require(len(payload) <= MAX_RESPONSE_BYTES, "RESPONSE_TOO_LARGE")
                require(response.getheader("Content-Type", "").startswith(
                    "application/json"), "RESPONSE_CONTENT_TYPE")
                try:
                    decoded = json.loads(payload)
                except (ValueError, UnicodeError, RecursionError):
                    raise SmokeFailure("RESPONSE_INVALID_JSON") from None
                require(isinstance(decoded, dict), "RESPONSE_NOT_OBJECT")
                return response.status, decoded
            except (OSError, HTTPException) as exc:
                raise SmokeFailure(transport_failure_code(exc)) from None
            finally:
                connection.close()

        def get_state():
            status, snapshot = request("GET", "/api/state")
            require(status == 200, "STATE_HTTP_STATUS")
            require(snapshot == {
                "state": "idle", "busy": False, "activity": None, "alert": None,
                "audio_focus": {"blocked": False, "revision": 0,
                                "non_game_revision": 0},
                "can_start": False,
            }, "ISOLATED_STATE_MISMATCH")
            return snapshot

        def radio_state():
            # /api/state omits playing. Compare the actual fixture flag too,
            # otherwise a forbidden request could mutate it undetected.
            with state._lock:
                return state.snapshot(), state._radio_playing

        def post_radio(playing, expected_status=200, supplied_origin=None):
            body = '{"playing":true}' if playing else '{"playing":false}'
            status, result = request("POST", "/api/radio-focus", body,
                                     supplied_origin)
            require(status == expected_status, "RADIO_HTTP_STATUS")
            expected = "accepted" if expected_status == 200 else "forbidden"
            require(result.get("status") == expected, "RADIO_RESULT_STATUS")
            if expected_status == 200:
                require(radio_state()[1] is playing, "RADIO_INTENT_MISMATCH")
                require(result.get("audio_focus") == state.snapshot()["audio_focus"],
                        "RADIO_FOCUS_MISMATCH")
            else:
                require(result == {"status": "forbidden"}, "FORBIDDEN_RESPONSE")

        phase = "initial_get"
        get_state()
        print("PASS initial GET /api/state; idle, controller unavailable")
        phase = "valid_false_true"
        post_radio(False)
        post_radio(True)
        get_state()
        print("PASS valid radio false/true; HTTP 200 accepted")

        for forbidden_playing in (False, True):
            phase = "bad_origin_pause" if not forbidden_playing else "bad_origin_play"
            before_api = get_state()
            before_fixture = radio_state()
            require(before_fixture[1] is not forbidden_playing, "ATTACK_NOT_MUTATING")
            post_radio(forbidden_playing, 403, "http://evil.invalid")
            require(get_state() == before_api, "FORBIDDEN_CHANGED_API_STATE")
            require(radio_state() == before_fixture, "FORBIDDEN_CHANGED_FIXTURE_STATE")
            print("PASS " + phase + "; HTTP 403 forbidden, full state unchanged")
            phase = "valid_request_after_forbidden"
            post_radio(forbidden_playing)
            get_state()
            print("PASS normal POST/GET after rejection; server responsive")

        phase = "final_pause"
        post_radio(False)
        get_state()
        phase = "cleanup"
        # Stop while the temporary assets still exist; the outer finally
        # also attempts cleanup if any earlier assertion/transport failed.
        server_thread = server._thread
        server.stop()
        require(server._server is None and server._thread is None,
                "SERVER_CLEANUP_INCOMPLETE")
        require(server_thread is not None and not server_thread.is_alive(),
                "SERVER_THREAD_STILL_RUNNING")
        server = None
    except SmokeFailure as exc:
        primary_failure = (phase, str(exc))
    except Exception:
        primary_failure = (phase, "UNEXPECTED_FAILURE")
    finally:
        if server is not None:
            try:
                remaining_thread = server._thread
                server.stop()
                if remaining_thread is not None and remaining_thread.is_alive():
                    cleanup_failed = True
            except Exception:
                cleanup_failed = True
        # The temporary directory outlives stop()/join on every exit path.
        if assets_context is not None:
            try:
                assets_context.cleanup()
            except Exception:
                primary_failure = primary_failure or ("cleanup", "TEMP_ASSETS_CLEANUP_FAILED")
        socket.getfqdn = original_getfqdn
    if primary_failure is not None:
        print("FAIL " + primary_failure[0] + ": " + primary_failure[1], file=sys.stderr)
    if cleanup_failed:
        print("FAIL cleanup: SERVER_STOP_FAILED", file=sys.stderr)
    if primary_failure is not None or cleanup_failed:
        return 1
    print("PASS isolated HTTP smoke; server stopped. No cloud, SDK, or physical audio tested.")
    return 0


def main(argv=None):
    sys.dont_write_bytecode = True
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-dir", required=True,
                        help="Directory containing the three deployed display Python modules")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compile and print the plan only; no runtime imports/server/thread/socket")
    args = parser.parse_args(argv)
    try:
        sources = compile_sources(args.code_dir)
    except SmokeFailure as exc:
        print("FAIL preflight: " + str(exc), file=sys.stderr)
        return 2
    if args.dry_run:
        print("DRY RUN PASS: script and display_server/display_state/radio_presets compiled.")
        print("PLAN: real isolated DisplayState + DisplayServer(controller=None, port=0), loopback only.")
        print("PLAN: GET state; valid false/true POST; bad-Origin toggles rejected unchanged; normal POST/GET; stop.")
        print("NOT RUN: runtime imports, server, threads, sockets, HTTP, cloud, SDK, physical audio.")
        return 0
    return run_smoke(sources)


if __name__ == "__main__":
    raise SystemExit(main())
