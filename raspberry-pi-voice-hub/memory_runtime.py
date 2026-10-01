"""Process ownership for a single-device memory dispatch/reset runtime.

Advisory locks have no TTL: a paused process keeps ownership, and only release
or process death transfers it. Every runtime for an elder must use this fence;
the in-process dispatch barrier then covers all of that owner's API calls.
"""

import hashlib
import os
from pathlib import Path
import tempfile
import threading

if os.name == 'nt':
    import msvcrt
else:
    import fcntl


class MemoryRuntimeOwnership:
    def __init__(self, scope):
        digest = hashlib.sha256(str(scope).encode('utf-8')).hexdigest()
        # Scope is the canonical memory store URL, not cwd, device ID or outbox.
        # Keep the inode permanently: unlinking a held lock lets another process
        # create a second inode and acquire an unrelated lock at the same path.
        self.path = Path(tempfile.gettempdir()) / 'doran-memory-runtime' / (digest + '.lock')
        self._fd = None
        self._mutex = threading.Lock()

    @property
    def held(self):
        with self._mutex:
            return self._fd is not None

    def acquire(self):
        """Attempt ownership once without waiting; errors fail closed."""
        with self._mutex:
            if self._fd is not None:
                return True
            fd = None
            try:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
                fd = os.open(self.path, flags, 0o600)
                os.set_inheritable(fd, False)
                if os.name == 'nt':
                    # Byte-range locking needs a stable first byte. All owners
                    # write the same non-sensitive sentinel if the file is new.
                    if os.fstat(fd).st_size == 0:
                        os.write(fd, b'\0')
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fd = fd
                return True
            except OSError:
                if fd is not None:
                    os.close(fd)
                return False

    def release(self):
        """Release only after workers and dispatch have drained; never unlink."""
        with self._mutex:
            fd, self._fd = self._fd, None
            if fd is None:
                return
            try:
                if os.name == 'nt':
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
