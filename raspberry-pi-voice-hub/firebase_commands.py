"""At-most-once Firebase command intake for AI voice turns.

Commands are claimed with Firebase REST ETags before any microphone or speaker
side effect.  Only ``pending`` commands are executable; ``processing`` and
terminal commands are durable deduplication records across process restarts.
"""

from dataclasses import dataclass
import math
from urllib.parse import quote
from uuid import uuid4


SERVER_TIMESTAMP = {".sv": "timestamp"}
START_COMMAND = "start_ai_conversation"
TERMINAL_STATUSES = {"processed", "failed"}
_READ_FAILED = object()


@dataclass(frozen=True)
class PendingAcknowledgement:
    command_id: str
    claim_token: str
    status: str
    outcome: str
    error_type: str = ""


class FirebaseCommandClient:
    """Poll and consume one device-specific command at a time."""

    ROOT_PATH = "commands"
    REQUEST_TIMEOUT = 10

    def __init__(
        self,
        base_url,
        elder_id,
        device_id,
        poll_seconds=5,
        http=None,
    ):
        self.base_url = base_url.rstrip("/")
        self.elder_id = elder_id
        self.device_id = device_id
        self.poll_seconds = float(poll_seconds)

        if not self.base_url:
            raise ValueError("FIREBASE_DATABASE_URL is required for Firebase commands")
        if not self._valid_key(self.elder_id) or not self._valid_key(self.device_id):
            raise ValueError("ELDER_ID and DEVICE_ID must be valid Firebase keys")
        if not math.isfinite(self.poll_seconds) or not 1 <= self.poll_seconds <= 60:
            raise ValueError("AI_COMMAND_POLL_SECONDS must be between 1 and 60")

        if http is None:
            import requests

            http = requests
        self.http = http
        self.path = f"{self.ROOT_PATH}/{self.elder_id}/{self.device_id}"
        self._claim_tokens = {}
        # When a conditional PUT response and its immediate verification read
        # both fail, retain this process-local intent. A later pass can safely
        # resume only a processing record bearing this exact random token.
        self._pending_claim = None
        self._started_commands = set()
        # A completion write may fail after the physical audio side effect. Keep
        # retrying that acknowledgement before accepting another command, but
        # never call the completed turn again.
        self._pending_ack = None

    @staticmethod
    def _valid_key(value):
        return (
            isinstance(value, str)
            and bool(value)
            and not any(char in value for char in ".#$[]/")
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)
        )

    @staticmethod
    def _valid_timestamp(value):
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    def _collection_url(self):
        # Firebase keys may contain URL delimiters such as '?' and '%'.
        path = "/".join(quote(part, safe="") for part in (
            self.ROOT_PATH, self.elder_id, self.device_id
        ))
        return f"{self.base_url}/{path}.json"

    def _command_url(self, command_id):
        return f"{self._collection_url()[:-5]}/{quote(command_id, safe='')}.json"

    def _eligible(self, command_id, command):
        if not self._valid_key(command_id) or not isinstance(command, dict):
            return False
        if command.get("type") != START_COMMAND or command.get("status") != "pending":
            return False
        if not self._valid_timestamp(command.get("createdAt")):
            return False
        expected_identity = {
            "commandId": command_id,
            "elderId": self.elder_id,
            "deviceId": self.device_id,
        }
        return all(
            key not in command or command.get(key) == expected
            for key, expected in expected_identity.items()
        )

    def _read_command(self, command_id, *, with_etag):
        headers = {"X-Firebase-ETag": "true"} if with_etag else None
        try:
            response = self.http.get(
                self._command_url(command_id),
                headers=headers,
                timeout=self.REQUEST_TIMEOUT,
            )
            if response.status_code != 200:
                print("[AICommand] Command read pending; will retry.")
                return _READ_FAILED, None
            command = response.json()
            etag = response.headers.get("ETag") if with_etag else None
            if with_etag and not etag:
                print("[AICommand] Firebase did not return a command ETag.")
                return _READ_FAILED, None
            return command, etag
        except Exception:
            print("[AICommand] Command read pending; will retry.")
            return _READ_FAILED, None

    def _find_next_pending(self):
        try:
            response = self.http.get(
                self._collection_url(), timeout=self.REQUEST_TIMEOUT
            )
            if response.status_code != 200:
                print("[AICommand] Command poll pending; will retry.")
                return None
            commands = response.json()
        except Exception:
            print("[AICommand] Command poll pending; will retry.")
            return None

        if commands is None:
            return None
        if not isinstance(commands, dict):
            print("[AICommand] Ignored malformed command collection.")
            return None

        eligible = [
            (command_id, command)
            for command_id, command in commands.items()
            if self._eligible(command_id, command)
        ]
        if not eligible:
            return None
        eligible.sort(key=lambda item: (item[1]["createdAt"], item[0]))
        return eligible[0][0]

    def _verify_claim(self, command_id, claim_token):
        command, _etag = self._read_command(command_id, with_etag=False)
        return (
            isinstance(command, dict)
            and command.get("status") == "processing"
            and command.get("claimToken") == claim_token
            and command.get("acknowledgedBy") == self.device_id
        )

    def _claim(self, command_id):
        claim_token = self._claim_tokens.setdefault(command_id, uuid4().hex)
        self._pending_claim = (command_id, claim_token)
        command, etag = self._read_command(command_id, with_etag=True)
        if command is _READ_FAILED:
            return None
        if (
            isinstance(command, dict)
            and command.get("status") == "processing"
            and command.get("claimToken") == claim_token
            and command.get("acknowledgedBy") == self.device_id
        ):
            self._pending_claim = None
            return claim_token
        if not self._eligible(command_id, command):
            self._pending_claim = None
            self._claim_tokens.pop(command_id, None)
            return None

        claimed = dict(command)
        claimed.update({
            "commandId": command_id,
            "elderId": self.elder_id,
            "deviceId": self.device_id,
            "status": "processing",
            "acknowledgedAt": SERVER_TIMESTAMP,
            "acknowledgedBy": self.device_id,
            "claimToken": claim_token,
        })
        try:
            response = self.http.put(
                self._command_url(command_id),
                json=claimed,
                headers={"If-Match": etag},
                timeout=self.REQUEST_TIMEOUT,
            )
            if 200 <= response.status_code < 300:
                self._pending_claim = None
                return claim_token
            if response.status_code == 412:
                self._pending_claim = None
                self._claim_tokens.pop(command_id, None)
                return None
        except Exception:
            pass

        # A write response can be lost after Firebase commits it. Read back the
        # unique claim token before deciding whether the side effect is safe.
        if self._verify_claim(command_id, claim_token):
            self._pending_claim = None
            return claim_token
        print("[AICommand] Command claim pending; will retry.")
        return None

    @staticmethod
    def _terminal_matches(command, acknowledgement):
        matches = (
            isinstance(command, dict)
            and command.get("status") == acknowledgement.status
            and command.get("claimToken") == acknowledgement.claim_token
            and command.get("outcome") == acknowledgement.outcome
            and "processedAt" in command
        )
        if not matches:
            return False
        if acknowledgement.error_type:
            return command.get("errorType") == acknowledgement.error_type
        return not command.get("errorType")

    def _acknowledge(self, acknowledgement):
        command, etag = self._read_command(
            acknowledgement.command_id, with_etag=True
        )
        if command is _READ_FAILED:
            return False
        if self._terminal_matches(command, acknowledgement):
            return True
        if (
            isinstance(command, dict)
            and command.get("status") in TERMINAL_STATUSES
            and command.get("claimToken") == acknowledgement.claim_token
        ):
            print("[AICommand] Command has a conflicting terminal result.")
            return True
        if not (
            isinstance(command, dict)
            and command.get("status") == "processing"
            and command.get("claimToken") == acknowledgement.claim_token
            and command.get("acknowledgedBy") == self.device_id
        ):
            # Never overwrite another worker or a producer correction. The old
            # claim is already non-pending, so dropping this local retry cannot
            # cause the audio turn to be replayed.
            print("[AICommand] Command acknowledgement conflict; not overwritten.")
            return True

        completed = dict(command)
        completed.update({
            "status": acknowledgement.status,
            "outcome": acknowledgement.outcome,
            "processedAt": SERVER_TIMESTAMP,
        })
        if acknowledgement.error_type:
            completed["errorType"] = acknowledgement.error_type
        else:
            completed.pop("errorType", None)

        try:
            response = self.http.put(
                self._command_url(acknowledgement.command_id),
                json=completed,
                headers={"If-Match": etag},
                timeout=self.REQUEST_TIMEOUT,
            )
            if 200 <= response.status_code < 300:
                return True
        except Exception:
            pass

        # As with claiming, a lost response is success only when read-back
        # proves that this exact claim reached a terminal state.
        delivered, _etag = self._read_command(
            acknowledgement.command_id, with_etag=False
        )
        if self._terminal_matches(delivered, acknowledgement):
            return True
        print("[AICommand] Command acknowledgement pending; will retry.")
        return False

    def run_once(self, run_turn):
        """Consume at most one command, or retry one completed acknowledgement."""
        if self._pending_ack is not None:
            acknowledgement = self._pending_ack
            if self._acknowledge(acknowledgement):
                self._claim_tokens.pop(acknowledgement.command_id, None)
                self._started_commands.discard(acknowledgement.command_id)
                self._pending_ack = None
                return True
            return False

        if self._pending_claim is not None:
            command_id, _claim_token = self._pending_claim
        else:
            command_id = self._find_next_pending()
            if command_id is None:
                return False
        claim_token = self._claim(command_id)
        if claim_token is None:
            return False
        if command_id in self._started_commands:
            # A BaseException may have interrupted the caller after the physical
            # turn began. Never make a second attempt in the same process.
            return False
        self._started_commands.add(command_id)

        try:
            responded = bool(run_turn())
            acknowledgement = PendingAcknowledgement(
                command_id=command_id,
                claim_token=claim_token,
                status="processed",
                outcome="completed" if responded else "no_speech",
            )
        except Exception as exc:
            # SDK errors can contain private transcripts, URLs, or credentials.
            # Persist only the safe exception class and never retry the audio.
            error_type = type(exc).__name__
            print(f"[AICommand] Voice turn failed ({error_type}); it will not be replayed.")
            acknowledgement = PendingAcknowledgement(
                command_id=command_id,
                claim_token=claim_token,
                status="failed",
                outcome="failed",
                error_type=error_type,
            )

        self._pending_ack = acknowledgement
        if self._acknowledge(acknowledgement):
            self._claim_tokens.pop(command_id, None)
            self._started_commands.discard(command_id)
            self._pending_ack = None
        return True
