"""Conservative Korean check-in replies and an independent care worker."""

import re
import threading
import unicodedata


CHECK_IN_PROMPT = "어르신, 한동안 움직임이 없으신데 괜찮으세요?"


def classify_inactivity_reply(text):
    """Only explicit complete phrases can close an alert; never use an LLM.

    Mixed, quoted, hypothetical and unrecognized replies stay open. Help phrases
    are matched as complete clauses, so '도와주지 마세요' isn't a help request.
    """
    normalized = unicodedata.normalize("NFKC", text).strip()
    if not normalized:
        return "no_speech"
    if any(mark in normalized for mark in "\"'‘’“”「」『』"):
        return "unclear"
    clauses = [re.sub(r"\s+", "", part) for part in
               re.split(r"[,.!?…~。！？]+", normalized) if part.strip()]
    help_phrase = re.compile(
        r"(?:도란아|도란이야)?(?:"
        r"(?:좀|저좀|제발)?(?:도와줘|도와줘요|도와주세요|살려줘|살려줘요|살려주세요)|"
        r"도움(?:이)?필요(?:해|해요|합니다)|"
        r"(?:숨이|숨을)?(?:안쉬어져|안쉬어져요|못쉬겠어|못쉬겠어요)|"
        r"숨쉬기(?:가)?힘들(?:어|어요)|"
        r"(?:가슴이|가슴이너무|너무)아(?:파|파요)|"
        r"(?:넘어졌|쓰러졌)(?:어|어요|습니다)|"
        r"못일어나(?:요|겠어|겠어요)?|"
        r"(?:119|일일구|구급차)(?:를|에)?(?:불러줘|불러줘요|불러주세요|전화해줘|전화해주세요)"
        r")"
    )
    # Recognize explicit help after a simple acknowledgment in the same clause.
    if any(help_phrase.fullmatch(re.sub(r"^(?:네|응)", "", part))
           for part in clauses):
        return "help"
    okay_phrase = re.compile(
        r"(?:네|응)?(?:저는|난|나는)?(?:괜찮아|괜찮아요|괜찮습니다|"
        r"문제없어|문제없어요|문제없습니다|별일없어|별일없어요|별일없습니다)"
    )
    # Terminal punctuation is transcription formatting; the complete phrase still
    # has to match. '네' alone, uncertainty, and explanations remain insufficient.
    meaningful = [part for part in clauses if part not in {"네", "응"}]
    if meaningful and all(okay_phrase.fullmatch(part) for part in meaningful):
        return "okay"
    return "unclear"


class InactivityCareController:
    """Poll separately from medication; serialize audio with conversation sessions."""

    def __init__(self, client, conversation):
        self.client = client
        self.conversation = conversation
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="inactivity-care",
        )

    def start(self):
        self._thread.start()
        return self

    def _run(self):
        print(f"[InactivityCare] watching {self.client.path}; durable checks enabled.", flush=True)
        while not self.conversation.stop_event.is_set():
            try:
                self.client.run_once(self._check)
            except Exception as exc:
                print(f"[InactivityCare] poll failed ({type(exc).__name__}); retrying.", flush=True)
            self.conversation.stop_event.wait(self.client.poll_seconds)

    def _check(self, event_id):
        def before_start():
            # A guardian can resolve the event while an ongoing chat finishes.
            # Read again after that wait, before any TTS or microphone access.
            if not self.client.validate_before_audio(event_id):
                print(f"[InactivityCare] event={event_id} resolved while queued; check cancelled.", flush=True)
                return False
            return True

        return self.conversation.run_inactivity_check(
            event_id, before_start=before_start,
        )

    def stop(self):
        self.conversation.stop_event.set()
        self._thread.join(timeout=3)
