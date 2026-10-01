"""One device runtime owns all dispatch and reset for an elder on this host."""
from copy import deepcopy
import threading
import unittest
import uuid
from unittest.mock import Mock

from test_memory_protocol_v3 import row
from memory_protocol_fake import ProtocolFirebase
from persistent_memory import INITIAL_GENERATION, MemoryStore, PersistentMemoryService


class ServiceOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.http = ProtocolFirebase({'_control': {'generation': INITIAL_GENERATION, 'activeIds': {'old': True}},
                                      'active': {'old': row()}})
        self.url = 'https://' + uuid.uuid4().hex + '.invalid'
        self.first = self.service()
        self.second = self.service()

    def service(self):
        service = PersistentMemoryService(MemoryStore(self.url, 'elder_1', self.http), Mock(), device_id='device_001')
        self.addCleanup(service.stop)
        return service

    def test_second_runtime_cannot_dispatch_or_complete_reset_for_paused_first(self):
        self.assertTrue(self.first.start())
        self.assertTrue(self.first.wait_prefetch(1))
        self.assertFalse(self.second.start())
        self.second.prefetch(force=True)
        self.assertTrue(self.second.wait_prefetch(1))
        self.assertEqual(self.second.request_context('요가', INITIAL_GENERATION).dispatch(deepcopy), [])
        self.assertFalse(self.second.reset_worker._thread and self.second.reset_worker._thread.is_alive())
        self.assertTrue(self.first.request_context('요가', INITIAL_GENERATION).dispatch(deepcopy))
        self.assertTrue(self.first.stop())
        self.assertTrue(self.second.start())
        self.assertTrue(self.second.wait_prefetch(1))

    def test_shutdown_timeout_retains_ownership_until_active_callback_exits(self):
        self.assertTrue(self.first.start())
        self.assertTrue(self.first.wait_prefetch(1))
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def callback(messages):
            self.assertTrue(messages)
            entered.set()
            release.wait(2)
        thread = threading.Thread(target=lambda: self.first.request_context('요가', INITIAL_GENERATION).dispatch(callback))
        thread.start()
        self.assertTrue(entered.wait(1))
        self.assertFalse(self.first.stop(timeout=.01))
        self.assertFalse(self.second.start())
        release.set()
        thread.join(2)
        self.assertTrue(self.first.stop(timeout=1))
        self.assertTrue(self.second.start())
