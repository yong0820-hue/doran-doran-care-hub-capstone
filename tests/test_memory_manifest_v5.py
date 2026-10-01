"""Conversation publication preserves manifest entries it does not own."""
from copy import deepcopy
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'raspberry-pi-voice-hub'))
from memory_protocol_fake import ProtocolFirebase
from memory_storage import MAX_RECORD_BYTES
from persistent_memory import INITIAL_GENERATION, MemoryStore


def row(identity):
    return dict(id=identity, elderId='elder_1', source='conversation', type='preference',
                subject='요가', content='요가 좋아해.', status='active', importance=3,
                lastMentionedAt=int(time.time() * 1000))


class ManifestPreservationTests(unittest.TestCase):
    def setUp(self):
        self.http = ProtocolFirebase({'_control': {'generation': INITIAL_GENERATION}})
        self.store = MemoryStore('https://example.invalid', 'elder_1', self.http)

    def seed(self, bodies):
        self.http.root['active'] = deepcopy(bodies)
        self.http.root['_control']['activeIds'] = dict.fromkeys(bodies, True)

    def assert_preserved(self, bodies):
        manifest = self.http.root['_control']['activeIds']
        self.assertEqual({identity: manifest.get(identity) for identity in bodies},
                         dict.fromkeys(bodies, True))
        for identity, body in bodies.items():
            with self.subTest(identity=identity):
                self.assertEqual(self.http.root['active'].get(identity), body)
        self.assertFalse(any(url == self.store.url for url, _ in self.http.get_calls))
        self.assertFalse(any(url == self.store.url for url, *_ in self.http.put_calls))

    def test_nonconversation_and_other_elder_manifest_and_body_survive_save(self):
        bodies = {
            'user': {**row('user'), 'source': 'user'},
            'ai': {**row('ai'), 'source': 'ai'},
            'foreign': {**row('foreign'), 'elderId': 'elder_2'},
        }
        self.seed(bodies)
        self.assertTrue(self.store.save([row('new')], 'session', INITIAL_GENERATION))
        self.assert_preserved(bodies)
        self.assertEqual(len(self.store.active_read(self.http.root['_control'])), 1)

    def test_retry_preserves_foreign_manifest_added_before_publication(self):
        foreign = {**row('foreign'), 'source': 'user'}
        original = self.http.put
        inserted = False

        def concurrent_publication(url, **kwargs):
            nonlocal inserted
            if url.endswith('/_control.json') and not inserted:
                inserted = True
                self.http.root.setdefault('active', {})['foreign'] = deepcopy(foreign)
                self.http.root['_control'].setdefault('activeIds', {})['foreign'] = True
            return original(url, **kwargs)

        self.http.put = concurrent_publication
        self.assertTrue(self.store.save([row('new')], 'session', INITIAL_GENERATION))
        self.assertTrue(inserted)
        self.assert_preserved({'foreign': foreign})
        self.assertEqual(len(self.store.active_read(self.http.root['_control'])), 1)

    def test_unreadable_manifest_entry_is_preserved_without_unbounded_body_read(self):
        oversized = {**row('oversized'), 'source': 'user',
                     'unknownMetadata': 'x' * (2 * 1024 * 1024)}
        self.seed({'oversized': oversized})
        self.assertTrue(self.store.save([row('new')], 'session', INITIAL_GENERATION))
        self.assert_preserved({'oversized': oversized})
        self.assertLessEqual(self.http.bytes_streamed, MAX_RECORD_BYTES + 1024)
        body_gets = [(url, kwargs) for url, kwargs in self.http.get_calls if '/active/' in url]
        self.assertEqual(len(body_gets), 1)
        self.assertTrue(body_gets[0][1]['stream'])

    def test_foreign_entries_reserve_manifest_capacity_and_only_owned_rows_are_pruned(self):
        foreign = {f'user_{index}': {**row(f'user_{index}'), 'source': 'user'}
                   for index in range(127)}
        old = {**row('old'), 'lastMentionedAt': 1, 'subject': '오이', 'content': '오이 좋아해.'}
        self.seed({**foreign, 'old': old})
        self.assertTrue(self.store.save([row('new')], 'session', INITIAL_GENERATION))
        self.assert_preserved(foreign)
        self.assertEqual(len(self.http.root['_control']['activeIds']), 128)
        self.assertNotIn('old', self.http.root['_control']['activeIds'])
        self.assertNotIn('old', self.http.root['active'])
        owned = list(self.store.active_read(self.http.root['_control']).values())
        self.assertEqual(len(owned), 1)
        self.assertEqual(owned[0]['subject'], '요가')
        self.assertEqual(owned[0]['content'], '요가 좋아해.')

    def test_foreign_full_manifest_rejects_save_without_deleting_entries(self):
        foreign = {f'user_{index}': {**row(f'user_{index}'), 'source': 'user'}
                   for index in range(128)}
        self.seed(foreign)
        self.assertFalse(self.store.save([row('new')], 'session', INITIAL_GENERATION))
        self.assert_preserved(foreign)
        self.assertEqual(self.http.put_calls, [])
        self.assertEqual(self.http.patch_calls, [])

    def test_invalid_owned_body_can_be_repaired_without_leaving_duplicate_manifest(self):
        self.assertTrue(self.store.save([row('new')], 'session', INITIAL_GENERATION))
        old_key = next(iter(self.http.root['_control']['activeIds']))
        logical_id = self.http.root['active'][old_key]['id']
        self.http.root['active'][old_key].update(lastMentionedAt={'bad': True}, validUntil='bad')
        self.assertTrue(self.store.save([row('new')], 'session', INITIAL_GENERATION))
        self.assertEqual(len(self.http.root['_control']['activeIds']), 1)
        self.assertNotIn(old_key, self.http.root['_control']['activeIds'])
        self.assertNotIn(old_key, self.http.root['active'])
        repaired = list(self.store.active_read(self.http.root['_control']).values())
        self.assertEqual(len(repaired), 1)
        self.assertEqual(repaired[0]['id'], logical_id)
        self.assertIsInstance(repaired[0]['lastMentionedAt'], int)
        self.assertNotIn('validUntil', repaired[0])


if __name__ == '__main__':
    unittest.main()
