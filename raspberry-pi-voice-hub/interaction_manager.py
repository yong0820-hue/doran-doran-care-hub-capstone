"""Serialize voice interactions and frame proactive care in Doran's persona.

The manager owns coordination only. Care outcomes remain deterministic in the
medication and inactivity classifiers; the model is allowed to rewrite only the
spoken question, from a closed set of trusted inputs.
"""

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
import threading
import time


CARE_PROMPT_INSTRUCTIONS = """
너는 어르신과 따뜻하게 대화하는 AI 손주 '도란이'다.
이 호출에서는 제공된 돌봄 질문의 말투만 자연스럽게 작성한다.
- 한국어 존댓말로 1~2문장, 120자 이내로 쓴다.
- 기본 호칭은 '어르신'으로 한다.
- 직전 응답이 끝난 안전한 틈에 자연스럽게 끼어드는 연결 표현을 넣는다.
- 사용자가 약을 먹었거나 괜찮다고 이미 확인된 것처럼 말하지 않는다.
- 상태를 판정하거나 기록했다고 말하지 않는다.
- 새 사실, 의학적 판단, 명령, 도구 호출, 마크다운을 넣지 않는다.
- 질문은 물음표로 끝낸다.
""".strip()

CARE_PROMPT_REQUESTS = {
    "medication": (
        "복약 시간이 되었음을 알리고, 대화 중 잠깐 약부터 챙긴 뒤 이야기를 "
        "이어가도 될지 어르신께 묻는 질문을 작성한다."
    ),
    "inactivity": (
        "한동안 움직임이 거의 없었던 점을 조심스럽게 말하고, 몸이 "
        "괜찮은지 어르신께 묻는 질문을 작성한다."
    ),
}


@dataclass(frozen=True)
class InteractionContext:
    """Trusted orchestration context; it never contains conversation text."""

    kind: str
    after_conversation: bool = False


@dataclass
class _PendingCare:
    kind: str
    action: object
    check_stopped: object
    done: threading.Event = field(default_factory=threading.Event)
    state: str = "queued"
    result: object = None
    error: BaseException = None


