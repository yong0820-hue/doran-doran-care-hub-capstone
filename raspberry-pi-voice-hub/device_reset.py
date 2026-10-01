"""Non-executing device reset interface for future guardian/GPIO integrations.

This v1 module deliberately has no filesystem, Firebase, subprocess or GPIO
adapter. Completing confirmations returns ``unsupported``; it never resets a
device or authorizes deletion of cloud data.
"""

import math


LOCAL_SCOPE = ("local_cache", "local_conversation_state", "local_device_preferences")
PROTECTED_SCOPE = (
    "persistent_memories", "conversation_history", "care_events",
    "medication_schedules", "device_configuration", "guardian_account",
)


def device_reset_plan():
    """Describe a future local-only reset, without loading any device data."""
    return {
        "version": 1,
        "supported": False,
        "localScope": list(LOCAL_SCOPE),
        "preservedScope": list(PROTECTED_SCOPE),
        "requires": ["display_confirmation", "guardian_confirmation"],
    }


def request_device_reset(*, source="guardian", hold_seconds=None,
                         display_confirmed=False, guardian_confirmed=False):
    """Evaluate a request without side effects, even after all confirmations.

    Physical integrations may call this after measuring a 9–10 second hold.
    A short press (including eight seconds or less) cannot open reset approval.
    Confirmation flags must be explicit booleans; truthy strings cannot approve.
    """
    plan = device_reset_plan()
    if not isinstance(source, str) or source not in {"guardian", "physical"}:
        return {**plan, "status": "ignored", "reason": "invalid_source"}
    if source == "physical" and (
        not isinstance(hold_seconds, (int, float))
        or isinstance(hold_seconds, bool)
        or hold_seconds <= 8
        or hold_seconds > 3600
        or not math.isfinite(hold_seconds)
    ):
        return {**plan, "status": "ignored", "reason": "long_press_required"}
    required = []
    if display_confirmed is not True:
        required.append("display_confirmation")
    if guardian_confirmed is not True:
        required.append("guardian_confirmation")
    return {
        **plan,
        "status": "confirmation_required" if required else "unsupported",
        "requires": required,
    }
