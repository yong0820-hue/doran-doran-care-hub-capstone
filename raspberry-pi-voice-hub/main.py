import os
import sys
import time
import json
import subprocess
import threading
from pathlib import Path
from datetime import datetime

import requests
from dotenv import load_dotenv
from gtts import gTTS

from audio_playback import playback_device_from_env, with_playback_device
from display_state import DISPLAY
from firebase_auth import FirebaseAuthError, FirebaseTransportError
from medication_schedule import (
    MedicationScheduleCursor,
    MedicationScheduleProvider,
)


FIREBASE_RUNTIME_MODE = os.getenv("FIREBASE_RUNTIME_MODE", "legacy").strip()
if FIREBASE_RUNTIME_MODE != "isolated_authenticated":
    load_dotenv(dotenv_path=".env")

FIREBASE_DATABASE_URL = os.getenv("FIREBASE_DATABASE_URL", "").rstrip("/")
ELDER_ID = os.getenv("ELDER_ID", "elder_001")
DEVICE_ID = os.getenv("DEVICE_ID", "device_001")
LOCATION_NAME = os.getenv("LOCATION_NAME", "\uac70\uc2e4")  # 거실

REMINDER_MODE = os.getenv("REMINDER_MODE", "demo").strip().lower()
MEDICATION_TIMES_RAW = os.getenv("MEDICATION_TIMES", "08:00,12:00,19:00")
MEDICATION_SCHEDULE_POLL_SECONDS_RAW = os.getenv(
    "MEDICATION_SCHEDULE_POLL_SECONDS", "5"
)

AUDIO_DIR = Path("audio")
AUDIO_LOCK = threading.Lock()
_FIREBASE_HTTP = requests
_AUTHENTICATED_TRANSPORT_CONFIGURED = False
_ISOLATED_OPENAI_KEY = None


def configure_firebase_transport(http, *, mode="isolated_authenticated"):
    """Called only by the explicit launcher after device authentication succeeds."""
    from firebase_auth import AuthenticatedFirebaseHTTP
    if (mode != "isolated_authenticated" or FIREBASE_RUNTIME_MODE != mode
            or not isinstance(http, AuthenticatedFirebaseHTTP)
            or http.database_url != FIREBASE_DATABASE_URL
            or http.elder_id != ELDER_ID or http.device_id != DEVICE_ID):
        raise ValueError("Authenticated Firebase transport does not match runtime scope")
    http.provider.get_id_token()
    global _FIREBASE_HTTP, _AUTHENTICATED_TRANSPORT_CONFIGURED
    _FIREBASE_HTTP = http
    _AUTHENTICATED_TRANSPORT_CONFIGURED = True


def configure_openai_key(value):
    """Supply a key loaded by the isolated launcher without environment exposure."""
    if (FIREBASE_RUNTIME_MODE != "isolated_authenticated" or not isinstance(value, str)
            or not value or not value.isascii() or any(c.isspace() for c in value)):
        raise ValueError("Isolated OpenAI key is unavailable")
    global _ISOLATED_OPENAI_KEY
    _ISOLATED_OPENAI_KEY = value


def _firebase_http_kwargs():
    if FIREBASE_RUNTIME_MODE not in {"legacy", "isolated_authenticated"}:
        raise ValueError("Unsupported Firebase runtime mode")
    if FIREBASE_RUNTIME_MODE == "isolated_authenticated":
        if not _AUTHENTICATED_TRANSPORT_CONFIGURED:
            raise RuntimeError("Authenticated Firebase transport must be configured before startup")
        return {"http": _FIREBASE_HTTP}
    return {}

TEXT_MEDICATION = "\uc57d \ub4dc\uc2e4 \uc2dc\uac04\uc774\uc5d0\uc694"  # 약 드실 시간이에요
TEXT_INACTIVITY = "\uad1c\ucc2e\uc73c\uc138\uc694? \uc6c0\uc9c1\uc784\uc774 \uac10\uc9c0\ub418\uc9c0 \uc54a\uc558\uc5b4\uc694."  # 괜찮으세요? 움직임이 감지되지 않았어요.

