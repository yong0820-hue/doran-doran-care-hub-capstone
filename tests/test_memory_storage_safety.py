"""Storage safety under ambiguous publication, malformed data and maintenance."""
from copy import deepcopy
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'raspberry-pi-voice-hub'))
from persistent_memory import INITIAL_GENERATION, MemoryStore
from memory_protocol_fake import ProtocolFirebase


def candidate(**changes):
    return dict(type='preference', subject='요가', content='요가 좋아해.',
                lastMentionedAt=int(time.time() * 1000), importance=3, tags=[], **changes)


class MemoryStorageSafetyTests(unittest.TestCase):
    def setUp(self):
        self.http = ProtocolFirebase({'_control': {'generation': INITIAL_GENERATION}})
        self.store = MemoryStore('https://example.invalid', 'elder_1', self.http)

    def test_ambiguous_publication_never_deletes_staged_bodies(self):
        original = self.http.put
        deferred = []
        def delayed(url, **kwargs):
            if url.endswith('/_control.json'):
                deferred.append((url, deepcopy(kwargs)))
                raise TimeoutError('request may still arrive')
            return original(url, **kwargs)
        self.http.put = delayed
        self.assertFalse(self.store.save([candidate()], 'session', INITIAL_GENERATION))
        self.assertTrue(deferred)
        url, kwargs = deferred[0]
        # Deliver the timed-out control request after the save/read-back ended.
        self.assertEqual(original(url, **kwargs).status_code, 200)
        for identity in self.http.root['_control']['activeIds']:
            self.assertIn(identity, self.http.root['active'])
        self.assertEqual(self.http.patch_calls, [])

    def test_control_entry_values_and_total_bytes_fail_closed(self):
        controls = [
            {'activeIds': {'id': {'payload': 'invalid'}}},
            {'activeIds': []}, {'archiveIds': {'id': float('inf')}},
            {'leases': {'id': {'generation': 'initial', 'expiresAt': True}}},
            {'leases': {'id': {'generation': 'initial', 'expiresAt': float('nan')}}},
            {'pendingReset': {'operationId': 'valid', 'createdAt': 'invalid'}},
            {'resetCoveredIds': {'operation': 'true'}},
            {'resetCoveredIds': {str(index): True for index in range(65)}},
            {'extra': 'x' * 65537},
        ]
        for changes in controls:
            with self.subTest(changes=list(changes)):
                self.http.root['_control'] = {'generation': INITIAL_GENERATION, **changes}
                with self.assertRaises(ValueError):
                    self.store.control_read()

    def test_invalid_candidate_shapes_do_not_publish(self):
        for field, value in [('content', 'x' * 281), ('subject', 'x' * 61),
                             ('tags', ['x'] * 7), ('tags', ['x' * 31]), ('tags', 'text')]:
            with self.subTest(field=field, value_length=len(value)):
                row = candidate()
                row[field] = value
                self.assertFalse(self.store.save([row], 'session', INITIAL_GENERATION))
        self.assertEqual(self.http.put_calls, [])

    def test_candidate_and_body_projection_do_not_copy_unknown_payload(self):
        row = candidate()
        row['transcript'] = 'private' * 30000
        self.assertTrue(self.store.save([row], 'session', INITIAL_GENERATION))
        identity = next(iter(self.http.root['_control']['activeIds']))
        self.assertNotIn('transcript', self.http.root['active'][identity])
        self.http.root['active'][identity]['unknownMetadata'] = 'irrelevant'
        self.assertNotIn('unknownMetadata', self.store.active_read(self.http.root['_control'])[identity])

    def test_copy_on_write_changes_storage_key_but_preserves_logical_identity(self):
        first = candidate()
        self.assertTrue(self.store.save([first], 'session', INITIAL_GENERATION))
        old_key = next(iter(self.http.root['_control']['activeIds']))
        logical_id = self.http.root['active'][old_key]['id']
        updated = {**first, 'content': '요가 싫어해.', 'lastMentionedAt': first['lastMentionedAt'] + 1}
        self.assertTrue(self.store.save([updated], 'session2', INITIAL_GENERATION))
        new_key = next(iter(self.http.root['_control']['activeIds']))
        self.assertNotEqual(new_key, old_key)
        self.assertEqual(self.http.root['active'][new_key]['id'], logical_id)

    def test_delayed_immutable_put_is_invisible_and_explicitly_maintainable(self):
        original = self.http.put
        deferred = []
        def delayed(url, **kwargs):
            if '/active/' in url:
                deferred.append((url, deepcopy(kwargs)))
                raise TimeoutError('body is still in flight')
            return original(url, **kwargs)
        self.http.put = delayed
        self.assertFalse(self.store.save([candidate()], 'session', INITIAL_GENERATION))
        for url, kwargs in deferred:
            original(url, **kwargs)
        self.assertEqual(self.http.root['_control'].get('activeIds', {}), {})
        self.assertEqual(self.store.active_read(self.http.root['_control']), {})
        self.assertTrue(self.store.maintain_orphans(list(self.http.root['active']), []))
        self.assertEqual(self.http.root['active'], {})

    def test_maintenance_deletes_only_known_detached_leaves_after_cas_fence(self):
        self.http.root.update(active={'live': {}, 'orphan': {}}, archive={'kept': {}, 'old': {}})
        self.http.root['_control'].update(activeIds={'live': True}, archiveIds={'kept': 1})
        self.assertTrue(self.store.maintain_orphans(['live', 'orphan', 'kept'], ['old', 'live', 'kept']))
        self.assertIn('live', self.http.root['active'])
        self.assertIn('kept', self.http.root['archive'])
        self.assertNotIn('orphan', self.http.root['active'])
        self.assertNotIn('old', self.http.root['archive'])
        self.assertTrue(self.http.root['_control'].get('maintenanceRevision'))
        self.assertFalse(any(url == self.store.url for url, _ in self.http.get_calls))
        self.assertFalse(any(url == self.store.url for url, *_ in self.http.put_calls))

    def test_maintenance_fence_rejects_a_delayed_old_publication(self):
        self.http.root['active'] = {'orphan': {}}
        control, etag, _, _ = self.store.control_read()
        self.assertTrue(self.store.maintain_orphans(['orphan'], []))
        self.assertFalse(self.store.control_put({**control, 'activeIds': {'orphan': True}}, etag))

    def test_maintenance_retries_conflict_and_preserves_newly_published_body(self):
        self.http.root['active'] = {'orphan': {}}
        def publish(http):
            http.root['_control']['activeIds'] = {'orphan': True}
            http.version += 1
        self.http.before_put = publish
        self.assertTrue(self.store.maintain_orphans(['orphan'], []))
        self.assertIn('orphan', self.http.root['active'])

    def test_maintenance_aborts_for_pending_reset_or_ambiguous_fence(self):
        self.http.root['active'] = {'orphan': {}}
        self.http.root['_control']['pendingReset'] = {'operationId': 'reset', 'createdAt': 1}
        self.assertFalse(self.store.maintain_orphans(['orphan'], []))
        self.http.root['_control'].pop('pendingReset')
        self.http.write_failure = True
        self.assertFalse(self.store.maintain_orphans(['orphan'], []))
        self.assertEqual(self.http.patch_calls, [])

    def test_maintenance_input_is_bounded_and_path_safe(self):
        for active in ([str(i) for i in range(129)], ['a/b'], 'not-a-list'):
            with self.subTest(active_type=type(active).__name__):
                with self.assertRaises(ValueError):
                    self.store.maintain_orphans(active, [])
        self.assertEqual(self.http.get_calls, [])


if __name__ == '__main__':
    unittest.main()
