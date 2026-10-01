"""V5 deterministic intent/CAS races; all traffic uses an in-memory RTDB."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'raspberry-pi-voice-hub'))
from persistent_memory import MemoryStore
from memory_protocol_fake import ProtocolFirebase
from memory_reset import MemoryResetWorker
from test_memory_reset_worker import Barrier, body


def request(http, identity, device='device_001', intent=None):
    control = http.root.get('_control', {})
    coordinator = control.get('resetCoordinator') or {}
    if intent is None:
        intent = dict(version=1, generation=control.get('generation', 'initial'),
                      epoch=coordinator.get('epoch', 0), operationId=None)
        if coordinator.get('deviceId') == device and coordinator.get('state') != 'completed':
            intent['operationId'] = coordinator.get('activeOperationId')
    http.operations[identity] = dict(status='requested', deviceId=device,
        deviceStatus=device + '|requested', requestedBy='guardian_dashboard',
        requestedAt=int(http.now * 1000), intent=deepcopy(intent))


class ResetCoordinatorV5Tests(unittest.TestCase):
    def setUp(self):
        self.http = ProtocolFirebase({'_control': {'generation': 'initial', 'activeIds': {'old': True}},
                                      'active': {'old': body()}})
        self.store = MemoryStore('https://example.invalid', 'elder_1', self.http)
        self.barrier = Barrier()
        self.worker = MemoryResetWorker(self.store, self.barrier, lambda control: None)

    def save_after_success(self):
        self.assertEqual(self.http.operations['A']['status'], 'completed')
        self.assertTrue(self.store.save([body()], 'new_session', 1))
        return deepcopy(self.http.root['active'])

    def assert_one_reset(self, saved):
        control = self.http.root['_control']
        self.assertEqual(control['generation'], 1)
        self.assertEqual(control['resetCoordinator']['state'], 'completed')
        self.assertEqual(control['resetCoordinator']['activeOperationId'], 'A')
        self.assertEqual(self.http.root['active'], saved)
        for identity in ('A', 'B'):
            operation = self.http.operations[identity]
            self.assertEqual(operation['status'], 'completed')
            self.assertEqual(operation['resultingGeneration'], 1)
        self.assertTrue(self.http.operations['B']['coalesced'])
        self.assertEqual(self.http.operations['B']['coalescedTo'], 'A')
        self.assertFalse(self.barrier.pending)

    def interleave(self, boundary):
        request(self.http, 'A')
        original = self.http.put
        injected = False
        def put(url, **kwargs):
            nonlocal injected
            data = kwargs['json']
            destructive = url.endswith('/_control.json') and data.get('generation') == 1 and 'pendingReset' not in data
            if destructive and not injected:
                injected = True
                if boundary == 'before':
                    request(self.http, 'B')
                if boundary == 'during':
                    # Enter RTDB's serialized CAS, then create B before its
                    # ETag comparison/assignment. Operation and control paths
                    # remain independent atomic domains in this double.
                    self.http.before_put = lambda http: request(http, 'B')
                result = original(url, **kwargs)
                if boundary == 'after':
                    request(self.http, 'B')
                return result
            return original(url, **kwargs)
        self.http.put = put
        self.worker.process_pending()
        self.http.put = original
        self.assertTrue(injected)
        saved = self.save_after_success()
        self.worker.process_pending()
        self.assert_one_reset(saved)

    def test_a_intent_created_immediately_before_destructive_cas(self):
        self.interleave('before')

    def test_a_intent_created_immediately_after_destructive_cas_before_completion(self):
        self.interleave('after')

    def test_a_intent_created_inside_destructive_cas_transport(self):
        self.interleave('during')

    def test_a_intent_snapshot_before_cas_arrives_after_success(self):
        request(self.http, 'A')
        original = self.http.put
        intent = None
        def put(url, **kwargs):
            nonlocal intent
            if url.endswith('/_control.json') and kwargs['json'].get('generation') == 1 and intent is None:
                request(self.http, 'B')
                intent = self.http.operations.pop('B')['intent']
            return original(url, **kwargs)
        self.http.put = put
        self.worker.process_pending()
        self.http.put = original
        saved = self.save_after_success()
        self.http.now += 10
        request(self.http, 'B', intent=intent)
        self.worker.process_pending()
        self.assert_one_reset(saved)

    def test_b_simultaneous_preclaim_intents(self):
        request(self.http, 'A')
        request(self.http, 'B')
        self.worker.process_pending()
        self.assert_one_reset({})

    def test_c_new_intent_after_completion_resets_even_same_millisecond(self):
        request(self.http, 'A')
        self.worker.process_pending()
        self.save_after_success()
        request(self.http, 'C')
        self.worker.process_pending()
        self.assertEqual(self.http.root['_control']['generation'], 2)
        self.assertEqual(self.http.root['_control']['resetCoordinator']['epoch'], 2)
        self.assertEqual(self.http.root['_control']['activeIds'], {})
        self.assertFalse(self.http.operations['C']['coalesced'])

    def test_c_new_intent_after_success_with_coordinator_finish_unacknowledged(self):
        request(self.http, 'A')
        original = self.http.put
        def fail_finish(url, **kwargs):
            if (kwargs['json'].get('resetCoordinator') or {}).get('state') == 'completed':
                raise TimeoutError('finish CAS has not reached server')
            return original(url, **kwargs)
        self.http.put = fail_finish
        self.worker.process_pending()
        self.assertEqual(self.http.root['_control']['resetCoordinator']['state'], 'committed')
        self.save_after_success()
        self.http.now += 60
        # Guardian preparation resolves the completed permanent leader before
        # allowing a new final confirmation, even while coordinator says committed.
        request(self.http, 'C', intent=dict(version=1, generation=1, epoch=1, operationId=None))
        self.http.put = original
        self.worker.process_pending()
        self.assertEqual(self.http.root['_control']['generation'], 2)
        self.assertEqual(self.http.root['_control']['activeIds'], {})
        self.assertEqual(self.http.operations['C']['status'], 'completed')
        self.assertFalse(self.http.operations['C']['coalesced'])

    def test_d_restart_after_claim_commit_and_completion_ack_loss(self):
        for boundary in ('claim', 'commit', 'completion', 'finish'):
            with self.subTest(boundary=boundary):
                self.setUp()
                request(self.http, 'A')
                original = self.http.put
                failed = False
                def put(url, **kwargs):
                    nonlocal failed
                    result = original(url, **kwargs)
                    data = kwargs['json']
                    match = ((boundary == 'claim' and data.get('pendingReset')) or
                             (boundary == 'commit' and data.get('generation') == 1 and '/_control.' in url) or
                             (boundary == 'completion' and data.get('status') == 'completed') or
                             (boundary == 'finish' and (data.get('resetCoordinator') or {}).get('state') == 'completed'))
                    if match and not failed:
                        failed = True
                        if boundary == 'finish':
                            request(self.http, 'B', intent=dict(version=1, generation=1, operationId='A', epoch=1))
                        else:
                            request(self.http, 'B')
                        raise TimeoutError('applied; process dies before ACK')
                    return result
                self.http.put = put
                self.worker.process_pending()
                self.assertTrue(failed)
                self.http.put = original
                restarted = MemoryResetWorker(self.store, self.barrier, lambda control: None)
                restarted.process_pending()
                restarted.process_pending()
                # Lost commit ACK can leave an invisible orphan; only manifest
                # visibility is authoritative, and restart must not reset again.
                self.assert_one_reset(deepcopy(self.http.root.get('active', {})))
                self.assertEqual(self.http.root['_control']['activeIds'], {})

    def test_old_device_cannot_starve_current_device(self):
        for index in range(70):
            request(self.http, 'a_old_' + str(index).zfill(3), device='old_device')
        request(self.http, 'z_current')
        self.worker.process_pending()
        self.assertEqual(self.http.operations['z_current']['status'], 'completed')
        self.assertTrue(all(row['status'] == 'requested' for key, row in self.http.operations.items() if key.startswith('a_old')))
        self.assertTrue(all(int(kwargs['params']['limitToFirst']) <= 33
                            for _, kwargs in self.http.get_calls if 'params' in kwargs))

    def test_legacy_old_device_page_advances_to_current_unindexed_request(self):
        for index in range(70):
            request(self.http, 'a_old_' + str(index).zfill(3), device='old_device')
        request(self.http, 'z_current')
        for row in self.http.operations.values():
            row.pop('deviceStatus')
            row.pop('intent')
        for _ in range(3):
            self.worker.process_pending()
        self.assertEqual(self.http.operations['z_current']['status'], 'completed')
        self.assertEqual(self.http.root['_control']['generation'], 1)

    def test_malformed_intents_leave_index_and_cannot_starve_current_request(self):
        for index in range(32):
            request(self.http, 'bad_' + str(index).zfill(3), intent={'version': 1})
        request(self.http, 'z_valid')
        self.worker.process_pending()
        self.assertFalse(self.barrier.pending)
        self.worker.process_pending()
        self.assertEqual(self.http.operations['z_valid']['status'], 'completed')
        for key, row in self.http.operations.items():
            if key.startswith('bad_'):
                self.assertEqual((row['status'], row['deviceStatus']), ('failed', 'device_001|failed'))

    def test_delayed_bound_intent_uses_permanent_leader_after_receipt_eviction(self):
        request(self.http, 'A')
        self.worker.process_pending()
        delayed = dict(version=1, generation='initial', operationId='A', epoch=1)
        for index in range(33):
            request(self.http, 'later_' + str(index))
            self.worker.process_pending()
        generation = self.http.root['_control']['generation']
        self.assertNotIn('A', self.http.root['_control']['resetOperations'])
        self.assertTrue(self.store.save([body()], 'latest_session', generation))
        saved = deepcopy(self.http.root['active'])
        request(self.http, 'B', intent=delayed)
        self.worker.process_pending()
        self.assertEqual(self.http.root['_control']['generation'], generation)
        self.assertEqual(self.http.root['active'], saved)
        self.assertEqual(self.http.operations['B']['resultingGeneration'], 1)
        self.assertEqual(self.http.operations['B']['coalescedTo'], 'A')

    def test_delayed_preclaim_intent_keeps_first_epoch_result_after_later_resets(self):
        original_intent = dict(version=1, generation='initial', operationId=None, epoch=0)
        request(self.http, 'A')
        self.worker.process_pending()
        for index in range(33):
            request(self.http, 'later_' + str(index))
            self.worker.process_pending()
        generation = self.http.root['_control']['generation']
        self.assertNotIn('A', self.http.root['_control']['resetOperations'])
        self.assertTrue(self.store.save([body()], 'latest_session', generation))
        saved = deepcopy(self.http.root['active'])
        request(self.http, 'B', intent=original_intent)
        self.worker.process_pending()
        self.assertEqual(self.http.root['_control']['generation'], generation)
        self.assertEqual(self.http.root['active'], saved)
        self.assertEqual(self.http.operations['B']['resultingGeneration'], 1)
        self.assertEqual(self.http.operations['B']['coalescedTo'], 'A')

    def test_nonconversation_manifest_survives_reset_then_conversation_save(self):
        protected = {**body(), 'source': 'user'}
        self.http.root['active']['user'] = protected
        self.http.root['_control']['activeIds']['user'] = True
        request(self.http, 'A')
        self.worker.process_pending()
        self.save_after_success()
        self.assertTrue(self.http.root['_control']['activeIds']['user'])
        self.assertEqual(self.http.root['active']['user'], protected)

    def test_control_claim_conflict_retries_without_duplicate_generation(self):
        request(self.http, 'A')
        original = self.http.put
        conflicted = False
        def put(url, **kwargs):
            nonlocal conflicted
            if kwargs['json'].get('pendingReset') and not conflicted:
                conflicted = True
                self.http.root['_control']['maintenanceRevision'] = 'concurrent-writer'
                request(self.http, 'B')
            return original(url, **kwargs)
        self.http.put = put
        self.worker.process_pending()
        self.http.put = original
        self.worker.process_pending()
        self.assertTrue(conflicted)
        self.assert_one_reset({})


if __name__ == '__main__':
    unittest.main()
