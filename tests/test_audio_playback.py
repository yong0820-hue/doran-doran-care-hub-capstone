"""Offline checks for explicit ALSA routing without a physical sound device."""

import os
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "raspberry-pi-voice-hub"))
from audio_playback import (DEFAULT_PLAYBACK_DEVICE, playback_device_from_env,
                            with_playback_device)
from voice_ai import VoiceSettings


class AudioPlaybackTests(unittest.TestCase):
    def test_default_and_separate_capture_playback_settings(self):
        with patch.dict(os.environ, {}, clear=True):
            defaults = VoiceSettings.from_env()
        self.assertEqual(defaults.playback_device, DEFAULT_PLAYBACK_DEVICE)
        with patch.dict(os.environ, {
                "AI_AUDIO_DEVICE": "plughw:CARD=XVF,DEV=0",
                "AI_PLAYBACK_DEVICE": "plughw:CARD=Speaker,DEV=1",
        }, clear=True):
            settings = VoiceSettings.from_env()
            self.assertEqual(playback_device_from_env(), settings.playback_device)
        self.assertEqual(settings.audio_device, "plughw:CARD=XVF,DEV=0")
        self.assertEqual(settings.playback_device, "plughw:CARD=Speaker,DEV=1")

    def test_inserts_alsa_device_once_and_preserves_existing_option(self):
        device = "plughw:CARD=USB,DEV=0"
        expected = ["mpg123", "-o", "alsa", "-a", device, "-q", "prompt.mp3"]
        self.assertEqual(with_playback_device(["mpg123", "-q", "prompt.mp3"], device),
                         expected)
        for command in (
                expected,
                ["mpg123", "--audiodevice", "hw:2,0", "prompt.mp3"],
                ["mpg123", "--audiodevice=hw:2,0", "prompt.mp3"],
                ["mpg123", "-ahw:2,0", "prompt.mp3"],
        ):
            with self.subTest(command=command):
                self.assertEqual(with_playback_device(command, device), command)

    def test_empty_playback_device_is_rejected(self):
        with patch.dict(os.environ, {"AI_PLAYBACK_DEVICE": " "}, clear=True):
            with self.assertRaises(ValueError):
                playback_device_from_env()

    def test_standalone_demo_reminder_routes_to_playback_device(self):
        hub_dir = Path(__file__).resolve().parents[1] / "raspberry-pi-voice-hub"
        spec = importlib.util.spec_from_file_location(
            "standalone_medication_scheduler", hub_dir / "medication_scheduler.py")
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {
                "gtts": types.SimpleNamespace(gTTS=Mock()),
                "firebase_client": types.SimpleNamespace(FirebaseClient=Mock()),
        }):
            spec.loader.exec_module(module)
        with patch.dict(os.environ, {"AI_PLAYBACK_DEVICE": "plughw:CARD=Speaker,DEV=1"}), \
                patch.object(module.subprocess, "run") as run:
            module._play_audio()
        self.assertEqual(run.call_args.args[0],
                         ["mpg123", "-o", "alsa", "-a",
                          "plughw:CARD=Speaker,DEV=1", module.AUDIO_FILE])


if __name__ == "__main__":
    unittest.main()
