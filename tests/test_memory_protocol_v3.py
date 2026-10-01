"""Deterministic H1/M6 regressions. No Firebase/network is used."""
import sys
from pathlib import Path
import time
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'raspberry-pi-voice-hub'))
from persistent_memory import INITIAL_GENERATION, MemoryStore, PersistentMemoryService
from memory_protocol_fake import ProtocolFirebase


def row():
    return dict(id='old', elderId='elder_1', source='conversation', type='preference',
                subject='요가', content='요가 좋아해.', status='active', importance=3,
                lastMentionedAt=int(time.time() * 1000))


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.http = ProtocolFirebase({'_control': {'generation': INITIAL_GENERATION, 'activeIds': {'old': True}},
                                      'active': {'old': row()}, 'legacy': {'content': 'x' * (2 * 1024 * 1024)}})
        self.store = MemoryStore('https://example.invalid', 'elder_1', self.http)
        self.service = PersistentMemoryService(self.store, Mock())
        self.addCleanup(self.service.stop)

    def warm(self):
        self.service.prefetch()
        self.assertTrue(self.service.wait_prefetch(2))

    def test_legacy_payload_is_never_downloaded(self):
        self.warm()
        self.assertLess(self.http.bytes_read, 10000)
        self.assertFalse(any(url == self.store.url for url, _ in self.http.get_calls))

    def test_ten_turns_no_full_bucket_and_archive_growth_no_payload_growth(self):
        self.warm()
        before = self.http.bytes_read
        for _ in range(10):
            with self.service.request_context('요가', INITIAL_GENERATION) as request:
                self.assertTrue(request.dispatch(lambda messages: messages))
        first = self.http.bytes_read - before
        self.http.root['archive'] = {str(i): {'content': 'a' * 10000} for i in range(200)}
        before = self.http.bytes_read
        for _ in range(10):
            with self.service.request_context('요가', INITIAL_GENERATION) as request:
                self.assertTrue(request.dispatch(lambda messages: messages))
        self.assertLessEqual(self.http.bytes_read - before, first + 10000)
        self.assertFalse(any(url == self.store.url for url, _ in self.http.get_calls))

    def test_single_update_never_puts_full_bucket(self):
        self.assertTrue(self.store.save([row()], 'session', INITIAL_GENERATION))
        self.assertFalse(any(url == self.store.url for url, *_ in self.http.put_calls))
        self.assertTrue(any('/active/' in url for url, *_ in self.http.put_calls))

    def test_pending_reset_cannot_acquire_old_memory(self):
        self.warm()
        self.assertTrue(self.service.dispatch_barrier.acquire_exclusive(.1))
        try:
            with self.service.request_context('요가', INITIAL_GENERATION) as request:
                self.assertEqual(request.dispatch(lambda messages: messages), [])
        finally:
            self.service.dispatch_barrier.release_exclusive()

    def test_prepared_request_cannot_keep_context_across_completed_reset(self):
        self.warm()
        with self.service.request_context('요가', INITIAL_GENERATION) as request:
            self.assertTrue(self.service.dispatch_barrier.acquire_exclusive(.1))
            self.http.reset()
            self.service._apply_memory_reset(self.http.root['_control'])
            self.service.dispatch_barrier.release_exclusive()
            self.assertEqual(request.dispatch(lambda messages: messages), [])

    def test_request_is_one_shot_even_when_no_reset_is_pending(self):
        self.warm()
        with self.service.request_context('요가', INITIAL_GENERATION) as request:
            self.assertTrue(request.dispatch(lambda messages: messages))
            with self.assertRaises(RuntimeError):
                request.dispatch(lambda messages: messages)

    def test_reset_between_body_write_and_manifest_cannot_resurrect(self):
        original = self.http.put
        def put(url, **kwargs):
            result = original(url, **kwargs)
            if '/active/' in url:
                self.http.reset()
            return result
        self.http.put = put
        self.assertFalse(self.store.save([row()], 'session', INITIAL_GENERATION))
        self.assertEqual(self.http.root['_control']['activeIds'], {})

if __name__ == '__main__':
    unittest.main()
