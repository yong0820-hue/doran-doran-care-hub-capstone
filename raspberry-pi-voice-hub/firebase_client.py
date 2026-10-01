import time
import requests

from config import FIREBASE_DATABASE_URL, ELDER_ID, DEVICE_ID, LOCATION_NAME


class FirebaseClient:
    def __init__(self):
        self.base_url = FIREBASE_DATABASE_URL

    def write_medication_reminder(self, title, message):
        now_ms   = int(time.time() * 1000)
        event_id = f"pi_medication_reminder_sent_{now_ms}"
        run_id   = f"pi_medication_run_{now_ms}"

        event = {
            "deviceId":  DEVICE_ID,
            "elderId":   ELDER_ID,
            "type":      "medication_reminder_sent",
            "severity":  "info",
            "title":     title,
            "message":   message,
            "status":    "open",
            "createdAt": now_ms,
            "location":  LOCATION_NAME,
            "runId":     run_id,
        }

        patch_payload = {
            f"events/{ELDER_ID}/{event_id}": event,
            f"devices/{DEVICE_ID}/lastSeen":   now_ms,
            f"devices/{DEVICE_ID}/lastSignal": now_ms,
        }

        url = f"{self.base_url}/.json"
        print(f"[Firebase Debug] eventPath: events/{ELDER_ID}/{event_id}")
        print(f"[Firebase Debug] runId: {run_id}")

        try:
            response = requests.patch(url, json=patch_payload, timeout=10)
        except requests.RequestException as e:
            print(f"[Firebase] Request failed: {e}")
            return False

        if not (200 <= response.status_code < 300):
            print(f"[Firebase] write failed: {response.status_code} {response.text[:200]}")
            return False

        # Verification read-back
        verify_url = f"{self.base_url}/events/{ELDER_ID}/{event_id}.json"
        try:
            verify_resp = requests.get(verify_url, timeout=10)
            print(f"[Firebase Debug] verify status: {verify_resp.status_code}")
            if verify_resp.status_code == 200:
                data = verify_resp.json() or {}
                print(f"[Firebase Debug] verify title: {data.get('title', '?')}")
                print(f"[Firebase Debug] verify createdAt: {data.get('createdAt', '?')}")
        except Exception as e:
            print(f"[Firebase Debug] verify error: {e}")

        return True

    def update_device_signal(self):
        now_ms = int(time.time() * 1000)
        patch_payload = {
            f"devices/{DEVICE_ID}/lastSeen":   now_ms,
            f"devices/{DEVICE_ID}/lastSignal": now_ms,
        }
        url = f"{self.base_url}/.json"
        try:
            response = requests.patch(url, json=patch_payload, timeout=10)
        except requests.RequestException as e:
            print(f"[Firebase] Request failed: {e}")
            return
        if response.status_code != 200:
            print(f"[Firebase] Error {response.status_code}: {response.text}")
