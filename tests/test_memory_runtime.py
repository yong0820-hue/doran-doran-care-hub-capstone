"""OS ownership fences for one live memory runtime per database/elder scope."""

import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'raspberry-pi-voice-hub'))
from memory_runtime import MemoryRuntimeOwnership


class MemoryRuntimeOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.scope = 'https://example.invalid/memories/test_' + uuid.uuid4().hex + '.json'
        self.owner = MemoryRuntimeOwnership(self.scope)
        self.addCleanup(self.owner.release)

    def test_same_scope_second_instance_fails_without_blocking(self):
        second = MemoryRuntimeOwnership(self.scope)
        self.addCleanup(second.release)
        self.assertTrue(self.owner.acquire())
        self.assertTrue(self.owner.held)
        self.assertTrue(self.owner.acquire())
        self.assertFalse(second.acquire())
        self.assertFalse(second.held)
        self.owner.release()
        self.assertTrue(second.acquire())

    def test_different_elder_scope_has_independent_ownership(self):
        second = MemoryRuntimeOwnership(self.scope + '_other')
        self.addCleanup(second.release)
        self.assertTrue(self.owner.acquire())
        self.assertTrue(second.acquire())

    def test_stable_path_uses_tempdir_and_hash_without_exposing_database_url(self):
        expected = Path(tempfile.gettempdir()) / 'doran-memory-runtime' / (hashlib.sha256(self.scope.encode('utf-8')).hexdigest() + '.lock')
        self.assertEqual(self.owner.path, expected)
        self.assertTrue(self.owner.acquire())
        self.owner.release()  # Windows denies reads of another handle's locked byte.
        self.assertNotIn(b'https:', self.owner.path.read_bytes())

    def test_release_is_idempotent_and_never_unlinks_lock_file(self):
        self.assertTrue(self.owner.acquire())
        identity = self.owner.path.stat().st_ino
        self.owner.release()
        self.owner.release()
        self.assertFalse(self.owner.held)
        self.assertTrue(self.owner.path.exists())
        self.assertTrue(self.owner.acquire())
        self.assertEqual(self.owner.path.stat().st_ino, identity)

    def test_failed_open_returns_unheld_and_retry_can_acquire(self):
        with patch('memory_runtime.os.open', side_effect=PermissionError('blocked')):
            self.assertFalse(self.owner.acquire())
        self.assertFalse(self.owner.held)
        self.assertTrue(self.owner.acquire())

    def test_failed_os_lock_closes_its_file_descriptor(self):
        import memory_runtime
        lock_name = 'memory_runtime.msvcrt.locking' if sys.platform == 'win32' else 'memory_runtime.fcntl.flock'
        opened = []
        real_open = memory_runtime.os.open
        def record_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd
        with patch('memory_runtime.os.open', side_effect=record_open), patch(lock_name, side_effect=OSError('busy')):
            self.assertFalse(self.owner.acquire())
        self.assertFalse(self.owner.held)
        for fd in opened:
            with self.assertRaises(OSError):
                memory_runtime.os.fstat(fd)
        self.assertTrue(self.owner.acquire())

    def test_child_process_blocks_other_owner_until_killed(self):
        module_dir = str(Path(__file__).resolve().parents[1] / 'raspberry-pi-voice-hub')
        code = ('import sys,time; sys.path.insert(0,sys.argv[1]); '
                'from memory_runtime import MemoryRuntimeOwnership; '
                'owner=MemoryRuntimeOwnership(sys.argv[2]); '
                'print(owner.acquire(),flush=True); time.sleep(60)')
        child = subprocess.Popen([sys.executable, '-u', '-c', code, module_dir, self.scope],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def stop_child():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            child.stdout.close()
            child.stderr.close()
        self.addCleanup(stop_child)
        line = []
        ready = threading.Event()
        def read_ready():
            line.append(child.stdout.readline())
            ready.set()
        reader = threading.Thread(target=read_ready, daemon=True)
        reader.start()
        self.assertTrue(ready.wait(5), 'child did not report ownership within budget')
        self.assertEqual(line, ['True\n'])
        self.assertFalse(self.owner.acquire())
        child.kill()
        child.wait(timeout=5)
        self.assertTrue(self.owner.acquire())


if __name__ == '__main__':
    unittest.main()
