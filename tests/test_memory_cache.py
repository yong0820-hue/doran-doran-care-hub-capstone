"""Deterministic reset races and bounded, offline-safe retrieval policy."""
from copy import deepcopy
import json
import threading
import time
import unittest
from unittest.mock import Mock, patch

from test_persistent_memory import FakeFirebase, timestamp
from persistent_memory import INITIAL_GENERATION, MemoryStore, PersistentMemoryService


def record(index=0, **extra):
    return {"id": str(index), "elderId": "elder_1", "source": "conversation",
            "type": "profile", "subject": f"친구{index}", "content": f"친구{index}는 오랜 친구야",
            "status": "active", "lastMentionedAt": timestamp() + index,
            "createdAt": timestamp(), "importance": 3, **extra}


class MemoryCacheTests(unittest.TestCase):
    def setUp(self):
        self.http = FakeFirebase({'old': record()})
        self.store = MemoryStore('https://example.firebaseio.com', 'elder_1', self.http)
        self.service = PersistentMemoryService(self.store, Mock())
        self.addCleanup(self.service.stop)

    def context(self, query='친구', generation=INITIAL_GENERATION, wait_release=True):
        with self.service.request_context(query, generation) as request:
            result = request.dispatch(deepcopy)
        return result

    def warm(self):
        self.service.prefetch()
        self.assertTrue(self.service.wait_prefetch(1))

    def test_retrieve_starts_reset_commits_old_response_arrives_is_discarded(self):
        original_read = self.store.read
        entered, release = threading.Event(), threading.Event()
        def delayed_read():
            snapshot = original_read()
            entered.set()
            self.assertTrue(release.wait(2))
            return snapshot
        self.store.read = delayed_read
        result = []
        worker = threading.Thread(target=lambda: result.append(self.store.retrieve('친구', INITIAL_GENERATION)))
        worker.start()
        self.assertTrue(entered.wait(1))
        self.http.reset()
        self.assertEqual(self.http.root['_control']['activeIds'], {})
        release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [[]])

    def test_session_start_is_nonblocking_and_cold_context_has_no_network(self):
        entered, release = threading.Event(), threading.Event()
        def blocked_get(*args, **kwargs):
            entered.set()
            release.wait(2)
            raise TimeoutError()
        self.http.get = blocked_get
        start = time.monotonic()
        try:
            self.assertIsNone(self.service.begin_session())
            self.assertEqual(self.context(), [])
            self.assertLess(time.monotonic() - start, .1)
        finally:
            release.set()

    def test_warm_turns_do_not_download_bucket(self):
        self.warm()
        self.http.get_calls.clear()
        for _ in range(10):
            self.assertTrue(self.context())
        # The local dispatch barrier requires no HTTP read/write on warm turns.
        self.assertEqual(len(self.http.get_calls), 0)
        self.assertTrue(all(url.endswith('/_control.json') for url, _ in self.http.get_calls))
        self.assertFalse(any(url == self.store.url for url, _ in self.http.get_calls))

    def test_background_snapshot_late_after_reset_never_populates_context(self):
        original_read = self.store.cache_snapshot
        entered, release = threading.Event(), threading.Event()
        def delayed():
            snapshot = original_read()
            entered.set()
            self.assertTrue(release.wait(2))
            return snapshot
        self.store.cache_snapshot = delayed
        self.service.prefetch()
        self.assertTrue(entered.wait(1))
        self.http.reset()
        self.service._apply_memory_reset(self.http.root['_control'])
        release.set()
        self.assertTrue(self.service.wait_prefetch(1))
        self.assertEqual(self.context(), [])
        self.assertEqual(self.service._read_cache.records, {})

    def test_reset_commits_after_prefetch_before_turn_discards_cached_context(self):
        self.service.prefetch()
        self.assertTrue(self.service.wait_prefetch(1))
        self.http.reset()
        self.service._apply_memory_reset(self.http.root['_control'])
        self.http.get_calls.clear()
        self.assertEqual(self.context(), [])
        self.assertEqual(len(self.http.get_calls), 0)
        self.assertEqual(self.service._read_cache.records, {})

    def test_pending_local_reset_blocks_warm_cache_without_control_round_trip(self):
        self.warm()
        self.http.get_calls.clear()
        barrier = self.service.dispatch_barrier
        self.assertTrue(barrier.acquire_exclusive(timeout=.1))
        try:
            self.assertEqual(self.context(), [])
            self.assertEqual(self.http.get_calls, [])
        finally:
            barrier.release_exclusive()

    def test_unknown_reset_keeps_warm_context_blocked_until_same_operation_resolves(self):
        self.warm()
        barrier = self.service.dispatch_barrier
        self.assertTrue(barrier.acquire_exclusive(timeout=.1))
        barrier.release_exclusive(clear_pending=False)
        self.assertEqual(self.context(), [])
        self.assertTrue(barrier.acquire_exclusive(timeout=.1))
        self.http.reset()
        self.service._apply_memory_reset(self.http.root['_control'])
        barrier.release_exclusive()
        self.assertEqual(self.context(), [])

    def test_expired_cache_is_not_used_and_byte_budget_cannot_be_bypassed(self):
        # Each stored body satisfies the 4 KiB schema limit; their aggregate
        # still exceeds the 128 KiB cache budget.
        self.http.seed({str(i): record(i, subject=str(i) + '친' * 57,
                                      content='가' * 280, tags=['가' * 30] * 6)
                        for i in range(128)})
        self.warm()
        size = len(json.dumps(self.service._read_cache.records, ensure_ascii=False).encode('utf-8'))
        self.assertLessEqual(size, 128 * 1024)
        self.assertGreater(size, 0)
        self.service._read_cache.loaded_at -= 61
        self.http.get_calls.clear()
        self.assertEqual(self.context(), [])
        self.assertEqual(self.http.get_calls, [])

    def test_cache_byte_bound_includes_record_keys_and_json_structure(self):
        self.http.seed({('k' * 75) + str(i): record(i) for i in range(12)})
        with patch('persistent_memory.MAX_CACHE_BYTES', 1000):
            self.service.prefetch()
            self.assertTrue(self.service.wait_prefetch(1))
        self.assertLessEqual(len(json.dumps(self.service._read_cache.records,
                                          ensure_ascii=False).encode('utf-8')), 1000)
        self.assertGreater(len(self.service._read_cache.records), 0)

    def test_save_also_compacts_owned_records_under_same_cas(self):
        self.http.seed({str(i): record(i) for i in range(128)})
        candidate = {'type': 'profile', 'subject': '새친구', 'content': '새친구는 영수야',
                     'lastMentionedAt': timestamp() + 500, 'importance': 3}
        self.assertTrue(self.store.save([candidate], 'session', INITIAL_GENERATION))
        identities = self.http.root['_control']['activeIds']
        self.assertEqual(len(identities), 128)
        rows = [self.http.root['active'][identity] for identity in identities]
        self.assertIn('새친구', [row['subject'] for row in rows])
        self.assertNotIn('친구0', [row['subject'] for row in rows])
        self.assertFalse(any(call[0] == self.store.url for call in self.http.put_calls))

    def test_oversize_manifest_fails_closed_without_downloading_record_bodies(self):
        self.http.seed({str(i): record(i) for i in range(129)})
        self.warm()
        self.assertEqual(self.service._read_cache.records, {})
        self.assertIsNone(self.service.begin_session())
        self.assertEqual(self.context(), [])
        self.assertTrue(all(url.endswith('/_control.json') for url, _ in self.http.get_calls))
        self.assertFalse(self.store.save([record(999)], 'session', INITIAL_GENERATION))
        self.assertEqual(self.http.put_calls, [])

    def test_oversize_record_is_excluded_from_cache(self):
        self.http.seed({'old': record(tags=['가' * 5000])})
        self.warm()
        self.assertEqual(self.service._read_cache.records, {})
        self.assertEqual(self.context(), [])

    def test_generation_migration_is_monotonic_and_invalid_values_fail_closed(self):
        for generation in (INITIAL_GENERATION, '-legacyPush', 1, 2):
            self.assertTrue(self.store.observe_generation(generation))
        for generation in (INITIAL_GENERATION, '-legacyPush', 1):
            self.assertFalse(self.store.observe_generation(generation))
        for generation in (True, [], {}, -1, 2.5, 9007199254740992):
            with self.subTest(generation=generation), self.assertRaises(ValueError):
                self.store.observe_generation(generation)

    def test_higher_generation_invalidates_cache_and_late_lower_check(self):
        self.service.prefetch()
        self.assertTrue(self.service.wait_prefetch(1))
        self.http.data['_control'] = {'generation': 2}
        self.service._apply_memory_reset(self.http.root['_control'])
        self.assertEqual(self.context(), [])
        self.assertFalse(self.store.observe_generation(1))
        self.http.data['_control'] = {'generation': 1}
        self.assertEqual(self.context(), [])
        self.assertEqual(self.store.observed_generation, 2)

    def test_higher_generation_observed_during_serialization_discards_context(self):
        self.service.prefetch()
        self.assertTrue(self.service.wait_prefetch(1))
        original = json.dumps
        def reset_during_serialization(*args, **kwargs):
            # Only the final prompt serialization advances the local observer;
            # transport JSON encoding must retain its normal CAS behavior.
            if isinstance(args[0], list):
                self.store.observe_generation(2)
            return original(*args, **kwargs)
        with patch('persistent_memory.json.dumps', side_effect=reset_during_serialization):
            self.assertEqual(self.context(), [])

    def test_warm_dispatch_has_no_transport_dependency(self):
        self.warm()
        self.http.get = Mock(side_effect=TimeoutError())
        started = time.monotonic()
        self.assertTrue(self.context())
        self.assertLess(time.monotonic() - started, .1)
        self.http.get.assert_not_called()
        # An offline reset cannot complete. Once local reset begins, dispatch
        # falls back immediately even if reset's transport is unavailable.
        self.assertTrue(self.service.dispatch_barrier.acquire_exclusive(.1))
        try:
            self.assertEqual(self.context(), [])
        finally:
            self.service.dispatch_barrier.release_exclusive()

    def test_cache_and_owned_retention_are_bounded_preserve_foreign_records(self):
        now = int(time.time() * 1000)
        data = {str(i): record(i) for i in range(300)}
        data.update({f'a{i}': record(i, status='archived', archivedAt=now) for i in range(100)})
        data['expired'] = record(validUntil=now - 1)
        data['ancient'] = record(status='archived', archivedAt=now - 31 * 86400000)
        data['legacy'] = {'source': 'user', 'content': 'preserve'}
        data['_control'] = {'generation': 2, 'lastResetOperationId': 'preserve'}
        original = deepcopy(data)
        retained = self.store._prune(data, now_ms=now)
        self.assertEqual(len([v for v in retained.values() if v.get('status') == 'active']), 128)
        self.assertEqual(len([v for v in retained.values() if v.get('status') == 'archived']), 64)
        self.assertNotIn('expired', retained)
        self.assertNotIn('ancient', retained)
        self.assertEqual(retained['legacy'], data['legacy'])
        self.assertEqual(retained['_control'], data['_control'])
        self.assertIn('299', retained)
        self.assertNotIn('0', retained)
        self.assertEqual(data, original)
        self.http.seed(retained)
        self.warm()
        self.assertEqual(len(self.service._read_cache.records), 128)
        self.assertFalse(any(url == self.store.url or '/archive/' in url
                             for url, _ in self.http.get_calls))


if __name__ == '__main__':
    unittest.main()
