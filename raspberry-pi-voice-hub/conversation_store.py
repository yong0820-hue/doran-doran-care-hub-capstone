"""Durable, idempotent Firebase logging for the Pi voice conversation.

Each message and its event are written together with one multi-location PATCH.
The local outbox contains conversation data only; URLs and credentials are never
saved. Keep ``outbox_dir`` outside version control and protect it like transcripts.
"""

import json
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid


class ConversationStore:
    REQUEST_TIMEOUT = 10
    MAX_FLUSH_MESSAGES = 5
    EVENT_IDENTITY_FIELDS = (
        "runId", "messageId", "sessionId", "elderId", "deviceId", "role",
        "createdAt", "type",
    )

    def __init__(
        self, base_url, elder_id, device_id, location, outbox_dir, http=None
    ):
        self.base_url = base_url.rstrip("/")
        self.elder_id = elder_id
        self.device_id = device_id
        self.location = location
        self.outbox_dir = Path(outbox_dir)
        if http is None:
            import requests
            http = requests
        self.http = http
        self._enqueue_lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._sequence = 0

    @staticmethod
    def _valid_key(value):
        return (
            isinstance(value, str)
            and bool(value)
            and not any(char in value for char in ".#$[]/")
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)
        )

    @staticmethod
    def _valid_number(value):
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    def write_message(
        self,
        session_id,
        session_started_at,
        message_id,
        role,
        text,
        created_at,
        turn_index,
        audio_status,
        *,
        flush=True,
    ):
        """Save a message before attempting any network request.

        With ``flush=False``, True means the message was queued locally. With the
        default ``flush=True``, True means the outbox was completely delivered.
        False never discards an already queued message. All storage/network
        failures return False so conversation audio can continue independently.
        """
        try:
            if not all(
                self._valid_key(key)
                for key in (self.elder_id, self.device_id, session_id, message_id)
            ):
                return False
            if role not in ("user", "assistant") or not isinstance(text, str):
                return False
            if not all(
                self._valid_number(value)
                for value in (session_started_at, created_at, turn_index)
            ) or not isinstance(audio_status, str):
                return False

            session_path = f"conversations/{self.elder_id}/{session_id}"
            event_id = f"pi_ai_conversation_message_{message_id}"
            payload = {
                f"{session_path}/elderId": self.elder_id,
                f"{session_path}/deviceId": self.device_id,
                f"{session_path}/createdAt": session_started_at,
                f"{session_path}/updatedAt": created_at,
                f"{session_path}/messages/{message_id}": {
                    "messageId": message_id,
                    "role": role,
                    "text": text,
                    "createdAt": created_at,
                    "turnIndex": turn_index,
                    "audioStatus": audio_status,
                },
                f"events/{self.elder_id}/{event_id}": {
                    "elderId": self.elder_id,
                    "deviceId": self.device_id,
                    "type": "ai_conversation_message",
                    "title": "AI 손주 대화",
                    "message": text,
                    "status": "open",
                    "severity": "info",
                    "createdAt": created_at,
                    "location": self.location,
                    "runId": f"pi_ai_conversation_run_{session_id}",
                    "sessionId": session_id,
                    "messageId": message_id,
                    "role": role,
                },
                # Recovery may deliver old messages after other hub activity.
                # Device health reflects delivery time, while the transcript
                # and event keep the original time the conversation happened.
                f"devices/{self.device_id}/lastSeen": {".sv": "timestamp"},
                f"devices/{self.device_id}/lastSignal": {".sv": "timestamp"},
            }
            self._enqueue(payload)
        except Exception:
            # Exception strings can contain transcripts, URLs, or credentials.
            print("[ConversationStore] Could not save pending message locally.")
            return False

        return self.flush_pending() if flush else True

    def _enqueue(self, payload):
        # Serializing enqueues keeps their filenames in insertion order even if
        # the system clock moves backwards. The flush lock is separate so a
        # slow network request cannot block a voice turn saving its transcript.
        with self._enqueue_lock:
            self.outbox_dir.mkdir(parents=True, exist_ok=True)
            existing = [
                int(path.name.split("-", 1)[0])
                for path in self.outbox_dir.glob("*.json")
                if path.name.split("-", 1)[0].isdigit()
            ]
            self._sequence = max(
                time.time_ns(), self._sequence + 1, max(existing, default=0) + 1
            )
            target = self.outbox_dir / (
                f"{self._sequence:020d}-{uuid.uuid4().hex}.json"
            )
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.outbox_dir,
                    prefix="pending-", suffix=".tmp", delete=False,
                ) as handle:
                    temporary = Path(handle.name)
                    json.dump(payload, handle, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            finally:
                if temporary is not None and temporary.exists():
                    temporary.unlink()

    def flush_pending(self):
        """Try up to five pending messages in FIFO order, once each.

        Stop on the first failure and retain that message and later messages.
        Return True only when the outbox is empty. Concurrent flush attempts
        return False immediately; enqueuing remains available during HTTP I/O.
        Read the event before writing: an existing matching event proves the
        prior atomic PATCH succeeded, even if its response was lost. Skipping
        that PATCH preserves any subsequent guardian acknowledgement.
        """
        if not self._flush_lock.acquire(blocking=False):
            return False
        try:
            pending = sorted(self.outbox_dir.glob("*.json"))
            if not pending:
                return True
            if not self.base_url:
                return False

            for path in pending[:self.MAX_FLUSH_MESSAGES]:
                with path.open(encoding="utf-8") as handle:
                    payload = json.load(handle)
                if not isinstance(payload, dict) or not payload:
                    return False
                events = [
                    (key, value) for key, value in payload.items()
                    if key.startswith("events/")
                ]
                if len(events) != 1:
                    return False
                event_path, expected_event = events[0]
                if not isinstance(expected_event, dict) or not all(
                    key in expected_event for key in self.EVENT_IDENTITY_FIELDS
                ):
                    return False

                verification = self.http.get(
                    f"{self.base_url}/{event_path}.json",
                    timeout=self.REQUEST_TIMEOUT,
                )
                if verification.status_code != 200:
                    print("[ConversationStore] Delivery check pending; will retry.")
                    return False
                existing_event = verification.json()
                if existing_event is not None:
                    if not isinstance(existing_event, dict) or not all(
                        existing_event.get(key) == expected_event[key]
                        for key in self.EVENT_IDENTITY_FIELDS
                    ):
                        print("[ConversationStore] Event conflict; message retained.")
                        return False
                    path.unlink()
                    continue

                response = self.http.patch(
                    f"{self.base_url}/.json",
                    json=payload,
                    timeout=self.REQUEST_TIMEOUT,
                )
                if not 200 <= response.status_code < 300:
                    print("[ConversationStore] Firebase write pending; will retry.")
                    return False
                # If deletion fails, the same stable paths are safe to retry.
                path.unlink()
            return not any(self.outbox_dir.glob("*.json"))
        except Exception:
            print("[ConversationStore] Delivery pending; will retry.")
            return False
        finally:
            self._flush_lock.release()
