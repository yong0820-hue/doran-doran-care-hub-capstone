"""Local reset epochs fence cold sessions independently of Pi/server clocks."""

from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'raspberry-pi-voice-hub'))
from persistent_memory import INITIAL_GENERATION, MAX_PENDING_EXTRACTIONS, MemoryStore, PersistentMemoryService
from memory_protocol_fake import ProtocolFirebase


class MemoryResetEpochTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.http = ProtocolFirebase({'_control': {'generation': INITIAL_GENERATION, 'activeIds': {}}})
        self.started = int(self.http.now * 1000) + 60000  # Pi is ahead of Firebase.
        self.messages = [{'text': '요가 좋아해.', 'createdAt': self.started}]
        self.extractor = Mock()
        self.extractor.extract.return_value = [{'type': 'preference', 'subject': '요가', 'content': '요가 좋아해.',
                                                'lastMentionedAt': self.started, 'importance': 3, 'tags': []}]

    def service(self):
        service = PersistentMemoryService(MemoryStore('https://example.invalid', 'elder_1', self.http),
                                          self.extractor, outbox_dir=self.directory.name)
        service.prefetch = Mock()  # Deterministic cold cache; worker uses actual control reads.
        self.addCleanup(lambda: service.stop(timeout=1))
        return service

    def reset(self, service):
        self.http.reset()
        service._apply_memory_reset(self.http.root['_control'])

    def drain(self, service):
        service.start()
        self.assertTrue(service.stop(timeout=1))

    def jobs(self):
        return list(Path(self.directory.name).rglob('*.json'))

    def test_ahead_clock_waiting_job_is_discarded_after_reset(self):
        service = self.service()
        self.assertTrue(service.submit('old_session', self.messages, None, session_started_at=self.started))
        self.reset(service)
        self.drain(service)
        self.extractor.extract.assert_not_called()
        self.assertEqual(self.http.root['_control']['activeIds'], {})
        self.assertEqual(self.jobs(), [])

    def test_live_cold_session_and_late_submission_are_invalidated_without_clock_comparison(self):
        service = self.service()
        self.assertEqual(service.session_status(self.started)['state'], 'WAITING')
        self.reset(service)
        self.assertEqual(service.session_status(self.started)['state'], 'INVALIDATED')
        self.assertFalse(service.submit('late_old', self.messages, None, session_started_at=self.started))
        self.assertEqual(self.jobs(), [])

    def test_durable_epoch_survives_restart_and_removes_old_waiting_job(self):
        old = self.service()
        self.assertTrue(old.submit('old_session', self.messages, None, session_started_at=self.started))
        self.reset(old)
        self.assertTrue((old._outbox_dir / 'reset-epoch.state').is_file())
        restarted = self.service()
        self.drain(restarted)
        self.extractor.extract.assert_not_called()
        self.assertEqual(self.jobs(), [])

    def test_repeated_callback_preserves_new_waiting_job_across_restart(self):
        service = self.service()
        self.reset(service)
        self.assertTrue(service.submit('new_session', self.messages, None, session_started_at=self.started))
        with patch('persistent_memory.os.replace', side_effect=AssertionError('epoch should be idempotent')):
            service._apply_memory_reset(self.http.root['_control'])
        self.drain(self.service())
        self.extractor.extract.assert_called_once()
        self.assertEqual(len(self.http.root['_control']['activeIds']), 1)
        self.assertEqual(self.jobs(), [])

    def test_registered_postreset_session_survives_pi_clock_behind_server(self):
        service = self.service()
        self.reset(service)
        started = int(self.http.now * 1000) - 60000
        self.assertTrue(service.register_session(started))
        self.assertEqual(service.session_status(started)['state'], 'READY')

    def test_unregistered_legacy_session_cannot_revive_after_timestamp_invalidation(self):
        service = self.service()
        self.reset(service)
        started = int(self.http.now * 1000) - 60000
        self.assertEqual(service.session_status(started)['state'], 'INVALIDATED')
        self.assertEqual(service.session_status(started)['state'], 'INVALIDATED')
        self.assertFalse(service.submit('legacy_old', self.messages, None, session_started_at=started))

    def test_nonowner_cannot_reconcile_epoch_or_expose_session_generation(self):
        service = self.service()
        self.reset(service)
        service._runtime_owner = Mock(held=False)
        with patch.object(service, '_reconcile_reset_epoch', side_effect=AssertionError('nonowner reconciled')):
            self.assertEqual(service.session_status(self.started)['state'], 'WAITING')
            self.assertIsNone(service.begin_session())

    def test_owner_handoff_reloads_epoch_written_since_instance_construction(self):
        stale_instance = self.service()
        owner = self.service()
        self.reset(owner)
        self.assertTrue(owner.submit('new_session', self.messages, None, session_started_at=self.started))
        stale_instance._runtime_owner = Mock(held=True)
        stale_instance._runtime_owner.acquire.return_value = True
        # Ownership is held only for this start; normal shutdown test adapter.
        stale_instance.start()
        self.assertTrue(stale_instance.wait_idle(1))
        stale_instance._runtime_owner = None
        self.assertTrue(stale_instance.stop(timeout=1))
        self.extractor.extract.assert_called_once()

    def test_registry_is_bounded_and_evicted_old_session_cannot_rebind_after_reset(self):
        service = self.service()
        for offset in range(MAX_PENDING_EXTRACTIONS + 1):
            self.assertTrue(service.register_session(self.started + offset))
        self.assertEqual(len(service._session_epochs), MAX_PENDING_EXTRACTIONS)
        self.reset(service)
        self.assertFalse(service.register_session(self.started))
        self.assertFalse(service.submit('evicted_old', self.messages, None, session_started_at=self.started))

    def test_inflight_old_waiting_job_is_removed_when_reset_fences_its_save(self):
        service = self.service()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        rows = self.extractor.extract.return_value
        def extract(_messages):
            entered.set()
            self.assertTrue(release.wait(2))
            return rows
        self.extractor.extract.side_effect = extract
        service.submit('inflight_old', self.messages, None, session_started_at=self.started)
        service.start()
        self.assertTrue(entered.wait(1))
        self.reset(service)
        release.set()
        self.assertTrue(service.stop(timeout=1))
        self.assertEqual(self.jobs(), [])
        self.assertEqual(self.http.root['_control']['activeIds'], {})

    def test_control_reset_publication_before_callback_still_fences_waiting_job(self):
        service = self.service()
        self.assertTrue(service.submit('old_session', self.messages, None, session_started_at=self.started))
        self.http.reset()  # Worker has committed control, but local callback has not run yet.
        self.drain(service)
        self.extractor.extract.assert_not_called()
        self.assertEqual(self.jobs(), [])

    def test_failed_epoch_persistence_prevents_reset_callback_success(self):
        service = self.service()
        self.http.reset()
        with patch('persistent_memory.os.replace', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):
                service._apply_memory_reset(self.http.root['_control'])

    def test_legacy_waiting_job_without_epoch_recovers_before_any_reset(self):
        service = self.service()
        service._persist(('legacy', self.messages, {'waitingSince': self.started}))
        self.drain(self.service())
        self.extractor.extract.assert_called_once()
        self.assertEqual(self.jobs(), [])

    def test_legacy_waiting_job_without_epoch_is_discarded_after_reset(self):
        service = self.service()
        service._persist(('legacy', self.messages, {'waitingSince': self.started}))
        self.reset(service)
        self.drain(self.service())
        self.extractor.extract.assert_not_called()
        self.assertEqual(self.jobs(), [])


if __name__ == '__main__':
    unittest.main()