TITLE_MEDICATION = "\ubcf5\uc57d \uc548\ub0b4"  # 복약 안내
MESSAGE_MEDICATION = "\uc815\ud574\uc9c4 \ubcf5\uc57d \uc2dc\uac04\uc5d0 \uc74c\uc131 \uc548\ub0b4\ub97c \uc2e4\ud589\ud588\uc2b5\ub2c8\ub2e4."

TITLE_INACTIVITY = "\uc548\ubd80 \uc74c\uc131 \ud655\uc778"  # 안부 음성 확인
MESSAGE_INACTIVITY = "\uc7a5\uc2dc\uac04 \ubbf8\ud65c\ub3d9 \uac10\uc9c0 \ud6c4 \ub77c\uc988\ubca0\ub9ac\ud30c\uc774\uac00 \uc548\ubd80 \uc74c\uc131 \uc548\ub0b4\ub97c \uc2e4\ud589\ud588\uc2b5\ub2c8\ub2e4."


def now_ms():
    return int(time.time() * 1000)


def get_medication_times():
    times = []

    for item in MEDICATION_TIMES_RAW.split(","):
        item = item.strip()

        if not item:
            continue

        try:
            parsed = datetime.strptime(item, "%H:%M")
            times.append(parsed.strftime("%H:%M"))
        except ValueError:
            print(f"[Medication] invalid time ignored: {item}")

    return times or ["08:00", "12:00", "19:00"]


def get_medication_schedule_poll_seconds():
    try:
        seconds = float(MEDICATION_SCHEDULE_POLL_SECONDS_RAW)
        if not 1 <= seconds <= 60:
            raise ValueError
        return seconds
    except (TypeError, ValueError):
        print("[MedicationSchedule] invalid poll interval; using 5 seconds.")
        return 5


def start_medication_schedule_provider():
    try:
        return MedicationScheduleProvider(
            FIREBASE_DATABASE_URL,
            ELDER_ID,
            DEVICE_ID,
            get_medication_times(),
            poll_seconds=get_medication_schedule_poll_seconds(),
            **_firebase_http_kwargs(),
        ).start()
    except Exception as exc:
        print(f"[MedicationSchedule] startup failed ({type(exc).__name__}); "
              "no unclaimed medication audio will be played.")
        return None


def dispatch_medication_schedule_once(provider, cursor, dispatcher, now=None):
    """Enqueue current Seoul-time entries with their immutable schedule identity."""
    if provider is None or dispatcher is None:
        return 0
    due_items = cursor.due(provider.snapshot(), now=now)
    accepted = 0
    for due in due_items:
        event = build_medication_event(due.entry, due.schedule_revision)

        def still_current(entry=due.entry, date=due.date):
            return provider.is_current_occurrence(entry, date)

        if dispatcher.run_scheduled(
            due.slot_key,
            event=event,
            validity_check=still_current,
        ):
            accepted += 1
            print(f"[Hub] queued medication schedule={due.entry.schedule_id} "
                  f"slot={due.slot_key}")
    return accepted


def play_tts_message(text, filename, before_play=None):
    try:
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        audio_path = AUDIO_DIR / filename

        print(f"[TTS] creating {audio_path}")
        with DISPLAY.phase("thinking"):
            tts = gTTS(text, lang="ko")
            tts.save(str(audio_path))
        print(f"[TTS] created {audio_path}")

        print(f"[Audio] playing {audio_path}")
        with AUDIO_LOCK:
            if before_play is not None and not before_play():
                print("[Audio] playback cancelled because the schedule changed.")
                return False
            with DISPLAY.phase("speaking"):
                subprocess.run(with_playback_device(
                    ["mpg123", str(audio_path)], playback_device_from_env()), check=True)
        print("[Audio] playback complete")
        return True

    except Exception as exc:
        print(f"[Audio] TTS/playback failed: {exc}")
        return False


