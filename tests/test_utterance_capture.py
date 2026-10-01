"""Deterministic streaming microphone tests without ALSA or native WebRTC."""

import io
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import wave
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "raspberry-pi-voice-hub"))
from voice_ai import (ConversationStopped, MIC_FRAME_BYTES, MIC_RATE,
                      VoiceConversation, VoiceSettings)


QUIET = bytes(MIC_FRAME_BYTES)
SPEECH = struct.pack("<hh", 1200, -1200) * (MIC_FRAME_BYTES // 4)
LOW_ENERGY = struct.pack("<hh", 299, -299) * (MIC_FRAME_BYTES // 4)
SOFT_SPEECH = struct.pack("<hh", 200, -200) * (MIC_FRAME_BYTES // 4)
LOW_NOISE = struct.pack("<hh", 199, -199) * (MIC_FRAME_BYTES // 4)


class PCMStream(io.BytesIO):
    def __init__(self, data, chunk_size=None):
        super().__init__(data)
        self.chunk_size = chunk_size
        self.bytes_read = 0

    def read(self, size=-1):
        if self.chunk_size is not None:
            size = min(size, self.chunk_size)
        data = super().read(size)
        self.bytes_read += len(data)
        return data


class UtteranceCaptureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "utterance.wav"
        self.voice = VoiceConversation(Mock(), VoiceSettings(), Mock(),
                                       threading.Lock(), temporary.name)
        self.vad = Mock()
        self.vad.is_speech.return_value = True
        self.process = Mock()
        self.process.poll.return_value = None
        self.process.wait.return_value = 0
        self.install_stream(b"")
        self.popen = self.start_patch(patch("voice_ai.subprocess.Popen",
                                           return_value=self.process))
        self.select = self.start_patch(patch("voice_ai.select.select"))
        self.select.side_effect = lambda readers, *_: (readers, [], [])
        self.clock = self.start_patch(patch("voice_ai.time.monotonic", return_value=100))
        self.new_vad = self.start_patch(patch.object(self.voice, "new_vad",
                                                    return_value=self.vad))
        self.output = io.StringIO()
        quiet = redirect_stdout(self.output)
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def start_patch(self, patcher):
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def install_stream(self, pcm, chunk_size=None):
        self.stream = PCMStream(pcm, chunk_size)
        self.process.stdout = self.stream

    def recorded_pcm(self):
        with wave.open(str(self.path), "rb") as recording:
            self.assertEqual((recording.getnchannels(), recording.getsampwidth(),
                              recording.getframerate()), (1, 2, MIC_RATE))
            return recording.readframes(recording.getnframes())

    def timing_events(self, stage):
        events = [dict(field.split("=", 1) for field in line.split()[2:])
                  for line in self.output.getvalue().splitlines()
                  if line.startswith("[Voice Timing] ")]
        return [event for event in events if event["stage"] == stage]

    def assert_stopped(self):
        self.assertTrue(self.stream.closed)
        self.process.terminate.assert_called_once_with()
        self.process.wait.assert_called_with(timeout=1)

    def test_no_speech_times_out_after_four_seconds_without_wav(self):
        self.install_stream(QUIET * 150)
        self.assertFalse(self.voice.capture_utterance(self.path))
        self.assertEqual(self.stream.bytes_read, 134 * MIC_FRAME_BYTES)
        self.assertFalse(self.path.exists())
        self.assert_stopped()
        self.assertIn("stage=endpoint", self.output.getvalue())
        self.assertIn("reason=no_speech", self.output.getvalue())

    def test_normal_utterance_preserves_onset_and_ends_after_trailing_silence(self):
        # A distinct first frame verifies that confirmation does not clip onset.
        onset = struct.pack("<hh", 800, -800) * (MIC_FRAME_BYTES // 4)
        speech = onset + SPEECH * 14
        self.install_stream(QUIET * 20 + speech + QUIET * 60)
        self.assertTrue(self.voice.capture_utterance(self.path))
        self.assertEqual(self.recorded_pcm(), QUIET * 10 + speech + QUIET * 60)
        self.assertEqual(self.stream.bytes_read, (20 + 15 + 60) * MIC_FRAME_BYTES)
        self.assertTrue(self.voice.has_meaningful_speech(self.path))
        self.assert_stopped()
        command = self.popen.call_args.args[0]
        self.assertEqual(command[0], "arecord")
        self.assertIn(self.voice.settings.audio_device, command)
        self.assertEqual(command[command.index("-t") + 1], "raw")
        self.assertEqual(self.popen.call_args.kwargs,
                         {"stdout": subprocess.PIPE, "bufsize": 0})
        self.assertTrue(all(call.args[1] == MIC_RATE and
                            len(call.args[0]) == MIC_FRAME_BYTES
                            for call in self.vad.is_speech.call_args_list))

    def test_capture_uses_input_when_playback_device_differs(self):
        self.voice.settings = replace(
            self.voice.settings, audio_device="plughw:CARD=XVF,DEV=0",
            playback_device="plughw:CARD=Speaker,DEV=1")
        self.install_stream(QUIET * 150)
        self.assertFalse(self.voice.capture_utterance(self.path))
        command = self.popen.call_args.args[0]
        self.assertEqual(command[command.index("-D") + 1],
                         "plughw:CARD=XVF,DEV=0")
        self.assertEqual(self.voice.playback_command("reply.mp3")[:5],
                         ["mpg123", "-o", "alsa", "-a",
                          "plughw:CARD=Speaker,DEV=1"])

    def test_natural_pauses_do_not_end_utterance_when_speech_resumes(self):
        # Include breaths at/after the old endpoint and just before the new one.
        for pause_frames in (14, 20, 27, 34, 40, 45, 50, 55, 59):
            with self.subTest(pause_ms=pause_frames * 30):
                self.output.seek(0)
                self.output.truncate()
                utterance = SPEECH * 10 + QUIET * pause_frames + SPEECH * 10
                self.install_stream(utterance + QUIET * 60)
                self.assertTrue(self.voice.capture_utterance(self.path))
                self.assertEqual(self.recorded_pcm(), utterance + QUIET * 60)
                self.assertEqual(self.stream.bytes_read,
                                 (20 + pause_frames + 60) * MIC_FRAME_BYTES)
                self.assertTrue(self.voice.has_meaningful_speech(self.path))
                endpoint = self.timing_events("endpoint")[0]
                self.assertEqual(int(endpoint["resumed_pauses"]), 1)
                self.assertEqual(int(endpoint["longest_resumed_gap_ms"]), pause_frames * 30)
                self.assertEqual(int(endpoint["resumed_candidates"]), int(pause_frames >= 45))
                self.assertEqual(int(endpoint["endpoint_candidates"]), 1 + int(pause_frames >= 45))

    def test_final_silence_ends_at_1800ms_with_endpoint_timing(self):
        self.install_stream(SPEECH * 10 + QUIET * 60 + SPEECH * 10)
        # Model a real-time recorder without waiting or relying on CPU speed.
        self.clock.side_effect = lambda: 100 + self.stream.bytes_read / MIC_FRAME_BYTES * 0.03
        self.assertTrue(self.voice.capture_utterance(self.path))
        self.assertEqual(self.recorded_pcm(), SPEECH * 10 + QUIET * 60)
        self.assertEqual(self.stream.bytes_read, 70 * MIC_FRAME_BYTES)
        endpoint = next(line for line in self.output.getvalue().splitlines()
                        if " stage=endpoint " in line)
        self.assertIn("[Voice Timing]", endpoint)
        self.assertIn("reason=silence", endpoint)
        fields = dict(item.split("=", 1) for item in endpoint.split() if "=" in item)
        self.assertAlmostEqual(float(fields["trailing_audio_ms"]), 1800, delta=1)
        self.assertAlmostEqual(float(fields["endpoint_wall_ms"]), 1800, delta=1)
        self.assertAlmostEqual(float(fields["pcm_ms"]), 2100, delta=1)
        self.assertAlmostEqual(float(fields["stream_audio_ms"]), 2100, delta=1)
        self.assertAlmostEqual(float(fields["capture_wall_ms"]), 2100, delta=1)
        self.assertEqual(float(fields["pre_roll_ms"]), 0)
        self.assertEqual(int(fields["endpoint_candidates"]), 1)
        self.assertEqual(int(fields["resumed_candidates"]), 0)
        candidate = self.timing_events("endpoint_candidate")
        self.assertEqual(len(candidate), 1)
        self.assertEqual(candidate[0]["status"], "start")
        self.assertEqual(int(candidate[0]["trailing_audio_ms"]), 1350)
        config = self.timing_events("capture_config")[0]
        self.assertEqual(int(config["candidate_silence_ms"]), 1350)
        self.assertEqual(int(config["silence_ms"]), 1800)
        self.assertEqual(int(config["hangover_ms"]), 450)
        self.assertEqual(int(config["max_utterance_ms"]), 20010)

    def test_repeated_candidate_endings_resume_and_restart_final_silence(self):
        utterance = (SPEECH * 10 + QUIET * 45 + SPEECH * 10
                     + QUIET * 50 + SPEECH * 10)
        self.install_stream(utterance + QUIET * 80)
        self.assertTrue(self.voice.capture_utterance(self.path))
        self.assertEqual(self.recorded_pcm(), utterance + QUIET * 60)
        self.assertEqual(self.stream.bytes_read, (30 + 45 + 50 + 60) * MIC_FRAME_BYTES)
        candidates = self.timing_events("endpoint_candidate")
        self.assertEqual([event["status"] for event in candidates],
                         ["start", "resumed", "start", "resumed", "start"])
        self.assertEqual([int(event["gap_audio_ms"]) for event in candidates
                          if event["status"] == "resumed"], [1350, 1500])
        endpoint = self.timing_events("endpoint")[0]
        self.assertEqual(int(endpoint["endpoint_candidates"]), 3)
        self.assertEqual(int(endpoint["resumed_candidates"]), 2)
        self.assertEqual(int(endpoint["resumed_pauses"]), 2)
        self.assertEqual(int(endpoint["longest_resumed_gap_ms"]), 1500)
        self.assertEqual(int(endpoint["trailing_audio_ms"]), 1800)

    def test_soft_speech_resumes_candidate_without_new_onset_confirmation(self):
        # One VAD-positive frame at the continuation floor cancels the candidate.
        utterance = SPEECH * 10 + QUIET * 50 + SOFT_SPEECH
        self.install_stream(utterance + QUIET * 80)
        self.assertTrue(self.voice.capture_utterance(self.path))
        self.assertEqual(self.recorded_pcm(), utterance + QUIET * 60)
        self.assertTrue(self.voice.has_meaningful_speech(self.path))
        candidates = self.timing_events("endpoint_candidate")
        self.assertEqual([event["status"] for event in candidates],
                         ["start", "resumed", "start"])
        self.assertEqual(int(candidates[1]["gap_audio_ms"]), 1500)
        endpoint = self.timing_events("endpoint")[0]
        self.assertEqual(int(endpoint["soft_voice_ms"]), 30)
        self.assertEqual(int(endpoint["resumed_candidates"]), 1)
        self.assertEqual(int(endpoint["trailing_audio_ms"]), 1800)

    def test_full_300ms_preroll_survives_start_confirmation(self):
        # Quiet syllable-like bytes differ by frame so retention is exact.
        pre_roll = b"".join(struct.pack("<hh", level, -level) * (MIC_FRAME_BYTES // 4)
                            for level in range(100, 200, 10))
        utterance = SPEECH * 10
        self.install_stream(QUIET * 20 + pre_roll + utterance + QUIET * 60)
        self.assertTrue(self.voice.capture_utterance(self.path))
        self.assertEqual(self.recorded_pcm(), pre_roll + utterance + QUIET * 60)
        endpoint = next(line for line in self.output.getvalue().splitlines()
                        if " stage=endpoint " in line)
        fields = dict(item.split("=", 1) for item in endpoint.split() if "=" in item)
        self.assertEqual(float(fields["pre_roll_ms"]), 300)

    def test_eighteen_second_speech_keeps_earlier_and_later_content(self):
        beginning = struct.pack("<hh", 700, -700) * (MIC_FRAME_BYTES // 4)
        ending = struct.pack("<hh", 900, -900) * (MIC_FRAME_BYTES // 4)
        utterance = beginning * 10 + SPEECH * 580 + ending * 10
        self.install_stream(utterance + QUIET * 60)
        self.assertTrue(self.voice.capture_utterance(self.path))
        self.assertEqual(self.recorded_pcm(), utterance + QUIET * 60)
        self.assertIn("reason=silence", self.output.getvalue())
        self.assertEqual(self.stream.bytes_read, 660 * MIC_FRAME_BYTES)
        endpoint = self.timing_events("endpoint")[0]
        self.assertEqual(int(endpoint["pcm_ms"]), 19800)

    def test_hard_max_duration_ends_even_with_candidate_pending(self):
        self.install_stream(SPEECH * 617 + QUIET * 80 + SPEECH * 10)
        self.assertTrue(self.voice.capture_utterance(self.path))
        self.assertEqual(self.recorded_pcm(), SPEECH * 617 + QUIET * 50)
        self.assertEqual(self.stream.bytes_read, 667 * MIC_FRAME_BYTES)
        endpoint = self.timing_events("endpoint")[0]
        self.assertEqual(endpoint["reason"], "max_duration")
        self.assertEqual(int(endpoint["pcm_ms"]), 20010)
        self.assertEqual(int(endpoint["trailing_audio_ms"]), 1500)
        self.assertEqual(int(endpoint["endpoint_candidates"]), 1)
        self.assertEqual(int(endpoint["resumed_candidates"]), 0)

    def test_hard_max_duration_takes_priority_at_candidate_and_final_boundaries(self):
        self.voice.settings = replace(self.voice.settings, max_utterance_seconds=3)
        for speech_frames, quiet_frames, candidates in ((55, 45, 0), (40, 60, 1)):
            with self.subTest(trailing_ms=quiet_frames * 30):
                self.output.seek(0)
                self.output.truncate()
                self.install_stream(SPEECH * speech_frames + QUIET * 100)
                self.assertTrue(self.voice.capture_utterance(self.path))
                self.assertEqual(self.recorded_pcm(),
                                 SPEECH * speech_frames + QUIET * quiet_frames)
                self.assertEqual(self.stream.bytes_read, 100 * MIC_FRAME_BYTES)
                endpoint = self.timing_events("endpoint")[0]
                self.assertEqual(endpoint["reason"], "max_duration")
                self.assertEqual(int(endpoint["endpoint_candidates"]), candidates)
                self.assertEqual(int(endpoint["trailing_audio_ms"]), quiet_frames * 30)

    def test_soft_continuation_uses_lower_rms_floor_after_strict_start(self):
        # Two seconds at the continuation floor must not be treated as silence.
        utterance = SPEECH * 10 + SOFT_SPEECH * 60 + SPEECH * 10
        self.install_stream(utterance + QUIET * 60)
        self.assertTrue(self.voice.capture_utterance(self.path))
        self.assertEqual(self.recorded_pcm(), utterance + QUIET * 60)
        self.assertTrue(self.voice.has_meaningful_speech(self.path))

    def test_continuation_still_requires_vad_and_rms_floor(self):
        loud_non_speech = struct.pack("<hh", 900, -900) * (MIC_FRAME_BYTES // 4)
        for gap, vad_decision in ((loud_non_speech, False), (LOW_NOISE, True)):
            with self.subTest(vad_decision=vad_decision):
                self.vad.is_speech.side_effect = lambda frame, rate: (
                    vad_decision if frame == gap else True)
                self.install_stream(SPEECH * 10 + gap * 60 + SPEECH * 10)
                self.assertTrue(self.voice.capture_utterance(self.path))
                self.assertEqual(self.recorded_pcm(), SPEECH * 10 + gap * 60)
                self.assertEqual(self.stream.bytes_read, 70 * MIC_FRAME_BYTES)

    def test_max_duration_starts_at_speech_onset_after_late_start(self):
        self.install_stream(QUIET * 120 + SPEECH * 717)
        self.assertTrue(self.voice.capture_utterance(self.path))
        self.assertEqual(self.recorded_pcm(), QUIET * 10 + SPEECH * 667)
        self.assertEqual(self.stream.bytes_read, (120 + 667) * MIC_FRAME_BYTES)
        self.assertTrue(self.voice.has_meaningful_speech(self.path))
        self.assert_stopped()
        self.assertIn("reason=max_duration", self.output.getvalue())

    def test_short_confirmed_start_still_fails_minimum_total_speech_gate(self):
        self.install_stream(SPEECH * 5 + QUIET * 60)
        self.assertTrue(self.voice.capture_utterance(self.path))
        self.assertEqual(self.recorded_pcm(), SPEECH * 5 + QUIET * 60)
        self.assertFalse(self.voice.has_meaningful_speech(self.path))

    def test_short_noise_bursts_do_not_reset_start_timeout(self):
        self.install_stream((SPEECH * 4 + QUIET) * 30)
        self.assertFalse(self.voice.capture_utterance(self.path))
        self.assertEqual(self.stream.bytes_read, 134 * MIC_FRAME_BYTES)
        self.assertFalse(self.path.exists())

    def test_both_vad_and_energy_are_required_for_start(self):
        for pcm, vad_decision in ((LOW_ENERGY, True), (SOFT_SPEECH, True),
                                  (SPEECH, False)):
            with self.subTest(vad_decision=vad_decision):
                self.install_stream(pcm * 150)
                self.vad.is_speech.return_value = vad_decision
                self.assertFalse(self.voice.capture_utterance(self.path))
                self.assertEqual(self.stream.bytes_read, 134 * MIC_FRAME_BYTES)
                self.assertFalse(self.path.exists())
                self.assertTrue(self.stream.closed)

    def test_timing_settings_control_wait_silence_and_duration(self):
        self.voice.settings = replace(self.voice.settings, speech_start_timeout=0.3,
                                      speech_silence_seconds=0.15,
                                      max_utterance_seconds=1)
        self.install_stream(QUIET * 20)
        self.assertFalse(self.voice.capture_utterance(self.path))
        self.assertEqual(self.stream.bytes_read, 10 * MIC_FRAME_BYTES)
        self.install_stream(SPEECH * 10 + QUIET * 10)
        self.assertTrue(self.voice.capture_utterance(self.path))
        self.assertEqual(self.recorded_pcm(), SPEECH * 10 + QUIET * 5)
        self.install_stream(SPEECH * 50)
        self.assertTrue(self.voice.capture_utterance(self.path))
        self.assertEqual(self.recorded_pcm(), SPEECH * 34)

    def test_shorter_legacy_and_extended_final_silence_overrides_are_honored(self):
        for silence_seconds, silence_frames in ((1.2, 40), (1.35, 45), (2.1, 70)):
            with self.subTest(silence_seconds=silence_seconds):
                self.output.seek(0)
                self.output.truncate()
                self.voice.settings = replace(self.voice.settings,
                                              speech_silence_seconds=silence_seconds)
                pause_frames = silence_frames - 1
                utterance = SPEECH * 10 + QUIET * pause_frames + SPEECH * 10
                self.install_stream(utterance + QUIET * 100 + SPEECH * 10)
                self.assertTrue(self.voice.capture_utterance(self.path))
                self.assertEqual(self.recorded_pcm(), utterance + QUIET * silence_frames)
                self.assertEqual(self.stream.bytes_read,
                                 (20 + pause_frames + silence_frames) * MIC_FRAME_BYTES)
                endpoint = self.timing_events("endpoint")[0]
                self.assertEqual(endpoint["reason"], "silence")
                self.assertEqual(int(endpoint["trailing_audio_ms"]), silence_frames * 30)
                config = self.timing_events("capture_config")[0]
                self.assertEqual(int(config["candidate_silence_ms"]), min(45, silence_frames) * 30)
                self.assertEqual(int(config["hangover_ms"]), max(0, silence_frames - 45) * 30)
                if silence_frames <= 45:
                    self.assertEqual(self.timing_events("endpoint_candidate"), [])
                    self.assertEqual(int(endpoint["endpoint_candidates"]), 0)
                else:
                    self.assertEqual(int(endpoint["endpoint_candidates"]), 2)
                    self.assertEqual(int(endpoint["resumed_candidates"]), 1)

    def test_partial_pipe_reads_are_reassembled_into_complete_frames(self):
        pcm = QUIET * 3 + SPEECH * 10 + QUIET * 60
        self.install_stream(pcm, chunk_size=137)
        self.assertTrue(self.voice.capture_utterance(self.path))
        self.assertEqual(self.recorded_pcm(), pcm)
        self.assertEqual(self.vad.is_speech.call_count, 73)
        self.assertTrue(self.voice.has_meaningful_speech(self.path))
        self.assert_stopped()

    def test_eof_and_device_failure_do_not_produce_partial_wav(self):
        for exit_code, error in ((None, RuntimeError), (0, RuntimeError),
                                 (2, subprocess.CalledProcessError)):
            with self.subTest(exit_code=exit_code):
                self.install_stream(SPEECH * 10 + b"\x00")
                self.process.poll.return_value = exit_code
                with self.assertRaises(error):
                    self.voice.capture_utterance(self.path)
                self.assertFalse(self.path.exists())
                self.assertTrue(self.stream.closed)

    def test_no_data_watchdog_is_bounded_and_cleans_up(self):
        self.select.return_value = ([], [], [])
        self.select.side_effect = None
        # Advance after select, independently of diagnostic clock reads.
        self.clock.side_effect = lambda: 100 + self.select.call_count * 20
        with self.assertRaises(subprocess.TimeoutExpired):
            self.voice.capture_utterance(self.path)
        self.assertEqual(self.stream.bytes_read, 0)
        self.assertFalse(self.path.exists())
        self.assert_stopped()
        self.assertTrue(all(0 < call.args[3] <= 0.1
                            for call in self.select.call_args_list))

    def test_shutdown_before_capture_does_not_start_recorder(self):
        self.voice.stop_event.set()
        with self.assertRaises(ConversationStopped):
            self.voice.capture_utterance(self.path)
        self.popen.assert_not_called()

    def test_shutdown_while_waiting_or_recording_closes_stream(self):
        for pcm in (QUIET * 20, SPEECH * 20):
            with self.subTest(active_speech=pcm.startswith(SPEECH)):
                self.voice.stop_event.clear()
                self.install_stream(pcm)
                self.process.reset_mock()
                polls = 0

                def select_until_shutdown(readers, *_):
                    nonlocal polls
                    polls += 1
                    if polls == 11:
                        self.voice.stop_event.set()
                        return [], [], []
                    return readers, [], []

                self.select.side_effect = select_until_shutdown
                with self.assertRaises(ConversationStopped):
                    self.voice.capture_utterance(self.path)
                self.assertEqual(self.stream.bytes_read, 10 * MIC_FRAME_BYTES)
                self.assertFalse(self.path.exists())
                self.assert_stopped()

    def test_shutdown_with_partial_frame_does_not_write_wav(self):
        self.install_stream(SPEECH, chunk_size=137)

        def cancel_after_partial_read(readers, *_):
            if self.stream.bytes_read:
                self.voice.stop_event.set()
                return [], [], []
            return readers, [], []

        self.select.side_effect = cancel_after_partial_read
        with self.assertRaises(ConversationStopped):
            self.voice.capture_utterance(self.path)
        self.vad.is_speech.assert_not_called()
        self.assertFalse(self.path.exists())
        self.assert_stopped()

    def test_shutdown_during_last_wait_frame_takes_priority_over_timeout(self):
        self.install_stream(QUIET * 150)

        def classify_until_shutdown(frame, rate):
            if self.stream.bytes_read == 134 * MIC_FRAME_BYTES:
                self.voice.stop_event.set()
            return False

        self.vad.is_speech.side_effect = classify_until_shutdown
        with self.assertRaises(ConversationStopped):
            self.voice.capture_utterance(self.path)
        self.assertFalse(self.path.exists())
        self.assert_stopped()

    def test_select_interrupt_terminates_child_and_closes_pipe(self):
        self.select.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.voice.capture_utterance(self.path)
        self.assert_stopped()

    def test_unresponsive_recorder_is_killed_after_terminate(self):
        self.install_stream(QUIET * 150)
        self.process.wait.side_effect = (subprocess.TimeoutExpired("arecord", 1), 0)
        self.assertFalse(self.voice.capture_utterance(self.path))
        self.process.kill.assert_called_once_with()
        self.assertEqual(self.process.wait.call_count, 2)
        self.assert_stopped()

    def test_vad_failure_stops_capture_without_wav(self):
        self.install_stream(SPEECH * 20)
        self.vad.is_speech.side_effect = ValueError("mock VAD failure")
        with self.assertRaises(ValueError):
            self.voice.capture_utterance(self.path)
        self.assertFalse(self.path.exists())
        self.assert_stopped()


if __name__ == "__main__":
    unittest.main()
