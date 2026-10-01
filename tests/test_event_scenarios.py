"""Offline cross-feature scenarios for Pi event writers and dashboard readers.

The scenarios use the same stateful HTTP doubles as the focused Firebase
tests, so no Firebase, audio, Pi hardware, or network service is contacted.
"""

from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"
HUB_DIR = REPO_ROOT / "raspberry-pi-voice-hub"
for path in (str(TESTS_DIR), str(HUB_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from firebase_inactivity import FirebaseInactivityClient
from firebase_medication import FirebaseMedicationClient
from test_firebase_inactivity import FakeFirebase as InactivityFirebase, source
from test_firebase_medication import FakeFirebase as MedicationFirebase, reminder


SIX_MEDICATION_RESULTS = (
    ("confirmed", "confirmed", "resolved"),
    ("defer", "deferred", "open"),
    ("no_speech", "no_speech", "open"),
    ("unclear", "unclear", "open"),
    ("cancelled", "cancelled", "open"),
    ("error", "error", "open"),
)


class ConcurrentEventScenarioTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        if not REPO_ROOT.is_dir():
            self.skipTest(f"repository not found: {REPO_ROOT}")
        self.output = io.StringIO()
        self.quiet = redirect_stdout(self.output)
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)
        self.addCleanup(self.output.close)

    def _make_medication_events(self, created_at):
        http = MedicationFirebase()
        clients = []
        callback_calls = []
        for index, (callback_result, _dashboard_state, expected_status) in enumerate(
            SIX_MEDICATION_RESULTS
        ):
            client = FirebaseMedicationClient(
                "https://example.firebaseio.com/", "elder_001", "pi_001", http=http
            )
            slot = f"2026-09-15_{8 + index:02d}:00"
            event = reminder(
                createdAt=created_at,
                runId=f"medication_{callback_result}",
                meta={"scheduleId": f"schedule_{callback_result}"},
            )

            def callback(event_id, result=callback_result):
                callback_calls.append((result, event_id))
                if result == "error":
                    raise RuntimeError("offline simulated callback failure")
                return result

            event_id = client._event_id(slot, event)
            self.assertTrue(client.run_scheduled(slot, event, callback))
            self.assertEqual(callback_calls[-1], (callback_result, event_id))
            stored = http.records[event_id]
            marker = stored["meta"]["voiceCheck"]
            self.assertEqual(marker["outcome"], callback_result)
            self.assertEqual(marker["status"], "completed")
            self.assertEqual(stored["status"], expected_status)
            clients.append((client, slot, event, callback))
        self.assertEqual(len(http.records), len(SIX_MEDICATION_RESULTS))
        return deepcopy(http.records), clients, callback_calls

    def _dashboard_projection(self, events):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the dashboard projection check")
        module_url = (REPO_ROOT / "apps" / "guardian-dashboard" / "src" /
                      "dashboardState.js").as_uri()
        medication_module_url = (
            REPO_ROOT / "apps" / "guardian-dashboard" / "src" / "medicationSchedule.js"
        ).as_uri()
        script = f"""
          import {{ summarizeEvents, getMedicationView }} from {json.dumps(module_url)};
          import {{ buildTodayMedicationRows }} from {json.dumps(medication_module_url)};
          let raw = '';
          process.stdin.setEncoding('utf8');
          for await (const chunk of process.stdin) raw += chunk;
          const events = JSON.parse(raw);
          const summary = summarizeEvents(events);
          const medicationStates = events
            .filter((event) => event.type === 'medication_reminder_sent')
            .map((event) => getMedicationView([event]).state)
            .sort();
          const medicationCardStates = buildTodayMedicationRows(
            null,
            events,
            Date.parse('2026-09-15T12:00:00+09:00'),
            'pi_001',
          ).map((row) => row.state).sort();
          process.stdout.write(JSON.stringify({{
            totalCount: summary.totalCount,
            openCount: summary.openCount,
            resolvedCount: summary.resolvedCount,
            unknownCount: summary.unknownCount,
            medicationStates,
            medicationCardStates,
          }}));
        """
        result = subprocess.run(
            [node, "--input-type=module", "-e", script],
            cwd=REPO_ROOT,
            input=json.dumps(list(events.values()), ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_simultaneous_sos_six_medication_results_and_inactivity_stay_independent(self):
        """Exercise same-timestamp state flow; thread concurrency is covered separately."""
        same_timestamp = 1_789_389_000_000
        medication_events, medication_runs, callback_calls = self._make_medication_events(
            same_timestamp
        )
        sos = {
            "type": "emergency_call",
            "status": "open",
            "severity": "critical",
            "createdAt": same_timestamp,
            "deviceId": "pi_001",
            "elderId": "elder_001",
            "title": "긴급 호출",
        }
        inactivity = source(
            created_at=same_timestamp,
            deviceId="pi_001",
            elderId="elder_001",
        )
        initial = {
            "sos_same_time": deepcopy(sos),
            "inactivity_same_time": deepcopy(inactivity),
            **deepcopy(medication_events),
        }
        http = InactivityFirebase(initial)
        client = FirebaseInactivityClient(
            "https://example.firebaseio.com/", "elder_001", "pi_001", http=http
        )

        self.assertTrue(client.run_once(lambda _event_id: "help"))
        self.assertEqual(http.records["sos_same_time"], sos)
        for event_id, event in medication_events.items():
            self.assertEqual(http.records[event_id], event)
        handled = http.records["inactivity_same_time"]
        self.assertEqual(handled["type"], "long_inactivity")
        self.assertEqual(handled["status"], "open")
        self.assertEqual(handled["severity"], "critical")
        self.assertEqual(handled["meta"]["voiceCheck"]["outcome"], "help")

        # A second poll and repeated medication slots must not add events or replay callbacks.
        self.assertFalse(client.run_once(lambda _event_id: self.fail("duplicate inactivity callback")))
        callbacks_before_retries = list(callback_calls)
        for med_client, slot, event, callback in medication_runs:
            self.assertFalse(med_client.run_scheduled(slot, event, callback))
        self.assertEqual(callback_calls, callbacks_before_retries)
        self.assertEqual(len(http.records), 8)

        projection = self._dashboard_projection(http.records)
        self.assertEqual(
            projection,
            {
                "totalCount": 8,
                "openCount": 7,
                "resolvedCount": 1,
                "unknownCount": 0,
                "medicationStates": sorted(
                    dashboard_state for _, dashboard_state, _ in SIX_MEDICATION_RESULTS
                ),
                "medicationCardStates": sorted(
                    dashboard_state for _, dashboard_state, _ in SIX_MEDICATION_RESULTS
                ),
            },
        )

    def test_statusless_legacy_inactivity_help_is_persisted_as_open_for_guardians(self):
        legacy = source(created_at=1_789_389_000_000)
        legacy.pop("status")
        http = InactivityFirebase({"legacy_inactivity": legacy})
        client = FirebaseInactivityClient(
            "https://example.firebaseio.com/", "elder_001", "pi_001", http=http
        )

        self.assertTrue(client.run_once(lambda _event_id: "help"))
        stored = http.records["legacy_inactivity"]
        self.assertEqual(stored["meta"]["voiceCheck"]["outcome"], "help")
        self.assertEqual(stored["severity"], "critical")
        self.assertEqual(
            stored.get("status"),
            "open",
            "A legacy statusless source is treated as open by the Pi client, so the "
            "completed help result must persist explicit status=open for the dashboard.",
        )
        projection = self._dashboard_projection(http.records)
        self.assertEqual(projection["openCount"], 1)
        self.assertEqual(projection["unknownCount"], 0)


if __name__ == "__main__":
    unittest.main()