def firebase_patch_and_verify(event_id, event):
    if not FIREBASE_DATABASE_URL:
        print("[Firebase] FIREBASE_DATABASE_URL is missing. Check .env")
        return False

    event_path = f"events/{ELDER_ID}/{event_id}"
    timestamp = event["createdAt"]

    payload = {
        event_path: event,
        f"devices/{DEVICE_ID}/lastSeen": timestamp,
        f"devices/{DEVICE_ID}/lastSignal": timestamp,
    }

    print(f"[Firebase Debug] databaseURL: {FIREBASE_DATABASE_URL}")
    print(f"[Firebase Debug] eventPath: {event_path}")
    print(f"[Firebase Debug] runId: {event.get('runId')}")
    print(f"[Firebase Debug] createdAt: {timestamp}")

    try:
        patch_url = f"{FIREBASE_DATABASE_URL}/.json"
        http = _firebase_http_kwargs().get("http", requests)
        response = http.patch(patch_url, json=payload, timeout=10)

        print(f"[Firebase Debug] PATCH status: {response.status_code}")
        print(f"[Firebase Debug] PATCH response: {response.text[:300]}")

        if not (200 <= response.status_code < 300):
            print("[Firebase] write failed")
            return False

        verify_url = f"{FIREBASE_DATABASE_URL}/{event_path}.json"
        verify = http.get(verify_url, timeout=10)

        print(f"[Firebase Debug] verify URL: {verify_url}")
        print(f"[Firebase Debug] verify status: {verify.status_code}")

        data = verify.json()

        if not data or data.get("runId") != event.get("runId"):
            print("[Firebase Debug] VERIFY FAILED")
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return False

        print(f"[Firebase Debug] verify title: {data.get('title')}")
        print(f"[Firebase Debug] verify createdAt: {data.get('createdAt')}")
        return True

    except (requests.RequestException, FirebaseAuthError, FirebaseTransportError) as exc:
        print(f"[Firebase] request failed: {exc}")
        return False


def build_medication_event(schedule=None, schedule_revision=""):
    """Keep one event schema for legacy reminders and optional voice checks."""
    timestamp = now_ms()
    run_id = f"pi_medication_run_{timestamp}"

    event = {
        "deviceId": DEVICE_ID,
        "elderId": ELDER_ID,
        "type": "medication_reminder_sent",
        "severity": "info",
        "title": TITLE_MEDICATION,
        "message": MESSAGE_MEDICATION,
        "status": "open",
        "createdAt": timestamp,
        "location": LOCATION_NAME,
        "runId": run_id,
    }
    if schedule is not None:
        event["meta"] = {
            "scheduleId": schedule.schedule_id,
            "scheduleRevision": schedule_revision,
            "medicationName": schedule.medication_name,
        }
    return event


def write_medication_event():
    event = build_medication_event()
    event_id = f"pi_medication_reminder_sent_{event['createdAt']}"
    ok = firebase_patch_and_verify(event_id, event)

    if ok:
        print("[Medication] Firebase event written.")

    return ok


def write_inactivity_voice_check_event(source_event_id):
    timestamp = now_ms()
    event_id = f"pi_inactivity_voice_check_sent_{timestamp}"
    run_id = f"pi_inactivity_voice_check_run_{timestamp}"

    event = {
        "deviceId": DEVICE_ID,
        "elderId": ELDER_ID,
        "type": "inactivity_voice_check_sent",
        "severity": "warning",
        "title": TITLE_INACTIVITY,
        "message": MESSAGE_INACTIVITY,
        "status": "open",
        "createdAt": timestamp,
        "location": LOCATION_NAME,
        "runId": run_id,
        "sourceEventId": source_event_id,
    }

    ok = firebase_patch_and_verify(event_id, event)

    if ok:
        print("[Firebase] inactivity_voice_check_sent written.")

    return ok


def trigger_medication_reminder():
    print(f"[Medication] {TEXT_MEDICATION}")

    with DISPLAY.activity("medication"):
        play_tts_message(TEXT_MEDICATION, "medication_reminder.mp3")
        write_medication_event()


