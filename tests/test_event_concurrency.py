"""Three real care/chat workers with deterministic, entirely local I/O edges."""

from contextlib import redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
import queue
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "raspberry-pi-voice-hub"))
import voice_ai
from display_state import DisplayState
from inactivity_care import InactivityCareController
from medication_care import MedicationCare


class EventConcurrencyTests(unittest.TestCase):
    def run_three_workers(self, stop_waiting):
        state = DisplayState()
        audio_lock = threading.Lock()
        interaction_lock = threading.RLock()
        waiting = {name: threading.Event() for name in ("medication-care", "inactivity-care")}
        gates = {name: threading.Event() for name in ("chat", "medication", "inactivity")}
        done = {name: threading.Event() for name in gates}
        entered = queue.Queue()
        errors, outcomes, operations = [], {}, []
        active_audio = 0
        max_audio = 0
        observation_lock = threading.Lock()

        class ObservedInteraction:
            def acquire(self, timeout):
                request = waiting.get(threading.current_thread().name)
                if request is not None:
                    request.set()
                return interaction_lock.acquire(timeout=timeout)

            def release(self):
                interaction_lock.release()

        shared_interaction = ObservedInteraction()

        class LocalCareClient:
            path = "local/events"
            poll_seconds = .01

            def __init__(self, name):
                self.name = name
                self.started = False
                self.validations = []

            def invoke(self, callback):
                try:
                    outcomes[self.name] = callback(self.name + "_event")
                except voice_ai.ConversationStopped:
                    outcomes[self.name] = "stopped"
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    done[self.name].set()
                return True

            def run_once(self, callback):
                if self.started:
                    return False
                self.started = True
                return self.invoke(callback)

            def run_scheduled(self, _slot, _event, callback):
                return self.invoke(callback)

            def validate_before_audio(self, event_id):
                self.validations.append(event_id)
                return True

            def retry_pending(self):
                return False

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(voice_ai, "DISPLAY", state), \
                redirect_stdout(io.StringIO()):
            api = Mock()
            settings = replace(voice_ai.VoiceSettings(), session_max_no_speech=1)
            voices = {}
            for name in gates:
                voice = voice_ai.VoiceConversation(api, settings, Mock(), audio_lock, directory)
                voice.interaction_lock = shared_interaction

                def synthesize(_text, path, label=name):
                    operations.append((label, "tts"))
                    path.write_bytes(b"local mocked audio")

                def playback(_command, _timeout, label=name):
                    nonlocal active_audio, max_audio
                    self.assertTrue(audio_lock.locked())
                    with observation_lock:
                        active_audio += 1
                        max_audio = max(max_audio, active_audio)
                    entered.put(label)
                    try:
                        self.assertTrue(gates[label].wait(2), "test did not release " + label)
                    finally:
                        with observation_lock:
                            active_audio -= 1

                def capture(_path, label=name):
                    self.assertTrue(audio_lock.locked())
                    operations.append((label, "capture"))
                    return False

                voice.synthesize_speech = Mock(side_effect=synthesize)
                voice.run_audio = Mock(side_effect=playback)
                voice.capture_utterance = Mock(side_effect=capture)
                voices[name] = voice

            clients = {name: LocalCareClient(name) for name in ("medication", "inactivity")}
            medication = MedicationCare(clients["medication"], voices["medication"], Mock())
            inactivity = InactivityCareController(clients["inactivity"], voices["inactivity"])

            def run_chat():
                try:
                    outcomes["chat"] = voices["chat"].run_session()
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    done["chat"].set()

            chat = threading.Thread(target=run_chat, name="chat")
            try:
                chat.start()
                self.assertEqual(entered.get(timeout=2), "chat")
                medication.start()
                medication.run_scheduled("2026-09-20_08:00", {})
                inactivity.start()
                for request in waiting.values():
                    self.assertTrue(request.wait(2), "care did not reach the shared interaction")
                self.assertEqual(operations, [("chat", "tts")])
                self.assertEqual(state.snapshot()["activity"], "conversation")
                self.assertTrue(state.snapshot()["audio_focus"]["blocked"])
                self.assertFalse(state.request_radio(True))

                if stop_waiting:
                    # Both queued workers must stop while chat still owns its
                    # interaction, without validating, generating, or playing.
                    medication.stop()
                    inactivity.stop()
                    self.assertFalse(medication._thread.is_alive())
                    self.assertFalse(inactivity._thread.is_alive())
                    self.assertEqual(outcomes["medication"], "stopped")
                    self.assertEqual(outcomes["inactivity"], "stopped")
                    for client in clients.values():
                        self.assertEqual(client.validations, [])
                    self.assertEqual(operations, [("chat", "tts")])
                    gates["chat"].set()
                else:
                    gates["chat"].set()
                    observed_care = set()
                    for _ in range(2):
                        name = entered.get(timeout=2)
                        self.assertIn(name, {"medication", "inactivity"} - observed_care)
                        observed_care.add(name)
                        snapshot = state.snapshot()
                        self.assertEqual(snapshot["activity"], name)
                        self.assertEqual(snapshot["alert"]["kind"], name)
                        self.assertTrue(entered.empty(), "another worker entered physical audio")
                        self.assertEqual(active_audio, 1)
                        gates[name].set()
                    self.assertEqual(observed_care, {"medication", "inactivity"})

                for complete in done.values():
                    self.assertTrue(complete.wait(2))
            finally:
                for gate in gates.values():
                    gate.set()
                voices["chat"].stop_event.set()
                medication.stop()
                inactivity.stop()
                chat.join(2)

            self.assertFalse(chat.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(max_audio, 1)
            self.assertEqual(active_audio, 0)
            self.assertFalse(audio_lock.locked())
            snapshot = state.snapshot()
            self.assertEqual(snapshot["state"], "idle")
            self.assertFalse(snapshot["busy"])
            self.assertIsNone(snapshot["activity"])
            self.assertIsNone(snapshot["alert"])
            self.assertFalse(snapshot["audio_focus"]["blocked"])
            self.assertEqual(list(Path(directory).iterdir()), [])
            api.audio.transcriptions.create.assert_not_called()
            api.audio.speech.with_streaming_response.create.assert_not_called()
            api.responses.create.assert_not_called()
            if not stop_waiting:
                self.assertEqual(outcomes, {"chat": False, "medication": "no_speech", "inactivity": "no_speech"})
                for name in clients:
                    self.assertEqual(clients[name].validations, [name + "_event"])

    def test_chat_medication_and_inactivity_serialize_audio_and_restore_idle(self):
        self.run_three_workers(stop_waiting=False)

    def test_waiting_care_workers_stop_without_interrupting_active_chat(self):
        self.run_three_workers(stop_waiting=True)


if __name__ == "__main__":
    unittest.main()