class InteractionManager:
    """Serialize voice work while allowing care at safe chat boundaries.

    Care workers enqueue after their durable event claim, then wait for either
    the active conversation thread or an idle care worker to execute the work.
    The conversation calls ``drain_pending`` only between complete turns, so no
    care question can interrupt playback or microphone capture.
    """

    CARE_KINDS = frozenset({"medication", "inactivity"})
    BRIDGE_GRACE_SECONDS = 10.0

    def __init__(self, lock=None):
        self.lock = lock if lock is not None else threading.RLock()
        self._state_lock = threading.Lock()
        self._local = threading.local()
        self._active_kind = None
        self._last_completed_kind = None
        self._last_completed_at = 0.0
        self._bridge_available = False
        self._pending = deque()

    @contextmanager
    def use(self, lock, kind, check_stopped):
        """Acquire ``lock`` interruptibly and preserve re-entrant semantics."""
        depth = getattr(self._local, "depth", 0)
        context = getattr(self._local, "context", None)
        while not lock.acquire(timeout=0.1):
            check_stopped()
        try:
            check_stopped()
            if depth:
                self._local.depth = depth + 1
                try:
                    yield context
                finally:
                    self._local.depth = depth
                return

            now = time.monotonic()
            with self._state_lock:
                recent_conversation = (
                    self._bridge_available
                    and self._last_completed_kind == "conversation"
                    and now - self._last_completed_at <= self.BRIDGE_GRACE_SECONDS
                )
                after_conversation = kind in self.CARE_KINDS and recent_conversation
                if after_conversation:
                    self._bridge_available = False
                self._active_kind = kind
            context = InteractionContext(
                kind=kind, after_conversation=after_conversation,
            )
            self._local.depth = 1
            self._local.context = context
            completed = False
            try:
                yield context
                completed = True
            finally:
                completed_at = time.monotonic()
                with self._state_lock:
                    self._active_kind = None
                    self._last_completed_kind = kind
                    self._last_completed_at = completed_at
                    if kind == "conversation":
                        self._bridge_available = completed
                self._local.depth = 0
                self._local.context = None
        finally:
            lock.release()

    def run_care(self, lock, kind, check_stopped, action):
        """Queue one care action and return its result to the claiming worker."""
        if kind not in self.CARE_KINDS:
            raise ValueError("Unknown care interaction")
        pending = _PendingCare(kind, action, check_stopped)
        with self._state_lock:
            self._pending.append(pending)

        while not pending.done.is_set():
            self._drain_when_idle(lock)
            if pending.done.wait(0.05):
                break
            try:
                check_stopped()
            except BaseException:
                with self._state_lock:
                    if pending.state == "queued":
                        self._pending.remove(pending)
                        pending.state = "cancelled"
                        raise
                # A boundary already owns this action. Its own stop check will
                # cancel promptly; wait for it to publish the real result.

        if pending.error is not None:
            raise pending.error
        return pending.result

    def drain_pending(self, check_stopped):
        """Run all care queued for the current conversation turn boundary."""
        context = getattr(self._local, "context", None)
        if context is None or context.kind != "conversation":
            return []
        outcomes = []
        while True:
            check_stopped()
            pending = self._claim_pending()
            if pending is None:
                return outcomes
            self._execute_pending(pending, after_conversation=True)
            outcomes.append((pending.kind, pending.result))

    def _drain_when_idle(self, lock):
        """Let a care worker drain work only when no chat owns the lock."""
        try:
            acquired = lock.acquire(blocking=False)
        except TypeError:
            acquired = lock.acquire(timeout=0)
        if not acquired:
            return
        try:
            with self._state_lock:
                if self._active_kind is not None:
                    return
            while True:
                pending = self._claim_pending()
                if pending is None:
                    return
                now = time.monotonic()
                with self._state_lock:
                    after_conversation = (
                        self._bridge_available
                        and self._last_completed_kind == "conversation"
                        and now - self._last_completed_at <= self.BRIDGE_GRACE_SECONDS
                    )
                    if after_conversation:
                        self._bridge_available = False
                self._execute_pending(pending, after_conversation=after_conversation)
        finally:
            lock.release()

    def _claim_pending(self):
        with self._state_lock:
            while self._pending:
                pending = self._pending.popleft()
                if pending.state == "queued":
                    pending.state = "running"
                    return pending
        return None

    def _execute_pending(self, pending, after_conversation):
        """Execute one claimed action with a temporary care identity."""
        previous_depth = getattr(self._local, "depth", 0)
        previous_context = getattr(self._local, "context", None)
        context = InteractionContext(
            kind=pending.kind, after_conversation=after_conversation,
        )
        with self._state_lock:
            previous_kind = self._active_kind
            self._active_kind = pending.kind
        self._local.depth = max(previous_depth, 1)
        self._local.context = context
        try:
            pending.check_stopped()
            pending.result = pending.action(context)
        except BaseException as exc:
            pending.error = exc
        finally:
            completed_at = time.monotonic()
            self._local.depth = previous_depth
            self._local.context = previous_context
            with self._state_lock:
                self._active_kind = previous_kind
                self._last_completed_kind = pending.kind
                self._last_completed_at = completed_at
                pending.state = "done"
            pending.done.set()


class CarePromptService:
    """Generate only contextual care wording, with a fixed prompt fallback."""

    MAX_PROMPT_CHARS = 120

    def __init__(self, client, model):
        self.client = client
        self.model = model

    def generate(self, care_type, fallback_prompt, after_conversation):
        if not after_conversation:
            return fallback_prompt
        request = CARE_PROMPT_REQUESTS.get(care_type)
        if request is None:
            return fallback_prompt
        try:
            response = self.client.responses.create(
                model=self.model,
                instructions=CARE_PROMPT_INSTRUCTIONS,
                input=request,
                max_output_tokens=100,
                store=False,
            )
            text = response.output_text.strip()
            if response.status != "completed" or not self._is_safe(care_type, text):
                raise ValueError("invalid care prompt")
            return text
        except Exception as exc:
            print(
                f"[InteractionManager] {care_type} prompt fallback "
                f"({type(exc).__name__}).",
                flush=True,
            )
            return fallback_prompt

    def _is_safe(self, care_type, text):
        if (not text or len(text) > self.MAX_PROMPT_CHARS
                or "\n" in text or "\r" in text
                or not text.endswith("?") or "어르신" not in text):
            return False
        if any(phrase in text for phrase in (
                "드셨네요", "복용하셨네요", "확인됐", "확인했",
                "괜찮으시네요", "문제없으시네요")):
            return False
        if care_type == "medication":
            return "약" in text and any(
                word in text for word in ("드실", "드셨", "챙겨", "복용")
            )
        if care_type == "inactivity":
            return "괜찮" in text and any(
                word in text for word in ("움직임", "움직이지", "아까부터", "한동안")
            )
        return False