def announce_medication_reminder(validity_check=None):
    """Play one already-reserved announcement and return its delivery outcome."""
    if validity_check is not None and not validity_check():
        return "cancelled"
    print(f"[Medication] {TEXT_MEDICATION}")
    with DISPLAY.activity("medication"):
        played = play_tts_message(
            TEXT_MEDICATION,
            "medication_reminder.mp3",
            before_play=validity_check,
        )
    if validity_check is not None and not validity_check():
        return "cancelled"
    if not played:
        raise RuntimeError("Medication announcement playback failed")
    return "announced"


def trigger_inactivity_voice_check(source_event_id):
    print(f"[Inactivity] {TEXT_INACTIVITY}")

    with DISPLAY.activity("inactivity"):
        play_tts_message(TEXT_INACTIVITY, "inactivity_voice_check.mp3")
        write_inactivity_voice_check_event(source_event_id)


def run_demo_mode():
    print("[Medication] mode: demo")
    print("[Medication] Waiting 30 seconds before reminder...")
    time.sleep(30)
    trigger_medication_reminder()


def run_schedule_mode():
    print("[Medication] mode: schedule")
    provider = start_medication_schedule_provider()
    dispatcher = start_medication_announcement_care()
    cursor = MedicationScheduleCursor()
    print("[Medication] waiting for validated scheduled reminders...")
    try:
        while True:
            dispatch_medication_schedule_once(provider, cursor, dispatcher)
            time.sleep(5)
    finally:
        if dispatcher is not None:
            dispatcher.stop()
        if provider is not None:
            provider.stop()


def fetch_events():
    url = f"{FIREBASE_DATABASE_URL}/events/{ELDER_ID}.json"

    try:
        http = _firebase_http_kwargs().get("http", requests)
        response = http.get(url, timeout=10)

        if response.status_code != 200:
            print(f"[Watcher] fetch failed: {response.status_code} {response.text[:200]}")
            return {}

        return response.json() or {}

    except (requests.RequestException, FirebaseAuthError, FirebaseTransportError) as exc:
        print(f"[Watcher] fetch request failed: {exc}")
        return {}


def find_new_open_long_inactivity_events(events, processed_source_event_ids, startup_ms):
    found = []
    if not isinstance(events, dict):
        return found
    previously_checked = {
        event.get("sourceEventId") for event in events.values()
        if isinstance(event, dict)
        and event.get("type") == "inactivity_voice_check_sent"
        and isinstance(event.get("sourceEventId"), str)
    }

    for event_id, event in events.items():
        if not isinstance(event, dict):
            continue

        event_type = event.get("type")
        status = event.get("status", "open")
        created_at = event.get("createdAt", 0)

        if event_type != "long_inactivity":
            continue

        if status != "open":
            continue

        if not isinstance(created_at, int):
            continue

        if created_at < startup_ms - 10_000:
            continue

        meta = event.get("meta")
        if (event_id in processed_source_event_ids or event_id in previously_checked
                or (isinstance(meta, dict) and "voiceCheck" in meta)):
            continue

        found.append((event_id, event))

    found.sort(key=lambda item: item[1].get("createdAt", 0))
    return found


def run_watch_mode():
    processed_source_event_ids = set()
    startup_ms = now_ms()

    print("[VoiceHub] mode: watch")
    print(f"[Watcher] polling events/{ELDER_ID}")
    print("[Watcher] target event type: long_inactivity")
    print("[Watcher] only new events after startup will be processed.")

    while True:
        events = fetch_events()
        new_events = find_new_open_long_inactivity_events(
            events,
            processed_source_event_ids,
            startup_ms,
        )

        for event_id, _event in new_events:
            print(f"[Watcher] open long_inactivity found: {event_id}")
            processed_source_event_ids.add(event_id)
            trigger_inactivity_voice_check(event_id)

        time.sleep(5)


