"""Offline checks at the existing scheduler, event, and display boundaries."""

from datetime import datetime
import io
from contextlib import redirect_stdout
import sys
import types
import unittest
from unittest.mock import Mock, patch

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'raspberry-pi-voice-hub'))

from test_main_regression import load_main
from display_state import DisplayState


class MedicationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.hub = load_main()
        self.output = io.StringIO()
        quiet = redirect_stdout(self.output)
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def test_event_builder_keeps_legacy_schema_and_writer(self):
        with patch.object(self.hub, 'now_ms', return_value=1000), \
                patch.object(self.hub, 'firebase_patch_and_verify', return_value=True) as write:
            self.assertTrue(self.hub.write_medication_event())
        event_id, event = write.call_args.args
        self.assertEqual(event_id, 'pi_medication_reminder_sent_1000')
        self.assertEqual(event, {
            'deviceId': self.hub.DEVICE_ID, 'elderId': self.hub.ELDER_ID,
            'type': 'medication_reminder_sent', 'severity': 'info',
            'title': self.hub.TITLE_MEDICATION, 'message': self.hub.MESSAGE_MEDICATION,
            'status': 'open', 'createdAt': 1000, 'location': self.hub.LOCATION_NAME,
            'runId': 'pi_medication_run_1000',
        })

    def test_voice_schedule_passes_original_slot_once_and_resets_next_day(self):
        dates = [datetime(2026, 9, 15, 8, 0), datetime(2026, 9, 15, 8, 0, 5),
                 datetime(2026, 9, 15, 8, 1), datetime(2026, 9, 16, 8, 0)]
        care = Mock()
        triggered, day = {}, '2026-09-15'
        with patch.object(self.hub, 'datetime') as clock, \
                patch.object(self.hub, 'trigger_medication_reminder') as legacy:
            clock.now.side_effect = dates
            for _ in dates:
                day = self.hub.check_medication_schedule_once(['08:00'], triggered, day, care)
        self.assertEqual([c.args for c in care.run_scheduled.call_args_list],
                         [('2026-09-15_08:00',), ('2026-09-16_08:00',)])
        legacy.assert_not_called()
        self.assertEqual(triggered, {'2026-09-16_08:00': True})

    def test_confirmation_is_opt_in_and_does_not_import_voice_when_disabled(self):
        with patch.dict('os.environ', {}, clear=True), \
                patch('builtins.__import__', side_effect=AssertionError('unexpected import')):
            self.assertIsNone(self.hub.start_medication_care(Mock()))

    def test_requested_confirmation_without_ai_keeps_legacy_reminders(self):
        with patch.dict('os.environ', {'MEDICATION_VOICE_CONFIRMATION_ENABLED': 'true'}):
            self.assertIsNone(self.hub.start_medication_care(None))
        self.assertIn('announcement-only', self.output.getvalue())

    def test_factory_receives_existing_event_builder_and_voice_adapters(self):
        voice = types.ModuleType('voice_ai')
        voice.create_medication_care = Mock()
        active = Mock()
        with patch.dict(sys.modules, {'voice_ai': voice}), \
                patch.dict('os.environ', {'MEDICATION_VOICE_CONFIRMATION_ENABLED': 'true'}):
            care = self.hub.start_medication_care(active)
        voice.create_medication_care.assert_called_once_with(
            self.hub.FIREBASE_DATABASE_URL, self.hub.ELDER_ID, self.hub.DEVICE_ID,
            active.conversation, self.hub.build_medication_event,
        )
        voice.create_medication_care.return_value.start.assert_called_once_with()
        self.assertIs(care, voice.create_medication_care.return_value.start.return_value)

    def test_failed_factory_does_not_leak_exception_details(self):
        voice = types.ModuleType('voice_ai')
        voice.create_medication_care = Mock(side_effect=ValueError('private response'))
        with patch.dict(sys.modules, {'voice_ai': voice}), \
                patch.dict('os.environ', {'MEDICATION_VOICE_CONFIRMATION_ENABLED': 'true'}):
            self.assertIsNone(self.hub.start_medication_care(Mock()))
        self.assertIn('ValueError', self.output.getvalue())
        self.assertNotIn('private response', self.output.getvalue())

    def test_announcement_factory_shares_active_chat_lock_and_keeps_no_ai_mode(self):
        for active in (None, Mock()):
            with self.subTest(ai_enabled=active is not None), \
                    patch('firebase_medication.FirebaseMedicationClient') as client_type, \
                    patch('medication_care.MedicationAnnouncementCare') as care_type:
                self.hub.start_medication_announcement_care(active)
                care_type.assert_called_once_with(
                    client_type.return_value, self.hub.announce_medication_reminder,
                    interaction_lock=(active.conversation.interaction_lock
                                      if active is not None else None),
                )

    def test_hub_passes_chat_to_announcement_fallback(self):
        active = Mock()
        with patch.object(self.hub, 'start_medication_schedule_provider'), \
                patch.object(self.hub, 'start_ai_conversation', return_value=active), \
                patch.object(self.hub, 'start_inactivity_care'), \
                patch.object(self.hub, 'start_medication_care', return_value=None), \
                patch.object(self.hub, 'start_medication_announcement_care') as fallback, \
                patch.object(self.hub, 'start_display'), \
                patch.object(self.hub, 'check_medication_schedule_once'), \
                patch.object(self.hub.time, 'sleep', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.hub.run_hub_mode()
        fallback.assert_called_once_with(active)

    def test_hub_enqueues_at_existing_cadence_and_stops_all_controllers(self):
        active, inactivity, medication, display = Mock(), Mock(), Mock(), Mock()
        with patch.object(self.hub, 'start_ai_conversation', return_value=active), \
                patch.object(self.hub, 'start_inactivity_care', return_value=inactivity), \
                patch.object(self.hub, 'start_medication_care', return_value=medication), \
                patch.object(self.hub, 'start_display', return_value=display), \
                patch.object(self.hub, 'check_medication_schedule_once', return_value='2026-09-15') as schedule, \
                patch.object(self.hub, 'check_inactivity_events_once') as legacy, \
                patch.object(self.hub.time, 'sleep', side_effect=[None, KeyboardInterrupt]) as sleep:
            with self.assertRaises(KeyboardInterrupt):
                self.hub.run_hub_mode()
        medication.retry_pending.assert_not_called()
        self.assertEqual(schedule.call_count, 2)
        self.assertTrue(all(c.args[-1] is medication for c in schedule.call_args_list))
        self.assertEqual([c.args for c in sleep.call_args_list], [(5,), (5,)])
        legacy.assert_not_called()
        for controller in (active, inactivity, medication, display):
            controller.stop.assert_called_once_with()


class MedicationCardTests(unittest.TestCase):
    def test_confirmation_hint_is_scoped_and_inactivity_priority_is_preserved(self):
        state = DisplayState()
        with state.activity('medication'):
            default_message = state.snapshot()['alert']['message']
            with state.activity('medication', message='안내가 끝나면 드셨는지 말씀해 주세요.'):
                self.assertIn('말씀해 주세요', state.snapshot()['alert']['message'])
                with state.activity('inactivity'):
                    self.assertEqual(state.snapshot()['alert']['kind'], 'inactivity')
                self.assertIn('말씀해 주세요', state.snapshot()['alert']['message'])
            self.assertEqual(state.snapshot()['alert']['message'], default_message)
        self.assertEqual(state.snapshot(), {
            'state': 'idle', 'busy': False, 'activity': None, 'alert': None,
            'audio_focus': {
                'blocked': False, 'revision': 2, 'non_game_revision': 3,
            },
        })

    def test_exception_clears_confirmation_hint(self):
        state = DisplayState()
        with self.assertRaises(RuntimeError):
            with state.activity('medication', message='복약 확인'):
                raise RuntimeError('stopped')
        with state.activity('medication'):
            self.assertEqual(state.snapshot()['alert']['message'], '복약 안내를 확인해 주세요.')


if __name__ == '__main__':
    unittest.main()
