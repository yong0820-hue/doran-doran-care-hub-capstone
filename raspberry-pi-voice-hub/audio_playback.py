"""Explicit ALSA output routing for local mpg123 announcements."""

import os


DEFAULT_PLAYBACK_DEVICE = "plughw:CARD=USB,DEV=0"


def playback_device_from_env():
    device = os.getenv("AI_PLAYBACK_DEVICE", DEFAULT_PLAYBACK_DEVICE).strip()
    if not device:
        raise ValueError("AI_PLAYBACK_DEVICE must name an ALSA playback device")
    return device


def with_playback_device(command, device):
    """Route mpg123 through ALSA unless the caller already chose a device."""
    command = list(command)
    if not command or command[0] != "mpg123":
        raise ValueError("Expected an mpg123 command")
    if not isinstance(device, str) or not device.strip():
        raise ValueError("AI_PLAYBACK_DEVICE must name an ALSA playback device")
    if any(option in ("-a", "--audiodevice")
           or option.startswith("--audiodevice=")
           or (option.startswith("-a") and len(option) > 2)
           for option in command[1:]):
        return command
    return [command[0], "-o", "alsa", "-a", device, *command[1:]]
