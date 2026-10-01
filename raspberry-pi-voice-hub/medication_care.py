"""One spoken confirmation for an existing scheduled medication event."""

import queue
import re
import threading
import unicodedata


MEDICATION_PROMPT = "어르신, 약 드실 시간이에요. 드셨으면 먹었어요라고 말씀해 주세요."
MEDICATION_DISPLAY_MESSAGE = "안내가 끝나면 드셨는지 말씀해 주세요."
_OUTER_QUOTE_PAIRS = {'"': '"', "'": "'", '‘': '’', '“': '”', '「': '」', '『': '』'}


def _normalize_medication_reply(text):
    """Remove transcription-only formatting without changing reply words."""
    normalized = unicodedata.normalize("NFKC", text).strip()
    while (len(normalized) >= 2
           and _OUTER_QUOTE_PAIRS.get(normalized[0]) == normalized[-1]
           and normalized[1:-1].strip()):
        normalized = normalized[1:-1].strip()
    normalized = re.sub(r"\s+", "", normalized)
    without_terminal_punctuation = normalized.rstrip(".?!…~。！？")
    return without_terminal_punctuation or normalized


def classify_medication_reply(text):
    """Confirm only explicit whole replies about completed medication.

    The prompt provides medication context for a bare '먹었어요'. STT may add
    terminal punctuation or balanced outer quotes, so those do not affect an
    otherwise exact reply. Question forms, internal quotations, reported speech,
    meals, and mixed clauses never close a reminder. Recognition is intentionally
    narrower than everyday Korean.
    """
    normalized = _normalize_medication_reply(text)
    if not normalized:
        return "no_speech"
    if any(mark in normalized for mark in "?\"'‘’“”「」『』"):
        return "unclear"
    prefix = r"(?:(?:네|예|응)[,.，。]?)?(?:저는|나는|난)?(?:이미|벌써)?(?:약(?:을|은)?)?"
    confirmed = (
        r"(?:먹었(?:어|어요|습니다|지|지요|죠)|"
        r"복용했(?:어|어요|습니다|지|지요|죠))"
    )
    if re.fullmatch(prefix + confirmed, normalized):
        return "confirmed"
    # Negation/future replies can never be taken as a successful confirmation.
    # Keep matching whole utterances so quoted/reported/mixed phrases stay unclear.
    defer = (
        r"(?:(?:아직)?(?:안|못)(?:먹었(?:어|어요|습니다)|복용했(?:어|어요|습니다))|"
        r"(?:아직)?(?:먹지|복용하지)않았(?:어|어요|습니다)|"
        r"(?:아직)?(?:먹지|복용하지)못했(?:어|어요|습니다)|"
        r"(?:조금있다|조금이따|조금있다가|조금이따가|좀있다|좀이따|"
        r"이따|이따가|나중에|조금후에)?(?:약(?:을|은)?)?"
        r"(?:먹을(?:게|게요|거야|거예요)|먹겠(?:어|어요|습니다)|"
        r"복용할(?:게|게요|거야|거예요)|복용하겠(?:어|어요|습니다))|"
        r"아직(?:이야|이에요|입니다)?)"
    )
    if re.fullmatch(prefix + defer, normalized):
        return "defer"
    return "unclear"


