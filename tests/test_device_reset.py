from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "raspberry-pi-voice-hub"))

from device_reset import device_reset_plan, request_device_reset


class DeviceResetInterfaceTests(unittest.TestCase):
    def test_plan_only_names_local_reset_scope(self):
        plan = device_reset_plan()
        self.assertFalse(plan["supported"])
        self.assertEqual(plan["localScope"], [
            "local_cache", "local_conversation_state", "local_device_preferences",
        ])
        for protected in ("persistent_memories", "conversation_history", "care_events",
                          "medication_schedules", "device_configuration", "guardian_account"):
            self.assertIn(protected, plan["preservedScope"])

    def test_short_physical_presses_never_reach_confirmation(self):
        for duration in (None, False, "10", -1, 0, 1, 8, 10 ** 1000,
                         float("nan"), float("inf")):
            with self.subTest(duration=duration):
                result = request_device_reset(source="physical", hold_seconds=duration,
                                              display_confirmed=True, guardian_confirmed=True)
                self.assertEqual(result["status"], "ignored")

    def test_long_press_requires_display_and_guardian_confirmation(self):
        for duration in (9, 10):
            result = request_device_reset(source="physical", hold_seconds=duration)
            self.assertEqual(result["status"], "confirmation_required")
            self.assertEqual(result["requires"], ["display_confirmation", "guardian_confirmation"])
        result = request_device_reset(source="physical", hold_seconds=10, display_confirmed=True)
        self.assertEqual(result["requires"], ["guardian_confirmation"])
        result = request_device_reset(display_confirmed="true", guardian_confirmed=1)
        self.assertEqual(result["requires"], ["display_confirmation", "guardian_confirmation"])

    def test_fully_confirmed_interface_has_no_filesystem_or_cloud_side_effect(self):
        with patch("builtins.open", side_effect=AssertionError("No file access")), \
                patch.object(Path, "unlink", side_effect=AssertionError("No deletion")):
            for source in ("guardian", "physical"):
                result = request_device_reset(source=source, hold_seconds=10,
                                              display_confirmed=True, guardian_confirmed=True)
                self.assertEqual(result["status"], "unsupported")
                self.assertFalse(result["supported"])
        for source in ("unknown", None, ["physical"]):
            self.assertEqual(request_device_reset(source=source)["status"], "ignored")


if __name__ == "__main__":
    unittest.main()
