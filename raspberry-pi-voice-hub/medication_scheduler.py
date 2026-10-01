import os
import subprocess
import time

from gtts import gTTS

from audio_playback import playback_device_from_env, with_playback_device
from firebase_client import FirebaseClient

MEDICATION_TITLE = "\ubcf5\uc57d \uc548\ub0b4"
MEDICATION_MESSAGE = "\uc815\ud574\uc9c4 \ubcf5\uc57d \uc2dc\uac04\uc5d0 \uc74c\uc131 \uc548\ub0b4\ub97c \uc2e4\ud589\ud588\uc2b5\ub2c8\ub2e4."
MEDICATION_CONSOLE_LOG = "[Medication] \uc57d \ub4dc\uc2e4 \uc2dc\uac04\uc774\uc5d0\uc694"
TTS_TEXT = "\uc57d \ub4dc\uc2e4 \uc2dc\uac04\uc774\uc5d0\uc694"

AUDIO_DIR = "audio"
AUDIO_FILE = os.path.join(AUDIO_DIR, "medication_reminder.mp3")

# Future fixed schedule times: 08:00, 13:00, 20:00


def _generate_tts():
    os.makedirs(AUDIO_DIR, exist_ok=True)
    print(f"[TTS] creating {AUDIO_FILE}")
    gTTS(text=TTS_TEXT, lang="ko").save(AUDIO_FILE)
    print("[TTS] created")


def _play_audio():
    print("[Audio] playing medication reminder")
    subprocess.run(with_playback_device(
        ["mpg123", AUDIO_FILE], playback_device_from_env()), check=True)
    print("[Audio] playback complete")


def run_demo_reminder():
    """Demo: one reminder 30 seconds after startup.
    Future: replace with time-based loop for 08:00, 13:00, 20:00."""
    print("[Medication] Waiting 30 seconds before reminder...")
    time.sleep(30)

    print(MEDICATION_CONSOLE_LOG)

    try:
        _generate_tts()
        _play_audio()
    except Exception as e:
        print(f"[Audio] playback failed: {e}")

    client = FirebaseClient()
    success = client.write_medication_reminder(
        title=MEDICATION_TITLE,
        message=MEDICATION_MESSAGE,
    )

    if success:
        print("[Medication] Firebase event written.")
    else:
        print("[Medication] Firebase write failed.")
