"""Process-local, writer-preferring dispatch fence for single-device memory v1.

Readers have no TTL. A suspended reader must exit before reset can complete.
Timeout bounds the reset worker's attempt, never the lifetime of a reader.
"""
import threading
import time


class MemoryDispatchBarrier:
    def __init__(self):
        self._condition = threading.Condition()
        self._readers = 0
        self._writer = False
        self._reset_pending = False

    def acquire_shared(self):
        """Voice never waits: a reset contender immediately selects no-memory."""
        with self._condition:
            if self._writer or self._reset_pending:
                return False
            self._readers += 1
            return True

    def release_shared(self):
        with self._condition:
            if self._readers <= 0:
                raise RuntimeError('Unbalanced memory dispatch release')
            self._readers -= 1
            self._condition.notify_all()

    def acquire_exclusive(self, timeout):
        deadline = time.monotonic() + max(0, timeout)
        with self._condition:
            self._reset_pending = True
            self._condition.notify_all()
            while self._readers or self._writer:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            self._writer = True
            return True

    def release_exclusive(self, clear_pending=True):
        with self._condition:
            if not self._writer:
                raise RuntimeError('Unbalanced memory reset release')
            self._writer = False
            self._reset_pending = not clear_pending
            self._condition.notify_all()

    def wait_reset_pending(self, timeout):
        """Bounded diagnostic/test observation; never grants ownership."""
        with self._condition:
            return self._condition.wait_for(lambda: self._reset_pending, timeout)
