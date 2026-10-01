"""Pi-owned reset regressions: deterministic RTDB double, no production I/O."""
from copy import deepcopy
from pathlib import Path
import sys
import json
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'raspberry-pi-voice-hub'))
from persistent_memory import INITIAL_GENERATION, MemoryStore
from memory_protocol_fake import ProtocolFirebase
from memory_reset import MemoryResetWorker
from memory_dispatch import MemoryDispatchBarrier


class Barrier:
    def __init__(self):
        self.lock = threading.Lock()
        self.pending = False
        self.exclusive = False

    def acquire_exclusive(self, timeout):
        self.pending = True
        self.exclusive = self.lock.acquire(timeout=timeout)
        return self.exclusive

    def release_exclusive(self, clear_pending=True):
        self.exclusive = False
        self.pending = not clear_pending
        self.lock.release()


def body(**changes):
    return dict(id='old', elderId='elder_1', source='conversation', type='preference',
                subject='요가', content='요가 좋아해.', status='active', importance=3,
                lastMentionedAt=100, **changes)


class ResetWorkerTests(unittest.TestCase):
    def setUp(self):
        self.http = ProtocolFirebase({'_control': {'generation': INITIAL_GENERATION,
                                                  'activeIds': {'old': True}},
                                      'active': {'old': body()}})
        self.store = MemoryStore('https://example.invalid', 'elder_1', self.http)
        self.barrier = Barrier()
        self.events = []
        def installed(control):
            self.assertTrue(self.barrier.exclusive)
            self.assertFalse(any(op['status'] == 'completed' for op in self.http.operations.values()))
            self.events.append(control['generation'])
        self.worker = MemoryResetWorker(self.store, self.barrier, installed)
        self.worker.BARRIER_TIMEOUT = 0.01

    def request(self, key='reset1', **changes):
        control = self.http.root.get('_control', {})
        coordinator = control.get('resetCoordinator') or {}
        intent = dict(version=1, generation=control.get('generation', INITIAL_GENERATION),
                      epoch=coordinator.get('epoch', 0), operationId=None)
        if coordinator.get('deviceId') == 'device_001' and coordinator.get('state') != 'completed':
            intent['operationId'] = coordinator.get('activeOperationId')
        self.http.operations[key] = dict(status='requested', deviceId='device_001',
                                         deviceStatus='device_001|requested', intent=intent,
                                         requestedAt=int(self.http.now * 1000),
                                         requestedBy='guardian_dashboard', **changes)

    def test_offline_request_remains_pending(self):
        self.request()
        self.http.read_failure = True
        self.worker.process_pending()
        self.assertEqual(self.http.operations['reset1']['status'], 'requested')
        self.assertEqual(self.http.root['_control']['generation'], INITIAL_GENERATION)

    def test_paused_dispatch_cannot_be_bypassed_by_timeout(self):
        self.request()
        self.barrier.lock.acquire()
        try:
            self.worker.process_pending()
            self.assertNotEqual(self.http.operations['reset1']['status'], 'completed')
            self.assertEqual(self.http.root['_control']['generation'], INITIAL_GENERATION)
            self.assertTrue(self.barrier.pending)
        finally:
            self.barrier.lock.release()
        self.worker.process_pending()
        self.assertEqual(self.events, [1])
        self.assertEqual(self.http.operations['reset1']['status'], 'completed')
        self.assertEqual(self.http.root['_control']['activeIds'], {})
        self.assertFalse(self.barrier.pending)

    def test_reset_preserves_nonconversation_and_other_elder_bodies(self):
        for key, changes in [('user', {'source': 'user'}), ('foreign', {'elderId': 'elder_2'})]:
            self.http.root['active'][key] = {**body(), **changes}
            self.http.root['_control']['activeIds'][key] = True
        preserved = deepcopy(self.http.root['active'])
        self.request()
        self.worker.process_pending()
        self.assertEqual(self.http.root['_control']['activeIds'], {'user': True, 'foreign': True})
        for key in ('user', 'foreign'):
            self.assertEqual(self.http.root['active'][key], preserved[key])
        self.assertNotIn('old', self.http.root['active'])
        self.assertEqual(len(self.http.root['_control']['archiveIds']), 1)

    def test_restart_after_completed_write_loss_does_not_reset_twice(self):
        self.request()
        original = self.http.put
        def lose_completion(url, **kwargs):
            if '/memoryResetOperations/' in url and kwargs['json']['status'] == 'completed':
                raise TimeoutError('completion write lost')
            return original(url, **kwargs)
        self.http.put = lose_completion
        self.worker.process_pending()
        self.assertEqual(self.http.operations['reset1']['status'], 'processing')
        self.assertTrue(self.barrier.pending)
        self.assertEqual(self.http.root['_control']['generation'], 1)
        self.http.put = original
        restarted = MemoryResetWorker(self.store, self.barrier, lambda control: self.events.append(control['generation']))
        restarted.process_pending()
        self.assertEqual(self.http.root['_control']['generation'], 1)
        self.assertEqual(self.http.operations['reset1']['status'], 'completed')
        self.assertEqual(self.events, [1, 1])

    def test_callback_failure_never_reports_completed_and_retry_reinstalls(self):
        self.request()
        self.worker.on_reset = lambda control: (_ for _ in ()).throw(RuntimeError('cache install failed'))
        self.worker.process_pending()
        self.assertEqual(self.http.operations['reset1']['status'], 'processing')
        self.assertTrue(self.barrier.pending)
        self.worker.on_reset = lambda control: self.events.append(control['generation'])
        self.worker.process_pending()
        self.assertEqual(self.http.operations['reset1']['status'], 'completed')
        self.assertEqual(self.http.root['_control']['generation'], 1)

    def test_two_simultaneous_requests_coalesce_to_one_generation(self):
        self.request('reset1')
        self.request('reset2')
        self.worker.on_reset = lambda control: self.events.append(control['generation'])
        self.worker.process_pending()
        self.assertEqual(self.http.root['_control']['generation'], 1)
        self.assertTrue(all(op['status'] == 'completed' for op in self.http.operations.values()))
        self.assertEqual(set(self.http.root['_control']['resetOperations']), {'reset1', 'reset2'})

    def test_pending_query_and_storage_access_are_bounded(self):
        self.request()
        self.worker.process_pending()
        queries = [kwargs['params'] for url, kwargs in self.http.get_calls
                   if '/memoryResetOperations/' in url and 'params' in kwargs]
        self.assertEqual(len(queries), 3)  # Two device/status queries + one legacy page.
        self.assertTrue(all(query['orderBy'] == '"deviceStatus"' and int(query['limitToFirst']) <= 32
                            for query in queries[:2]))
        self.assertEqual(queries[2], {'orderBy': '"$key"', 'limitToFirst': 33})
        self.assertFalse(any(url == self.store.url for url, _ in self.http.get_calls))
        self.assertFalse(any(url == self.store.url for url, *_ in self.http.put_calls))
        self.assertTrue(all('/' in key for _, changes in self.http.patch_calls for key in changes))

    def test_applied_completion_timeout_is_reconciled_by_remembered_id(self):
        self.request()
        self.worker.on_reset = lambda control: self.events.append(control['generation'])
        original = self.http.put
        def delayed_ack(url, **kwargs):
            response = original(url, **kwargs)
            if '/memoryResetOperations/' in url and kwargs['json']['status'] == 'completed':
                raise TimeoutError('write applied, response lost')
            return response
        self.http.put = delayed_ack
        self.worker.process_pending()
        self.assertEqual(self.http.operations['reset1']['status'], 'completed')
        self.assertTrue(self.barrier.pending)
        self.http.put = original
        self.worker.process_pending()
        self.assertFalse(self.barrier.pending)
        self.assertEqual(self.events, [1, 1])
        self.assertEqual(self.http.root['_control']['generation'], 1)

    def test_request_arriving_during_archive_is_coalesced_after_restart(self):
        self.request()
        original = self.http.put
        def during_archive(url, **kwargs):
            if '/archive/' in url:
                self.http.now += 1
                self.request('reset2')
            return original(url, **kwargs)
        self.http.put = during_archive
        self.worker.process_pending()
        self.http.put = original
        self.assertEqual(self.http.operations['reset2']['status'], 'requested')
        restarted = MemoryResetWorker(self.store, self.barrier, lambda control: None)
        restarted.process_pending()
        self.assertEqual(self.http.root['_control']['generation'], 1)
        self.assertTrue(self.http.operations['reset2']['coalesced'])

    def test_request_created_after_completed_cutoff_gets_new_generation(self):
        self.request()
        self.worker.process_pending()
        self.http.now += 1
        self.request('reset2')
        self.worker.on_reset = lambda control: self.events.append(control['generation'])
        self.worker.process_pending()
        self.assertEqual(self.http.root['_control']['generation'], 2)
        self.assertFalse(self.http.operations['reset2']['coalesced'])

    def test_restart_reclaims_same_durable_pending_reset(self):
        self.request()
        self.http.operations['reset1'].update(status='processing', baseGeneration=INITIAL_GENERATION)
        self.http.root['_control']['pendingReset'] = {'operationId': 'reset1', 'createdAt': 1}
        self.worker.process_pending()
        self.assertEqual(self.http.operations['reset1']['status'], 'completed')
        self.assertEqual(self.events, [1])
        self.assertNotIn('pendingReset', self.http.root['_control'])

    def test_archive_timeout_stays_processing_and_reuses_staged_body(self):
        self.request()
        original = self.http.put
        def lost_archive(url, **kwargs):
            response = original(url, **kwargs)
            if '/archive/' in url:
                raise TimeoutError('archive response lost')
            return response
        self.http.put = lost_archive
        self.worker.process_pending()
        self.assertTrue(self.barrier.pending)
        self.assertEqual(self.http.root['_control']['generation'], INITIAL_GENERATION)
        self.assertEqual(self.http.operations['reset1']['status'], 'processing')
        self.http.put = original
        self.worker.process_pending()
        self.assertEqual(self.http.root['_control']['generation'], 1)
        self.assertEqual(len(self.http.root['archive']), 1)

    def test_two_workers_share_barrier_and_never_destructively_reset_twice(self):
        self.request('reset1')
        self.request('reset2')
        barrier = MemoryDispatchBarrier()
        entered, release = threading.Event(), threading.Event()
        def pause(control):
            entered.set()
            self.assertTrue(release.wait(2))
        first = MemoryResetWorker(self.store, barrier, pause)
        second = MemoryResetWorker(self.store, barrier, lambda control: None)
        second.BARRIER_TIMEOUT = 0.01
        thread = threading.Thread(target=first.process_pending)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            second.process_pending()
            self.assertFalse(barrier.acquire_shared())
            self.assertFalse(any(op['status'] == 'completed' for op in self.http.operations.values()))
            self.assertEqual(self.http.root['_control']['generation'], 1)
        finally:
            release.set()
            thread.join(2)
        second.process_pending()
        self.assertTrue(all(op['status'] == 'completed' for op in self.http.operations.values()))
        self.assertEqual(self.http.root['_control']['generation'], 1)

    def test_archive_receipts_and_deletes_remain_bounded(self):
        for index in range(100):
            key = 'old' + str(index)
            self.http.root['active'][key] = body()
            self.http.root['_control']['activeIds'][key] = True
        for index in range(40):
            self.request('reset' + str(index).zfill(2))
        self.worker.on_reset = lambda control: None
        self.worker.process_pending()
        self.worker.process_pending()
        control = self.http.root['_control']
        # All preclaim intents share an epoch, even across bounded poll batches.
        self.assertEqual(control['generation'], 1)
        self.assertTrue(all(row['status'] == 'completed' for row in self.http.operations.values()))
        self.assertEqual(control['activeIds'], {})
        self.assertEqual(len(control['archiveIds']), 64)
        self.assertEqual(len(control['resetOperations']), 32)
        self.assertEqual(len(self.http.root['archive']), 64)

    def test_old_extraction_publication_is_fenced_by_reset_control_cas(self):
        control, etag, _, _ = self.store.control_read()
        self.request()
        self.worker.process_pending()
        self.assertFalse(self.store.control_put(control, etag))
        self.assertEqual(self.http.root['_control']['generation'], 1)

    def test_equal_timestamp_request_resets_newly_published_memory(self):
        self.request()
        self.worker.process_pending()
        self.assertTrue(self.store.save([body()], 'after_reset', 1))
        self.request('reset2')  # Same server millisecond as the first reset.
        self.worker.on_reset = lambda control: None
        self.worker.process_pending()
        self.assertEqual(self.http.root['_control']['generation'], 2)
        self.assertEqual(self.http.root['_control']['activeIds'], {})
        self.assertFalse(self.http.operations['reset2']['coalesced'])

    def test_expired_archive_is_removed_only_after_ownership_verification(self):
        expired = int(self.http.now * 1000) - 31 * 86400000
        self.http.root['archive'] = {'expired': {**body(), 'status': 'archived'},
                                     'foreign': {**body(), 'source': 'user'}}
        self.http.root['_control']['archiveIds'] = {'expired': expired, 'foreign': expired}
        self.request()
        self.worker.process_pending()
        self.assertNotIn('expired', self.http.root['archive'])
        self.assertNotIn('expired', self.http.root['_control']['archiveIds'])
        self.assertIn('foreign', self.http.root['archive'])

    def test_archive_projection_stays_bounded_after_adding_reset_metadata(self):
        row = {**body(), 'sourceSessionId': ''}
        row['sourceSessionId'] = 'x' * (4096 - len(json.dumps(row, ensure_ascii=False).encode()))
        self.assertIsNotNone(self.store.project_body(row))
        self.http.root['active']['old'] = row
        self.request()
        self.worker.process_pending()
        self.assertEqual(self.http.root['_control']['activeIds'], {})
        archived = next(iter(self.http.root['archive'].values()))
        self.assertLessEqual(len(json.dumps(archived, ensure_ascii=False).encode()), 4096)

    def test_malformed_request_cannot_stall_valid_queue_or_latch_dispatch(self):
        self.request('malformed')
        self.http.operations['malformed']['requestedAt'] = {'unexpected': 'shape'}
        self.worker.process_pending()
        self.assertEqual(self.http.operations['malformed']['status'], 'failed')
        self.assertFalse(self.barrier.pending)
        self.assertEqual(self.http.root['_control']['generation'], INITIAL_GENERATION)
        self.request('malformed2')
        self.http.operations['malformed2']['requestedAt'] = 'not-a-timestamp'
        self.request('valid')
        self.worker.process_pending()
        self.assertEqual(self.http.operations['valid']['status'], 'completed')
        self.assertEqual(self.http.operations['malformed2']['status'], 'failed')
        self.assertFalse(self.barrier.pending)

    def test_foreign_active_manifest_does_not_disable_coalescing(self):
        self.http.root['active']['user'] = {**body(), 'source': 'user'}
        self.http.root['_control']['activeIds']['user'] = True
        self.request('reset1')
        self.request('reset2')
        self.worker.on_reset = lambda control: None
        self.worker.process_pending()
        self.assertEqual(self.http.root['_control']['generation'], 1)
        self.assertEqual(self.http.root['_control']['activeIds'], {'user': True})
        self.assertTrue(self.http.operations['reset2']['coalesced'])

    def test_equal_timestamp_new_request_without_published_body_is_not_coalesced(self):
        self.request()
        self.worker.process_pending()
        self.assertEqual(self.http.root['_control']['activeIds'], {})
        self.request('reset2')
        self.worker.on_reset = lambda control: None
        self.worker.process_pending()
        # No active body is needed: generation1 extraction may still be in
        # flight, so this new request must fence it with another generation.
        self.assertEqual(self.http.root['_control']['generation'], 2)
        self.assertFalse(self.http.operations['reset2']['coalesced'])


if __name__ == '__main__':
    unittest.main()