class MedicationCare:
    """Queue existing scheduled slots on one audio/persistence worker.

    The scheduler remains free to notice adjacent medication times while a
    conversation holds the interaction lock. Queued slots are volatile until
    the worker durably reserves the event; a crash can lose unstarted work.
    """

    RETRY_SECONDS = 5

    def __init__(self, client, conversation, event_factory):
        self.client = client
        self.conversation = conversation
        self.event_factory = event_factory
        # The factory provides an isolated conversation and thus its own stop
        # event, while sharing physical audio and interaction locks with chat.
        self.stop_event = conversation.stop_event
        self._queue = queue.Queue()
        self._queue_lock = threading.Lock()
        self._scheduled_slots = set()
        self._started = False
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="medication-care",
        )

    def start(self):
        with self._queue_lock:
            if not self._started and not self.stop_event.is_set():
                self._started = True
                self._thread.start()
        return self

    @staticmethod
    def _occurrence_key(slot_key, event):
        meta = event.get("meta", {}) if isinstance(event, dict) else {}
        schedule_id = meta.get("scheduleId") if isinstance(meta, dict) else None
        return f"{slot_key[:10]}:{schedule_id}" if schedule_id else slot_key

    def run_scheduled(self, slot_key, event=None, validity_check=None):
        """Enqueue once without network/audio work on the scheduler thread."""
        with self._queue_lock:
            if self.stop_event.is_set():
                return False
            if event is None:
                if slot_key in self._scheduled_slots:
                    return False
                event = self.event_factory()
            occurrence_key = self._occurrence_key(slot_key, event)
            if occurrence_key in self._scheduled_slots:
                return False
            self._scheduled_slots.add(occurrence_key)
            self._queue.put((slot_key, event, validity_check))
        print(f"[MedicationCare] slot={slot_key} queued for one voice attempt.", flush=True)
        return True

    def _run(self):
        while not self.stop_event.is_set():
            try:
                item = self._queue.get(timeout=self.RETRY_SECONDS)
            except queue.Empty:
                if not self.stop_event.is_set():
                    try:
                        self.client.retry_pending()
                    except Exception as exc:
                        print(f"[MedicationCare] result retry failed ({type(exc).__name__}); "
                              "audio will not repeat.", flush=True)
                continue
            try:
                if item is None or self.stop_event.is_set():
                    return
                slot_key, event, validity_check = item
                if validity_check is not None and not validity_check():
                    print(f"[MedicationCare] slot={slot_key} is no longer scheduled; skipped.",
                          flush=True)
                    continue
                try:
                    callback = self._check if validity_check is None else (
                        lambda event_id: self._check(event_id, validity_check)
                    )
                    self.client.run_scheduled(
                        slot_key, event, callback,
                    )
                except Exception as exc:
                    print(f"[MedicationCare] slot={slot_key} worker failed "
                          f"({type(exc).__name__}); attempt will not repeat.", flush=True)
            finally:
                self._queue.task_done()

    def _check(self, event_id, validity_check=None):
        def before_start():
            if validity_check is not None and not validity_check():
                print(f"[MedicationCare] event={event_id} schedule changed while queued; "
                      "check cancelled.", flush=True)
                return False
            # A guardian may resolve the event while a chat holds the lock.
            if not self.client.validate_before_audio(event_id):
                print(f"[MedicationCare] event={event_id} resolved while queued; "
                      "check cancelled.", flush=True)
                return False
            if validity_check is not None and not validity_check():
                print(f"[MedicationCare] event={event_id} schedule changed before audio; "
                      "check cancelled.", flush=True)
                return False
            return True

        return self.conversation.run_medication_check(
            event_id, validity_check, before_start=before_start,
        )

    def stop(self):
        with self._queue_lock:
            self.stop_event.set()
            # Discard slots that never started. Shutdown must not launch their
            # Firebase claims, reminders or microphones after the current work.
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
                else:
                    self._queue.task_done()
            self._queue.put(None)  # Wake an idle worker without waiting five seconds.
            started = self._started
        if started and threading.current_thread() is not self._thread:
            self._thread.join(timeout=3)


class MedicationAnnouncementCare:
    """Durably reserve and asynchronously play announcement-only reminders."""

    RETRY_SECONDS = 5

    def __init__(self, client, announce, interaction_lock=None):
        self.client = client
        self.announce = announce
        self.interaction_lock = interaction_lock
        self.stop_event = threading.Event()
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self._scheduled = set()
        self._started = False
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="medication-announcement",
        )

    def start(self):
        with self._lock:
            if not self._started:
                self._started = True
                self._thread.start()
        return self

    def run_scheduled(self, slot_key, event, validity_check=None):
        occurrence = MedicationCare._occurrence_key(slot_key, event)
        with self._lock:
            if self.stop_event.is_set() or occurrence in self._scheduled:
                return False
            self._scheduled.add(occurrence)
            self._queue.put((slot_key, event, validity_check))
        print(f"[MedicationCare] slot={slot_key} queued for one announcement.", flush=True)
        return True

    def _run(self):
        while not self.stop_event.is_set():
            try:
                item = self._queue.get(timeout=self.RETRY_SECONDS)
            except queue.Empty:
                if not self.stop_event.is_set():
                    try:
                        self.client.retry_pending()
                    except Exception as exc:
                        print(f"[MedicationCare] result retry failed ({type(exc).__name__}); "
                              "audio will not repeat.", flush=True)
                continue
            try:
                if item is None or self.stop_event.is_set():
                    return
                slot_key, event, validity_check = item
                if validity_check is not None and not validity_check():
                    continue
                self.client.run_announcement(
                    slot_key,
                    event,
                    lambda event_id: self._announce(event_id, validity_check),
                )
            except Exception as exc:
                print(f"[MedicationCare] announcement worker failed "
                      f"({type(exc).__name__}); audio will not repeat.", flush=True)
            finally:
                self._queue.task_done()

    def _announce(self, event_id, validity_check):
        if self.interaction_lock is None:
            return self._announce_with_focus(event_id, validity_check)
        # Announcement-only mode still shares the full chat/care interaction,
        # not merely the playback lock between microphone or player operations.
        while not self.stop_event.is_set():
            if self.interaction_lock.acquire(timeout=0.1):
                try:
                    return self._announce_with_focus(event_id, validity_check)
                finally:
                    self.interaction_lock.release()
        return "cancelled"

    def _announce_with_focus(self, event_id, validity_check):
        def before_audio():
            if self.stop_event.is_set():
                return False
            if validity_check is not None and not validity_check():
                return False
            return self.client.validate_before_audio(event_id)

        if self.stop_event.is_set():
            return "cancelled"
        return self.announce(before_audio)

    def stop(self):
        with self._lock:
            self.stop_event.set()
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
                else:
                    self._queue.task_done()
            self._queue.put(None)
            started = self._started
        if started and threading.current_thread() is not self._thread:
            self._thread.join(timeout=3)
