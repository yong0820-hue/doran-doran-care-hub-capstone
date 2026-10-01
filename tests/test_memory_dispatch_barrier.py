"""Deterministic callback suspension races: no wall-clock lease can revoke a reader."""
from copy import deepcopy
import threading
import time
import unittest
from unittest.mock import Mock, patch

from test_memory_protocol_v3 import row
from memory_protocol_fake import ProtocolFirebase
from persistent_memory import INITIAL_GENERATION, MemoryStore, PersistentMemoryService
from memory_reset import MemoryResetWorker


class DispatchBarrierTests(unittest.TestCase):
    def setUp(self):
        self.http = ProtocolFirebase({'_control': {'generation': INITIAL_GENERATION, 'activeIds': {'old': True}},
                                      'active': {'old': row()}})
        self.service = PersistentMemoryService(MemoryStore('https://example.invalid', 'elder_1', self.http), Mock())
        self.addCleanup(self.service.stop)
        self.service.prefetch()
        self.assertTrue(self.service.wait_prefetch(1))

    def test_a_pause_immediately_before_callback_cannot_be_overtaken_by_reset(self):
        barrier = self.service.dispatch_barrier
        selected, resume, initiated, completed = (threading.Event() for _ in range(4))
        self.addCleanup(resume.set)
        original = self.service._read_cache.selected
        observed = []
        def installed(control):
            self.assertTrue(initiated.is_set())
            self.assertNotEqual(self.http.operations['reset-a']['status'], 'completed')
            self.service._apply_memory_reset(control)

        reset_worker = MemoryResetWorker(self.service.store, barrier, installed)
        reset_worker.BARRIER_TIMEOUT = 1

        def select(*args):
            records = original(*args)
            selected.set()
            self.assertTrue(resume.wait(2))
            return records

        def reset():
            if reset_worker.process_pending() == 1:
                completed.set()

        def send(messages):
            observed.extend(messages)
            initiated.set()

        with patch.object(self.service._read_cache, 'selected', side_effect=select):
            request = self.service.request_context('요가', INITIAL_GENERATION)
            reader = threading.Thread(target=lambda: request.dispatch(send))
            reader.start()
            self.assertTrue(selected.wait(1))
            self.http.operations['reset-a'] = {'status': 'requested', 'deviceId': 'device_001',
                                              'requestedAt': int(self.http.now * 1000),
                                              'requestedBy': 'guardian_dashboard'}
            # Advance remote clock beyond every former lease TTL.
            self.http.now += 3600
            writer = threading.Thread(target=reset)
            writer.start()
            self.assertTrue(barrier.wait_reset_pending(1))
            self.assertFalse(completed.is_set())
            self.assertNotEqual(self.http.operations['reset-a']['status'], 'completed')
            self.assertFalse(initiated.is_set())
            resume.set()
            reader.join(2)
            writer.join(2)
        self.assertFalse(reader.is_alive() or writer.is_alive())
        self.assertTrue(observed)
        self.assertTrue(completed.is_set())
        self.assertEqual(self.http.operations['reset-a']['status'], 'completed')
        self.assertEqual(self.service.request_context('요가', INITIAL_GENERATION).dispatch(deepcopy), [])

    def test_b_exclusive_reset_new_turn_falls_back_without_old_context(self):
        barrier = self.service.dispatch_barrier
        self.assertTrue(barrier.acquire_exclusive(timeout=.1))
        try:
            started = time.monotonic()
            self.assertEqual(self.service.request_context('요가', INITIAL_GENERATION).dispatch(deepcopy), [])
            self.assertLess(time.monotonic() - started, .1)
        finally:
            barrier.release_exclusive()

    def test_writer_timeout_never_revokes_paused_reader_and_blocks_new_readers(self):
        barrier = self.service.dispatch_barrier
        self.assertTrue(barrier.acquire_shared())
        started = time.monotonic()
        self.assertFalse(barrier.acquire_exclusive(timeout=.01))
        self.assertLess(time.monotonic() - started, .2)
        self.assertFalse(barrier.acquire_shared())
        barrier.release_shared()
        self.assertTrue(barrier.acquire_exclusive(timeout=.1))
        barrier.release_exclusive()
        self.assertTrue(barrier.acquire_shared())
        barrier.release_shared()

    def test_prepared_request_is_revalidated_inside_barrier_after_reset(self):
        request = self.service.request_context('요가', INITIAL_GENERATION)
        self.http.reset()
        self.service._apply_memory_reset(self.http.root['_control'])
        self.assertEqual(request.dispatch(deepcopy), [])

    def test_sdk_exception_releases_reader_and_request_is_one_shot(self):
        request = self.service.request_context('요가', INITIAL_GENERATION)
        with self.assertRaises(TimeoutError):
            request.dispatch(Mock(side_effect=TimeoutError()))
        self.assertTrue(self.service.dispatch_barrier.acquire_exclusive(timeout=.1))
        self.service.dispatch_barrier.release_exclusive()
        with self.assertRaises(RuntimeError):
            request.dispatch(deepcopy)