def check_medication_schedule_once(medication_times, triggered_today, current_day,
                                   medication_care=None):
    if medication_times is None:
        return current_day
    if isinstance(medication_times, MedicationScheduleProvider):
        dispatch_medication_schedule_once(
            medication_times, triggered_today, medication_care,
        )
        return current_day
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    current_hm = now.strftime("%H:%M")

    if today != current_day:
        current_day = today
        triggered_today.clear()
        print("[Hub] new day detected. reset medication schedule.")

    if current_hm in medication_times:
        key = f"{today}_{current_hm}"

        if not triggered_today.get(key):
            print(f"[Hub] triggering medication reminder: {current_hm}")
            triggered_today[key] = True
            if medication_care is None:
                trigger_medication_reminder()
            else:
                medication_care.run_scheduled(key)

    return current_day


def check_inactivity_events_once(processed_source_event_ids, startup_ms):
    events = fetch_events()
    new_events = find_new_open_long_inactivity_events(
        events,
        processed_source_event_ids,
        startup_ms,
    )

    for event_id, _event in new_events:
        print(f"[Hub] open long_inactivity found: {event_id}")
        processed_source_event_ids.add(event_id)
        trigger_inactivity_voice_check(event_id)


def run_hub_mode():
    processed_source_event_ids = set()
    startup_ms = now_ms()

    print("[VoiceHub] mode: hub")
    print(f"[Hub] watching events/{ELDER_ID} for long_inactivity")
    print("[Hub] running medication scheduler + inactivity watcher")

    medication_schedule = start_medication_schedule_provider()
    medication_cursor = MedicationScheduleCursor()
    medication_day = None
    conversation = start_ai_conversation()
    care = start_inactivity_care(conversation)
    medication_care = start_medication_care(conversation)
    if medication_care is None:
        medication_care = start_medication_announcement_care(conversation)
    display = start_display(conversation)
    ai_requested = os.getenv("AI_CONVERSATION_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }
    if ai_requested and care is None:
        print("[InactivityCare] unavailable; inactivity events remain open without audio. "
              "Medication scheduling continues.")
    try:
        while True:
            try:
                # Enqueue only: ongoing conversations cannot block schedule polling.
                medication_day = check_medication_schedule_once(
                    medication_schedule, medication_cursor,
                    medication_day, medication_care,
                )

                if care is None and not ai_requested:
                    check_inactivity_events_once(
                        processed_source_event_ids,
                        startup_ms,
                    )

            except Exception as exc:
                print(f"[Hub] loop error: {exc}")

            time.sleep(5)
    finally:
        if display is not None:
            display.stop()
        if medication_care is not None:
            medication_care.stop()
        if medication_schedule is not None:
            medication_schedule.stop()
        if care is not None:
            care.stop()
        if conversation is not None:
            conversation.stop()


def start_display(conversation=None):
    """Optional in-process local screen; a missing screen/server cannot stop care."""
    if os.getenv("DISPLAY_ENABLED", "false").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        return None
    server = None
    try:
        from display_server import DisplayServer

        port = int(os.getenv("DISPLAY_PORT", "8765"))
        if not 1 <= port <= 65535:
            raise ValueError("DISPLAY_PORT must be from 1 to 65535")
        server = DisplayServer(
            state=DISPLAY, controller=conversation,
            host=os.getenv("DISPLAY_HOST", "127.0.0.1"),
            port=port,
        ).start()
        if conversation is not None:
            conversation.enable_local_touch()
        print(f"[Display] http://127.0.0.1:{server.port} (local touchscreen)")
        return server
    except Exception as exc:
        if server is not None:
            server.stop()
        print(f"[Display] unavailable ({type(exc).__name__}); voice care continues.")
        return None


def start_medication_care(conversation):
    """Opt into one voice confirmation on the existing hub schedule."""
    if os.getenv("MEDICATION_VOICE_CONFIRMATION_ENABLED", "false").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        return None
    if conversation is None:
        print("[MedicationCare] AI unavailable; continuing announcement-only reminders.")
        return None
    try:
        from voice_ai import create_medication_care

        care = create_medication_care(
            FIREBASE_DATABASE_URL, ELDER_ID, DEVICE_ID, conversation.conversation,
            build_medication_event,
            **_firebase_http_kwargs(),
        ).start()
        print("[MedicationCare] enabled; one voice attempt per scheduled reminder.")
        return care
    except Exception as exc:
        print(f"[MedicationCare] startup failed ({type(exc).__name__}); "
              "continuing announcement-only reminders.")
        return None


def start_medication_announcement_care(conversation=None):
    """Use the same durable Firebase claim for announcement-only operation."""
    try:
        from firebase_medication import FirebaseMedicationClient
        from medication_care import MedicationAnnouncementCare

        client = FirebaseMedicationClient(
            FIREBASE_DATABASE_URL, ELDER_ID, DEVICE_ID,
            **_firebase_http_kwargs(),
        )
        care = MedicationAnnouncementCare(
            client, announce_medication_reminder,
            interaction_lock=(conversation.conversation.interaction_lock
                              if conversation is not None else None),
        ).start()
        print("[MedicationCare] announcement-only durable scheduling enabled.")
        return care
    except Exception as exc:
        print(f"[MedicationCare] announcement startup failed ({type(exc).__name__}); "
              "no unclaimed medication audio will be played.")
        return None


def start_inactivity_care(conversation):
    if conversation is None:
        return None
    try:
        from voice_ai import create_inactivity_controller

        return create_inactivity_controller(
            FIREBASE_DATABASE_URL, ELDER_ID, DEVICE_ID, conversation.conversation,
            **_firebase_http_kwargs(),
        ).start()
    except Exception as exc:
        print(f"[InactivityCare] startup failed ({type(exc).__name__}); "
              "inactivity events remain open without audio.")
        return None


def start_ai_conversation(force=False, trigger_mode=None):
    enabled = os.getenv("AI_CONVERSATION_ENABLED", "false").strip().lower()
    if not force and enabled not in {"1", "true", "yes", "on"}:
        return None
    try:
        selected = (trigger_mode or os.getenv(
            "AI_CONVERSATION_TRIGGER", "auto"
        )).strip().lower()
        if selected not in {"auto", "terminal", "firebase"}:
            raise ValueError(
                "AI_CONVERSATION_TRIGGER must be auto, terminal, or firebase"
            )
        if selected == "auto":
            try:
                selected = "terminal" if sys.stdin and sys.stdin.isatty() else "firebase"
            except (AttributeError, OSError):
                selected = "firebase"

        if selected == "firebase":
            from voice_ai import create_firebase_controller

            factory = create_firebase_controller
        else:
            from voice_ai import create_controller

            factory = create_controller

        print(f"[VoiceAI] trigger mode: {selected}")
        authenticated = _firebase_http_kwargs()
        if FIREBASE_RUNTIME_MODE == "isolated_authenticated":
            if _ISOLATED_OPENAI_KEY is None:
                raise RuntimeError("Isolated OpenAI key must be configured before startup")
            authenticated["api_key"] = _ISOLATED_OPENAI_KEY
        controller = factory(
            FIREBASE_DATABASE_URL, ELDER_ID, DEVICE_ID, LOCATION_NAME,
            AUDIO_LOCK, AUDIO_DIR,
            **authenticated,
        )
        return controller.start()
    except Exception as exc:
        print(
            f"[VoiceAI] 시작 실패 ({type(exc).__name__}). "
            "트리거, Firebase, 터미널, API 키와 AI 설정을 확인하세요."
        )
        return None


def run_chat_mode():
    conversation = start_ai_conversation(force=True, trigger_mode="terminal")
    if conversation is None:
        return
    try:
        while not conversation.finished.wait(0.2):
            pass
    finally:
        conversation.stop()


def main():
    _firebase_http_kwargs()
    print("[VoiceHub] started")
    print(f"[VoiceHub] elderId: {ELDER_ID}")
    print(f"[VoiceHub] deviceId: {DEVICE_ID}")
    print(f"[VoiceHub] REMINDER_MODE: {REMINDER_MODE}")

    if REMINDER_MODE == "schedule":
        run_schedule_mode()
    elif REMINDER_MODE == "watch":
        run_watch_mode()
    elif REMINDER_MODE == "hub":
        run_hub_mode()
    elif REMINDER_MODE == "chat":
        run_chat_mode()
    else:
        run_demo_mode()


if __name__ == "__main__":
    main()
