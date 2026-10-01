"""Offline checks for the imported diagnostic and prefetch timeout behavior."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "raspberry-pi-voice-hub"), str(ROOT / "tests")]

from firebase_auth import DiagnosticReporter, FirebaseTransportError, safe_error_details
from medication_schedule import MedicationScheduleProvider
from persistent_memory import MemoryStore
from test_memory_cache import record
from test_persistent_memory import FakeFirebase


class DiagnosticsTests(unittest.TestCase):
    def test_transport_categories_and_conditional_412(self):
        self.assertEqual(safe_error_details(TimeoutError("PRIVATE"))["category"], "timeout_unknown")
        self.assertEqual(safe_error_details(status=412, conditional=True)["category"], "precondition_failed")
        self.assertEqual(safe_error_details(status=412, conditional=False)["category"], "http_error")
        error = FirebaseTransportError("fixed", category="transport_rejected", request_phase="before_request")
        self.assertEqual(safe_error_details(error)["request_phase"], "before_request")

    def test_diagnostic_reporter_redacts_and_rate_limits(self):
        now = [0.0]
        reporter = DiagnosticReporter("memory_prefetch", clock=lambda: now[0], interval=60)
        output = io.StringIO()
        fields = dict(method="GET", path_template="/memories/{elder}/_control.json",
                      stage="control_read", category="http_error", http_status=403,
                      request_phase="response", url="PRIVATE_TOKEN", elder_id="PRIVATE_ELDER")
        with redirect_stdout(output):
            reporter.failure(**fields)
            reporter.failure(**fields)
            now[0] = 61
            reporter.failure(**fields)
            reporter.success(method="GET", path_template="/memories/{elder}/_control.json")
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        events = [json.loads(line.split(" ", 1)[1]) for line in lines]
        self.assertEqual([event["event"] for event in events],
                         ["failure", "failure_summary", "recovered"])
        self.assertEqual(events[1]["suppressed_count"], 1)
        self.assertNotIn("PRIVATE", output.getvalue())

    def test_medication_publish_failure_then_retry_recovery(self):
        class Http:
            def __init__(self):
                self.statuses = [412, 200]
                self.calls = []

            def patch(self, url, json, timeout):
                self.calls.append((url, json, timeout))
                return SimpleNamespace(status_code=self.statuses.pop(0))

        http = Http()
        provider = MedicationScheduleProvider("https://example.invalid", "elder_001",
                                              "device_001", ["08:00"], http=http)
        output = io.StringIO()
        with redirect_stdout(output):
            provider._publish_status(provider.snapshot())
            self.assertIsNone(provider._published_key)
            provider._publish_status(provider.snapshot())
        events = [json.loads(line.split(" ", 1)[1]) for line in output.getvalue().splitlines()]
        self.assertEqual([event["event"] for event in events], ["failure", "recovered"])
        self.assertEqual(events[0]["operation"], "medication_apply_status_publish")
        self.assertEqual((events[0]["http_status"], events[0]["category"]), (412, "http_error"))
        self.assertEqual(events[0]["target_template"], "/devices/{device}/medicationScheduleStatus")
        self.assertEqual(len(http.calls), 2)
        self.assertTrue(all(call[2] == 10 for call in http.calls))
        self.assertTrue(all(set(call[1]) == {"devices/device_001/medicationScheduleStatus"} for call in http.calls))

    def test_prefetch_timeout_only_on_control_and_body(self):
        http = FakeFirebase({"old": record()})
        store = MemoryStore("https://example.invalid", "elder_1", http)
        self.assertEqual(store.REQUEST_TIMEOUT, (0.25, 0.5))
        self.assertEqual(store.PREFETCH_REQUEST_TIMEOUT, (0.75, 1.0))
        store.cache_snapshot()
        self.assertEqual(len(http.get_calls), 2)
        self.assertEqual([kwargs["timeout"] for _, kwargs in http.get_calls], [(0.75, 1.0)] * 2)
        http.get_calls.clear()
        store.read()
        self.assertEqual([kwargs["timeout"] for _, kwargs in http.get_calls], [(0.25, 0.5)] * 2)

    def test_control_failure_keeps_stage_and_status(self):
        http = FakeFirebase()
        http.read_failure = True
        store = MemoryStore("https://example.invalid", "elder_1", http)
        with self.assertRaises(ConnectionError) as raised:
            store.cache_snapshot()
        self.assertEqual(raised.exception._memory_diagnostic["stage"], "control_read")
        self.assertEqual(raised.exception._memory_diagnostic["path_template"],
                         "/memories/{elder}/_control.json")

    def test_body_failure_keeps_body_stage(self):
        http = FakeFirebase({"old": record()})
        original_get = http.get

        def broken_get(url, **kwargs):
            if "/active/" in url:
                raise TimeoutError("PRIVATE")
            return original_get(url, **kwargs)

        http.get = broken_get
        store = MemoryStore("https://example.invalid", "elder_1", http)
        with self.assertRaises(TimeoutError) as raised:
            store.cache_snapshot()
        self.assertEqual(raised.exception._memory_diagnostic["stage"], "body_read")
        self.assertEqual(raised.exception._memory_diagnostic["path_template"],
                         "/memories/{elder}/active/{body}.json")


if __name__ == "__main__":
    unittest.main()
