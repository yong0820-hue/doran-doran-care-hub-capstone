"""Voice flow and failure tests without recording or contacting external services."""

import io
import json
import os
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import wave
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "raspberry-pi-voice-hub"))
from voice_ai import (ConversationController, ConversationStopped, VoiceConversation,
                      VoiceSettings, INSTRUCTIONS, TTS_INSTRUCTIONS, LISTEN_PROMPT, create_controller)


class InstructionTests(unittest.TestCase):
    # Prompt contracts catch accidental policy removal; live replies and voice
    # quality still need a Korean conversation/listening A/B test on the Pi.
    def test_persona_requires_explicit_context_for_gender_and_kinship(self):
        for rule in (
            "기본 호칭은 '어르신'으로 한다.",
            "성별이나 가족관계를 목소리, 이름, 나이, 말투로 추측하지 않는다.",
            "대화나 제공된 맥락에서 명시적으로 확인된 성별과 가족관계만 사실로 사용한다.",
            "'할머니', '할아버지', '엄마', '아버지' 같은 가족 호칭은 해당 인물과 관계가 명시적으로 확인되고 맥락에 맞을 때만 사용한다.",
            "불확실하면 '어르신'으로 부른다.",
            "다른 사람의 가족 이야기를 어르신 자신의 성별이나 가족관계로 혼동하지 않는다.",
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, INSTRUCTIONS)

    def test_persona_is_warm_but_does_not_claim_biological_family(self):
        for rule in (
            "AI 손주 '도란이'",
            "한국어로 따뜻하고 친근하게, 자연스러운 존댓말로 대화한다.",
            "'AI 손주'는 친근한 대화 역할이다.",
            "자신이 실제 사람이거나 어르신의 친손주 또는 혈연 가족인 척하지 않는다.",
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, INSTRUCTIONS)

    def test_tts_requests_clear_calm_young_adult_korean_and_comfortable_pacing(self):
        for direction in (
            "한국어를 자연스러운 젊은 성인의 목소리로, 따뜻하고 차분하게",
            "부드러운 존댓말과 또렷한 발음",
            "어르신이 듣고 이해하기 편하도록 일상 대화보다 약간 여유 있는 속도",
            "구절과 문장 사이에 짧고 자연스럽게 쉬어",
            "단어나 모음을 지나치게 늘이거나 부자연스럽게 긴 침묵을 넣지 마세요.",
            "과장된 감정 연기, 들뜬 억양, 아기 말투나 연극 같은 낭독은 피하고",
        ):
            with self.subTest(direction=direction):
                self.assertIn(direction, TTS_INSTRUCTIONS)


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.quiet = redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)
        self.client = Mock()
        self.client.audio.transcriptions.create.return_value.text = "오늘 뉴스 봤어요"
        self.client.responses.create.return_value = Mock(output_text="어떤 뉴스였어요?", status="completed")
        self.speech = Mock()
        self.client.audio.speech.with_streaming_response.create.return_value = Mock(
            __enter__=Mock(return_value=self.speech), __exit__=Mock(return_value=False))
        self.speech.stream_to_file.side_effect = lambda path: path.write_bytes(b"mock mp3")
        self.store = Mock()
        self.store.write_message.return_value = True
        self.lock = threading.Lock()
        self.voice = VoiceConversation(self.client, VoiceSettings(), self.store,
                                       self.lock, Path(self.temporary.name))

        # Synthetic PCM is not speech: mock VAD decisions to test gate/flow logic
        # without a native dependency or a microphone. Native smoke test is below.
        self.vad = Mock()
        self.vad.is_speech.return_value = True
        self.vad_module = Mock()
        self.vad_module.Vad.return_value = self.vad
        vad_patch = patch.dict(sys.modules, {"webrtcvad": self.vad_module})
        vad_patch.start()
        self.addCleanup(vad_patch.stop)

        # Mono 16 kHz PCM: quiet padding around a half-second audible burst.
        self.recorded_pcm = (
            b"\x00\x00" * 8000
            + b"\xb0\x04\x50\xfb" * 4000
            + b"\x00\x00" * 8000
        )

        def audio(command, timeout):
            self.voice.check_stopped()
            self.assertTrue(self.lock.locked())
            self.assertTrue(Path(command[-1]).is_file())

        def capture(path):
            self.voice.check_stopped()
            self.assertTrue(self.lock.locked())
            with wave.open(str(path), "wb") as recording:
                recording.setnchannels(1)
                recording.setsampwidth(2)
                recording.setframerate(16000)
                recording.writeframes(self.recorded_pcm)
            return True

        self.fake_audio = patch.object(self.voice, "run_audio", side_effect=audio)
        self.run_audio = self.fake_audio.start()
        self.addCleanup(self.fake_audio.stop)
        capture_patch = patch.object(self.voice, "capture_utterance", side_effect=capture)
        self.capture = capture_patch.start()
        self.addCleanup(capture_patch.stop)

    def write_gate_recording(self, pcm, channels=1, width=2, rate=16000):
        path = Path(self.temporary.name) / "gate.wav"
        with wave.open(str(path), "wb") as recording:
            recording.setnchannels(channels)
            recording.setsampwidth(width)
            recording.setframerate(rate)
            recording.writeframes(pcm)
        return path

    def assert_no_transcription_or_record(self):
        self.client.audio.transcriptions.create.assert_not_called()
        self.client.responses.create.assert_not_called()
        self.store.write_message.assert_not_called()
        self.assertEqual(self.voice.history, [])
        self.assertEqual(self.voice.turn_index, 0)
        self.assertFalse(self.lock.locked())
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])

    def test_two_turns_keep_order_and_validated_settings(self):
        self.assertTrue(self.voice.run_turn())
        self.assertTrue(self.voice.run_turn())
        calls = self.client.responses.create.call_args_list
        self.assertEqual(calls[1].kwargs["input"], [
            {"role": "user", "content": "오늘 뉴스 봤어요"},
            {"role": "assistant", "content": "어떤 뉴스였어요?"},
            {"role": "user", "content": "오늘 뉴스 봤어요"},
        ])
        for call in calls:
            self.assertEqual(call.kwargs["instructions"], INSTRUCTIONS)
        self.assertFalse(calls[0].kwargs["store"])
        self.assertEqual(calls[0].kwargs["model"], "gpt-5.6-luna")
        self.assertEqual(self.client.audio.transcriptions.create.call_args.kwargs["language"], "ko")
        speech = self.client.audio.speech.with_streaming_response.create.call_args.kwargs
        self.assertEqual((speech["voice"], speech["speed"]), ("marin", 1.15))
        self.assertEqual([call.args[0][0] for call in self.run_audio.call_args_list],
                         ["mpg123", "mpg123"] * 2)
        for call in self.run_audio.call_args_list:
            command = call.args[0]
            self.assertEqual(command[:6],
                             ["mpg123", "-o", "alsa", "-a",
                              self.voice.settings.playback_device, "-q"])
        self.assertEqual(self.capture.call_count, 2)
        self.assertEqual([
            call.kwargs["input"]
            for call in self.client.audio.speech.with_streaming_response.create.call_args_list
        ], [LISTEN_PROMPT, "어떤 뉴스였어요?"] * 2)
        self.assertEqual(self.store.write_message.call_count, 4)
        records = [call.kwargs for call in self.store.write_message.call_args_list]
        self.assertEqual([r["audio_status"] for r in records], ["recorded", "played"] * 2)
        self.assertEqual(len({r["session_id"] for r in records}), 1)
        self.assertEqual(len({r["message_id"] for r in records}), 4)
        self.assertTrue(all(r["flush"] is False for r in records))
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])

    def test_history_drops_complete_pairs(self):
        self.voice.settings = replace(self.voice.settings, history_turns=1)
        for _ in range(3):
            self.voice.run_turn()
        self.assertEqual(len(self.voice.history), 2)
        self.assertEqual(len(self.client.responses.create.call_args.kwargs["input"]), 3)

    @staticmethod
    def timing_events(output):
        return [dict(field.split("=", 1) for field in line.split()[2:])
                for line in output.getvalue().splitlines()
                if line.startswith("[Voice Timing] ")]

    def test_turn_timings_separate_api_lock_and_player_launch_latency(self):
        now = [100.0]
        output = io.StringIO()
        self.voice.last_activity = now[0]
        capture = self.capture.side_effect

        def capture_with_timestamps(path):
            result = capture(path)
            now[0] += 2
            self.voice._endpoint_at = now[0]
            self.voice._last_voice_at = now[0] - 1.8
            return result

        def synthesize(path):
            now[0] += 0.2 if path.name.endswith("_prompt.mp3") else 0.8
            path.write_bytes(b"mock mp3")

        def stt(**kwargs):
            now[0] += 0.4
            return Mock(text="오늘 뉴스 봤어요")

        def llm(**kwargs):
            now[0] += 0.6
            return Mock(output_text="어떤 뉴스였어요?", status="completed")

        process = Mock()
        process.poll.return_value = 0

        def wait(**kwargs):
            now[0] += 0.3
            return 0

        def spawn(command):
            self.assertTrue(self.lock.locked())
            now[0] += 0.05
            return process

        lock_calls = []

        def acquire(**kwargs):
            lock_calls.append(True)
            now[0] += 0.1 if len(lock_calls) == 1 else 0.2
            return self.lock.acquire(**kwargs)

        self.voice.audio_lock = Mock(acquire=Mock(side_effect=acquire),
                                     release=Mock(side_effect=self.lock.release))
        self.capture.side_effect = capture_with_timestamps
        self.speech.stream_to_file.side_effect = synthesize
        self.client.audio.transcriptions.create.side_effect = stt
        self.client.responses.create.side_effect = llm
        process.wait.side_effect = wait
        self.fake_audio.stop()
        with patch("voice_ai.time.monotonic", side_effect=lambda: now[0]), \
                patch("voice_ai.subprocess.Popen", side_effect=spawn), redirect_stdout(output):
            self.assertTrue(self.voice.run_turn())
        events = self.timing_events(output)
        self.assertEqual(len({event["turn"] for event in events}), 1)
        stages = {event["stage"]: event for event in events
                  if event.get("status") != "start"}
        for stage, expected_ms in {"prompt_tts": 200, "stt": 400, "llm": 600,
                                   "response_tts": 800, "response_audio_lock": 200}.items():
            self.assertEqual(int(stages[stage]["elapsed_ms"]), expected_ms)
        self.assertEqual(stages["stt"]["language"], "ko")
        launch = stages["response_playback_start"]
        self.assertEqual(launch["observation"], "process_spawn")
        self.assertEqual(int(launch["launch_ms"]), 50)
        self.assertEqual(int(launch["from_endpoint_ms"]), 2050)
        self.assertEqual(int(launch["from_last_voice_ms"]), 3850)
        ordered_stages = list(stages)
        self.assertLess(ordered_stages.index("response_playback_start"),
                        ordered_stages.index("response_playback"))
        self.assertNotIn("오늘 뉴스", str(events))
        self.assertNotIn("어떤 뉴스", str(events))
        self.assertFalse(self.lock.locked())

    def test_stt_failure_logs_duration_and_type_without_exception_body(self):
        now = [100.0]
        self.voice.last_activity = now[0]
        output = io.StringIO()

        def fail_stt(**kwargs):
            now[0] += 0.7
            raise TimeoutError("private SDK response body")

        self.client.audio.transcriptions.create.side_effect = fail_stt
        with patch("voice_ai.time.monotonic", side_effect=lambda: now[0]), \
                redirect_stdout(output), self.assertRaises(TimeoutError):
            self.voice.run_turn()
        events = self.timing_events(output)
        stt = [event for event in events if event["stage"] == "stt"]
        self.assertEqual([event["status"] for event in stt], ["start", "error"])
        self.assertEqual(stt[-1]["elapsed_ms"], "700")
        self.assertEqual(stt[-1]["error_type"], "TimeoutError")
        self.assertNotIn("private SDK", output.getvalue())
        self.assertNotIn("llm", [event["stage"] for event in events])
        self.client.audio.transcriptions.create.assert_called_once()
        self.client.responses.create.assert_not_called()
        self.store.write_message.assert_not_called()
        self.assertEqual(self.voice.history, [])
        self.assertFalse(self.lock.locked())
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])

    def test_cancelled_playback_timing_never_claims_capture_or_completion(self):
        self.run_audio.side_effect = ConversationStopped()
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(ConversationStopped):
            self.voice.run_turn()
        events = self.timing_events(output)
        playback = [event for event in events if event["stage"] == "prompt_playback"]
        self.assertEqual([event["status"] for event in playback], ["start", "cancelled"])
        self.assertNotIn("capture", [event["stage"] for event in events])
        self.capture.assert_not_called()
        self.assert_no_transcription_or_record()

    def test_silent_attempt_timings_skip_api_stages_and_use_fresh_ids(self):
        self.capture.side_effect = None
        self.capture.return_value = False
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertFalse(self.voice.run_turn())
            self.assertFalse(self.voice.run_turn())
        events = self.timing_events(output)
        self.assertEqual(len({event["turn"] for event in events}), 2)
        self.assertTrue({"stt", "llm", "response_tts", "response_playback_start"}.isdisjoint(
            event["stage"] for event in events))
        self.assertEqual(self.voice.turn_index, 0)
        self.assert_no_transcription_or_record()

    def test_idle_session_gets_new_id_and_no_old_history(self):
        self.voice.run_turn()
        old = self.voice.session_id
        self.voice.last_activity -= 301
        self.voice.run_turn()
        self.assertNotEqual(old, self.voice.session_id)
        self.assertEqual(self.voice.turn_index, 1)
        self.assertEqual(len(self.client.responses.create.call_args.kwargs["input"]), 1)

    def test_empty_stt_produces_no_response_or_record(self):
        self.client.audio.transcriptions.create.return_value.text = "  "
        self.assertFalse(self.voice.run_turn())
        self.client.responses.create.assert_not_called()
        self.store.write_message.assert_not_called()
        self.assertEqual(self.voice.history, [])
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])

    def test_unvoiced_recordings_skip_stt_and_response_and_clean_audio(self):
        quiet = b"\x00\x00" * 8000
        loud = b"\xb0\x04\x50\xfb"
        cases = {
            "silence": (quiet * 2, True),
            "steady_noise": (loud * 8000, False),
            "short_burst": (quiet + loud * 720 + quiet, True),
            "empty": (b"", True),
        }
        for name, (pcm, speech_decision) in cases.items():
            with self.subTest(recording=name):
                self.recorded_pcm = pcm
                self.vad.is_speech.return_value = speech_decision
                self.client.reset_mock()
                self.assertFalse(self.voice.run_turn())
                self.client.audio.transcriptions.create.assert_not_called()
                self.client.responses.create.assert_not_called()
                self.client.audio.speech.with_streaming_response.create.assert_called_once()
                self.assertEqual(
                    self.client.audio.speech.with_streaming_response.create.call_args.kwargs["input"],
                    LISTEN_PROMPT,
                )
                self.store.write_message.assert_not_called()
                self.assertEqual(self.voice.history, [])
                self.assertEqual(self.voice.turn_index, 0)
                self.assertFalse(self.lock.locked())
                self.assertEqual(list(Path(self.temporary.name).iterdir()), [])

    def test_vad_requires_energy_and_sustained_speech_in_same_frames(self):
        loud = b"\xb0\x04\x50\xfb" * 240  # 30 ms at RMS 1200
        low = b"\x2b\x01\xd5\xfe" * 240  # RMS 299, below the energy floor
        quiet = b"\x00\x00" * 480
        cases = {
            "continuous_speech_without_quiet_padding": (loud * 10, [True] * 10, True),
            "minimum_sustained_runs": (loud * 5 + quiet + loud * 5, [True] * 11, True),
            "loud_non_speech": (loud * 60, [False] * 60, False),
            "vad_positive_but_low_energy": (low * 10, [True] * 10, False),
            "less_than_300ms": (loud * 9, [True] * 9, False),
            "scattered_300ms": (loud * 20, [True, False] * 10, False),
            "runs_shorter_than_150ms": (loud * 12,
                                        [True] * 4 + [False] + [True] * 4
                                        + [False] + [True] * 2, False),
            "disjoint_energy_and_vad": (loud * 10 + quiet * 10,
                                         [False] * 10 + [True] * 10, False),
            "partial_frame_only": (loud[:600], [], False),
        }
        for name, (pcm, decisions, expected) in cases.items():
            with self.subTest(recording=name):
                self.vad.is_speech.reset_mock()
                self.vad.is_speech.side_effect = decisions
                self.assertEqual(self.voice.has_meaningful_speech(
                    self.write_gate_recording(pcm)), expected)
                self.assertEqual(self.vad.is_speech.call_count, len(decisions))

    def test_vad_processes_complete_frames_in_order_with_a_fresh_detector(self):
        loud = b"\xb0\x04\x50\xfb" * 240
        quiet = b"\x00\x00" * 480
        frames = [quiet] * 2 + [loud] * 10 + [quiet] * 2
        path = self.write_gate_recording(b"".join(frames) + loud[:600])
        detectors = [Mock(), Mock()]
        for detector in detectors:
            detector.is_speech.return_value = True
        self.vad_module.Vad.side_effect = detectors
        for detector in detectors:
            self.assertTrue(self.voice.has_meaningful_speech(path))
            self.assertEqual([call.args for call in detector.is_speech.call_args_list],
                             [(frame, 16000) for frame in frames])
        self.assertEqual([call.args for call in self.vad_module.Vad.call_args_list],
                         [(3,), (3,)])

    def test_invalid_or_truncated_wav_fails_before_vad(self):
        for format_args in ({"channels": 2}, {"width": 1}, {"rate": 8000}):
            with self.subTest(format=format_args):
                path = self.write_gate_recording(b"\x00" * 1920, **format_args)
                with self.assertRaises(ValueError):
                    self.voice.has_meaningful_speech(path)
        path = self.write_gate_recording(b"\x00" * 1920)
        path.write_bytes(path.read_bytes()[:-2])
        with self.assertRaises(ValueError):
            self.voice.has_meaningful_speech(path)
        path.write_bytes(b"not a WAV recording")
        with self.assertRaises((wave.Error, EOFError)):
            self.voice.has_meaningful_speech(path)
        self.vad_module.Vad.assert_not_called()

    def test_missing_or_failed_vad_never_falls_back_to_energy(self):
        for missing in (True, False):
            with self.subTest(missing_dependency=missing):
                self.client.reset_mock()
                self.vad.is_speech.side_effect = RuntimeError("native VAD failure")
                output = io.StringIO()
                with patch.dict(sys.modules, {"webrtcvad": None if missing else self.vad_module}), \
                        redirect_stdout(output), self.assertRaises(RuntimeError):
                    self.voice.run_turn()
                if missing:
                    self.assertIn("webrtcvad-wheels", output.getvalue())
                    self.assertIn("pip install", output.getvalue())
                self.assert_no_transcription_or_record()

    def test_stop_during_or_immediately_after_vad_prevents_stt(self):
        def stop_during_vad(frame, sample_rate):
            self.voice.stop_event.set()
            return True

        def stop_after_vad(path):
            self.voice.stop_event.set()
            return True

        for after_gate in (False, True):
            with self.subTest(after_gate=after_gate):
                self.voice.stop_event.clear()
                self.client.reset_mock()
                if after_gate:
                    gate_patch = patch.object(self.voice, "has_meaningful_speech",
                                              side_effect=stop_after_vad)
                else:
                    gate_patch = patch.object(self.vad, "is_speech",
                                              side_effect=stop_during_vad)
                with gate_patch, self.assertRaises(ConversationStopped):
                    self.voice.run_turn()
                self.assert_no_transcription_or_record()

    def test_recording_failure_releases_lock_and_does_not_transcribe(self):
        self.capture.side_effect = FileNotFoundError("arecord")
        with self.assertRaises(FileNotFoundError):
            self.voice.run_turn()
        self.assertFalse(self.lock.locked())
        self.client.audio.transcriptions.create.assert_not_called()
        self.store.write_message.assert_not_called()
        self.assertEqual(self.run_audio.call_count, 1)
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])

    def test_no_speech_timeout_skips_gate_stt_and_logs_and_releases_lock(self):
        self.capture.side_effect = None
        self.capture.return_value = False
        with patch.object(self.voice, "has_meaningful_speech") as gate:
            self.assertFalse(self.voice.run_turn())
        gate.assert_not_called()
        self.assert_no_transcription_or_record()
        self.assertEqual(self.run_audio.call_count, 1)

    def test_capture_cancellation_cleans_turn_without_transcription_or_logs(self):
        self.capture.side_effect = ConversationStopped
        with self.assertRaises(ConversationStopped):
            self.voice.run_turn()
        self.assert_no_transcription_or_record()
        self.assertEqual(self.run_audio.call_count, 1)

    def test_stt_failure_cleans_audio_without_fake_records(self):
        self.client.audio.transcriptions.create.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.voice.run_turn()
        self.store.write_message.assert_not_called()
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])

    def test_generation_failure_keeps_user_record_and_can_retry(self):
        self.client.responses.create.side_effect = [TimeoutError(), Mock(output_text="다시 들려주세요", status="completed")]
        with self.assertRaises(TimeoutError):
            self.voice.run_turn()
        self.assertEqual(self.store.write_message.call_count, 1)
        self.assertEqual(self.voice.history, [])
        self.assertTrue(self.voice.run_turn())
        self.assertEqual(len(self.voice.history), 2)

    def test_incomplete_generation_is_not_spoken(self):
        self.client.responses.create.return_value.status = "incomplete"
        with self.assertRaises(ValueError):
            self.voice.run_turn()
        self.client.audio.speech.with_streaming_response.create.assert_called_once()
        self.assertEqual(
            self.client.audio.speech.with_streaming_response.create.call_args.kwargs["input"],
            LISTEN_PROMPT,
        )
        self.assertEqual(self.store.write_message.call_count, 1)

    def test_tts_failure_marks_assistant_failed_without_remembering_unheard_reply(self):
        stream = self.speech.stream_to_file.side_effect

        def fail_response_tts(path):
            stream(path)
            if path.name.endswith("_response.mp3"):
                raise TimeoutError()

        self.speech.stream_to_file.side_effect = fail_response_tts
        with self.assertRaises(TimeoutError):
            self.voice.run_turn()
        self.assertEqual(self.store.write_message.call_args.kwargs["audio_status"], "failed")
        self.assertEqual(self.voice.history, [])
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])

    def test_playback_failure_has_no_successful_history(self):
        record = self.run_audio.side_effect

        def fail_playback(command, timeout):
            record(command, timeout)
            if command[0] == "mpg123" and command[-1].endswith("_response.mp3"):
                raise subprocess.CalledProcessError(1, command)

        self.run_audio.side_effect = fail_playback
        with self.assertRaises(subprocess.CalledProcessError):
            self.voice.run_turn()
        self.assertEqual(self.store.write_message.call_args.kwargs["audio_status"], "failed")
        self.assertFalse(self.lock.locked())
        self.assertEqual(self.voice.history, [])

    def test_network_and_log_enqueue_never_hold_audio_lock(self):
        def unlocked(*args, **kwargs):
            self.assertFalse(self.lock.locked())
            return Mock(text="안녕하세요")

        self.client.audio.transcriptions.create.side_effect = unlocked
        self.store.write_message.side_effect = lambda **kw: unlocked()
        response = self.client.responses.create.return_value
        speech = self.client.audio.speech.with_streaming_response.create.return_value

        def response_unlocked(**kwargs):
            unlocked()
            return response

        def speech_unlocked(**kwargs):
            unlocked()
            return speech

        self.client.responses.create.side_effect = response_unlocked
        self.client.audio.speech.with_streaming_response.create.side_effect = speech_unlocked
        self.assertTrue(self.voice.run_turn())

    def test_local_storage_failure_does_not_stop_voice(self):
        self.store.write_message.side_effect = OSError("disk full")
        self.assertTrue(self.voice.run_turn())
        self.assertEqual(len(self.voice.history), 2)

    def test_stop_during_response_tts_prevents_response_playback(self):
        stream = self.speech.stream_to_file.side_effect

        def stop_response(path):
            stream(path)
            if path.name.endswith("_response.mp3"):
                self.voice.stop_event.set()

        self.speech.stream_to_file.side_effect = stop_response
        with self.assertRaises(ConversationStopped):
            self.voice.run_turn()
        self.assertEqual(self.run_audio.call_count, 1)
        self.assertEqual(self.store.write_message.call_args.kwargs["audio_status"], "cancelled")
        self.assertEqual(self.voice.history, [])
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])

    def test_prompt_and_recording_share_lock_in_expected_turn_order(self):
        events = []
        audio = self.run_audio.side_effect
        capture = self.capture.side_effect
        stream = self.speech.stream_to_file.side_effect
        acquire = self.lock.acquire
        release = self.lock.release
        tracked_lock = Mock()

        def acquire_audio(**kwargs):
            acquired = acquire(**kwargs)
            if acquired:
                events.append("lock")
            return acquired

        def release_audio():
            events.append("unlock")
            release()

        def record_audio(command, timeout):
            audio(command, timeout)
            events.append("prompt-play" if command[-1].endswith("_prompt.mp3") else
                          "response-play")

        def capture_audio(path):
            result = capture(path)
            events.append("record")
            return result

        def speech_audio(path):
            events.append("prompt-tts" if path.name.endswith("_prompt.mp3") else "response-tts")
            stream(path)

        def transcribe(**kwargs):
            events.append("transcribe")
            return Mock(text="오늘 뉴스 봤어요")

        def respond(**kwargs):
            events.append("respond")
            return Mock(output_text="어떤 뉴스였어요?", status="completed")

        tracked_lock.acquire.side_effect = acquire_audio
        tracked_lock.release.side_effect = release_audio
        self.voice.audio_lock = tracked_lock
        self.run_audio.side_effect = record_audio
        self.capture.side_effect = capture_audio
        self.speech.stream_to_file.side_effect = speech_audio
        self.client.audio.transcriptions.create.side_effect = transcribe
        self.client.responses.create.side_effect = respond
        self.assertTrue(self.voice.run_turn())
        self.assertEqual(events, [
            "prompt-tts", "lock", "prompt-play", "record", "unlock",
            "transcribe", "respond", "response-tts", "lock", "response-play", "unlock",
        ])
        self.assertEqual([call.kwargs["text"] for call in self.store.write_message.call_args_list],
                         ["오늘 뉴스 봤어요", "어떤 뉴스였어요?"])

    def test_prompt_tts_failure_removes_partial_audio_and_can_retry(self):
        stream = self.speech.stream_to_file.side_effect

        def fail_prompt(path):
            stream(path)
            raise TimeoutError()

        self.speech.stream_to_file.side_effect = fail_prompt
        with self.assertRaises(TimeoutError):
            self.voice.run_turn()
        self.run_audio.assert_not_called()
        self.capture.assert_not_called()
        self.client.audio.transcriptions.create.assert_not_called()
        self.client.responses.create.assert_not_called()
        self.store.write_message.assert_not_called()
        self.assertFalse(self.lock.locked())
        self.assertEqual(self.voice.turn_index, 0)
        self.assertEqual(self.voice.history, [])
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])
        self.speech.stream_to_file.side_effect = stream
        self.assertTrue(self.voice.run_turn())

    def test_prompt_playback_failure_prevents_recording_and_cleans_audio(self):
        self.run_audio.side_effect = subprocess.CalledProcessError(1, "mpg123")
        with self.assertRaises(subprocess.CalledProcessError):
            self.voice.run_turn()
        self.assertEqual(self.run_audio.call_count, 1)
        self.capture.assert_not_called()
        self.assertTrue(self.run_audio.call_args.args[0][-1].endswith("_prompt.mp3"))
        self.client.audio.transcriptions.create.assert_not_called()
        self.client.responses.create.assert_not_called()
        self.store.write_message.assert_not_called()
        self.assertFalse(self.lock.locked())
        self.assertEqual(self.voice.history, [])
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])

    def test_stop_during_prompt_tts_prevents_playback_and_recording(self):
        stream = self.speech.stream_to_file.side_effect

        def stop_prompt(path):
            stream(path)
            self.voice.stop_event.set()

        self.speech.stream_to_file.side_effect = stop_prompt
        with self.assertRaises(ConversationStopped):
            self.voice.run_turn()
        self.run_audio.assert_not_called()
        self.client.audio.transcriptions.create.assert_not_called()
        self.store.write_message.assert_not_called()
        self.assertFalse(self.lock.locked())
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])

    def test_stop_during_prompt_playback_prevents_recording(self):
        audio = self.run_audio.side_effect

        def stop_after_prompt(command, timeout):
            audio(command, timeout)
            self.voice.stop_event.set()

        self.run_audio.side_effect = stop_after_prompt
        with self.assertRaises(ConversationStopped):
            self.voice.run_turn()
        # The capture entrypoint must check stop before opening the microphone.
        self.capture.assert_called_once()
        self.client.audio.transcriptions.create.assert_not_called()
        self.store.write_message.assert_not_called()
        self.assertFalse(self.lock.locked())
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])

    def test_wait_for_audio_lock_is_cancellable(self):
        self.lock.acquire()
        self.voice.stop_event.set()
        try:
            with self.assertRaises(ConversationStopped), self.voice.use_audio():
                self.fail("Stopped voice must not acquire microphone")
        finally:
            self.lock.release()

    def test_audio_subprocess_is_terminated_on_stop(self):
        self.fake_audio.stop()
        process = Mock()
        process.poll.return_value = None

        process.wait.side_effect = [subprocess.TimeoutExpired("arecord", 0.1), 0]
        with patch("voice_ai.subprocess.Popen", return_value=process):
            # Cancellation after spawn, before the first wait, still cleans the child.
            with patch.object(self.voice, "check_stopped", side_effect=[None, ConversationStopped()]):
                with self.assertRaises(ConversationStopped):
                    self.voice.run_audio(["arecord"], 5)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()

    def test_terminal_controls_reset_unknown_and_q(self):
        self.voice.run_turn = Mock()
        old = self.voice.session_id
        controller = ConversationController(self.voice)
        with patch("builtins.input", side_effect=["n", "unknown", "", "q"]):
            controller._run()
        self.assertNotEqual(old, self.voice.session_id)
        self.voice.run_turn.assert_called_once()
        self.assertTrue(controller.finished.is_set())

    def test_controller_recovers_from_turn_failure_and_eof(self):
        self.voice.run_turn = Mock(side_effect=[TimeoutError(), True])
        controller = ConversationController(self.voice)
        with patch("builtins.input", side_effect=["", "", EOFError()]):
            controller._run()
        self.assertEqual(self.voice.run_turn.call_count, 2)
        self.assertTrue(controller.finished.is_set())

    def test_invalid_configuration_and_non_terminal_fail_before_client_creation(self):
        for name, value in [("AI_MAX_UTTERANCE_SECONDS", "0"), ("OPENAI_TTS_SPEED", "nan"),
                            ("AI_HISTORY_TURNS", "-1"), ("AI_API_TIMEOUT_SECONDS", "infinity")]:
            with self.subTest(name=name), patch.dict(os.environ, {name: value}, clear=True):
                with self.assertRaises(ValueError):
                    VoiceSettings.from_env()
        with patch("voice_ai.sys.stdin") as terminal:
            terminal.isatty.return_value = False
            with self.assertRaises(ValueError):
                create_controller("", "elder", "device", "room", self.lock, self.temporary.name)

    def test_tts_defaults_and_env_overrides_reach_prompt_and_response(self):
        defaults = VoiceSettings()
        self.assertEqual((defaults.tts_model, defaults.tts_voice, defaults.tts_speed),
                         ("gpt-4o-mini-tts", "marin", 1.15))
        for env, expected in (
            ({}, ("gpt-4o-mini-tts", "marin", 1.15)),
            ({"OPENAI_TTS_MODEL": "gpt-4o-mini-tts-2025-12-15",
              "OPENAI_TTS_VOICE": "cedar", "OPENAI_TTS_SPEED": "0.95"},
             ("gpt-4o-mini-tts-2025-12-15", "cedar", 0.95)),
        ):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                settings = VoiceSettings.from_env()
                self.assertEqual((settings.tts_model, settings.tts_voice, settings.tts_speed),
                                 expected)
                self.voice.settings = settings
                self.client.audio.speech.with_streaming_response.create.reset_mock()
                self.assertTrue(self.voice.run_turn())
                calls = self.client.audio.speech.with_streaming_response.create.call_args_list
                self.assertEqual([call.kwargs["input"] for call in calls],
                                 [LISTEN_PROMPT, "어떤 뉴스였어요?"])
                for call in calls:
                    speech = call.kwargs
                    self.assertEqual((speech["model"], speech["voice"], speech["speed"]),
                                     expected)
                    self.assertEqual(speech["instructions"], TTS_INSTRUCTIONS)

    def test_tts_speed_bounds_and_nonfinite_values(self):
        for value in ("0.25", "4"):
            with self.subTest(value=value), \
                    patch.dict(os.environ, {"OPENAI_TTS_SPEED": value}, clear=True):
                self.assertEqual(VoiceSettings.from_env().tts_speed, float(value))
        for value in ("0.24", "4.01", "nan", "inf", "-inf", "oops"):
            with self.subTest(value=value), \
                    patch.dict(os.environ, {"OPENAI_TTS_SPEED": value}, clear=True), \
                    self.assertRaises(ValueError):
                VoiceSettings.from_env()

    def test_capture_timing_defaults_overrides_and_legacy_setting(self):
        with patch.dict(os.environ, {"AI_RECORD_SECONDS": "5"}, clear=True):
            settings = VoiceSettings.from_env()
        self.assertEqual((settings.speech_start_timeout, settings.speech_silence_seconds,
                          settings.max_utterance_seconds, settings.min_speech_ms),
                         (4.0, 1.8, 20.0, 300))
        with patch.dict(os.environ, {
            "AI_SPEECH_START_TIMEOUT_SECONDS": "3.5",
            "AI_SPEECH_SILENCE_SECONDS": "1.2",
            "AI_MAX_UTTERANCE_SECONDS": "12",
            "AI_MIN_SPEECH_MS": "600",
        }, clear=True):
            settings = VoiceSettings.from_env()
        self.assertEqual((settings.speech_start_timeout, settings.speech_silence_seconds,
                          settings.max_utterance_seconds, settings.min_speech_ms),
                         (3.5, 1.2, 12.0, 600))
        self.voice.settings = settings
        self.assertFalse(self.voice.has_meaningful_speech(self.write_gate_recording(
            b"\xb0\x04\x50\xfb" * 240 * 19)))  # 570 ms < configured 600 ms

    def test_capture_timing_rejects_invalid_bounds_and_nonfinite_values(self):
        for name, values in {
            "AI_SPEECH_START_TIMEOUT_SECONDS": ("0", "31", "nan", "inf", "oops"),
            "AI_SPEECH_SILENCE_SECONDS": ("0", "3.1", "nan", "inf"),
            "AI_MAX_UTTERANCE_SECONDS": ("0", "31", "nan", "inf"),
            "AI_MIN_SPEECH_MS": ("149", "3001", "nan", "300.5"),
        }.items():
            for value in values:
                with self.subTest(name=name, value=value), \
                        patch.dict(os.environ, {name: value}, clear=True), \
                        self.assertRaises(ValueError):
                    VoiceSettings.from_env()
        with patch.dict(os.environ, {
            "AI_MAX_UTTERANCE_SECONDS": "1", "AI_MIN_SPEECH_MS": "1200",
        }, clear=True), self.assertRaises(ValueError):
            VoiceSettings.from_env()

    def test_real_sdk_serializes_all_four_requests_without_network(self):
        try:
            import httpx
            from openai import OpenAI
        except ImportError:
            self.skipTest("Install requirements.txt to verify the real SDK request contract")
        seen = []

        def handler(request):
            seen.append(request)
            self.assertFalse(self.lock.locked())
            if request.url.path == "/v1/audio/transcriptions":
                return httpx.Response(200, json={"text": "뉴스 봤어요"})
            if request.url.path == "/v1/responses":
                return httpx.Response(200, json={
                    "id": "resp_test", "object": "response", "created_at": 1,
                    "status": "completed", "model": "gpt-5.6-luna",
                    "output": [{"id": "msg_test", "type": "message", "role": "assistant",
                                "status": "completed", "content": [
                                    {"type": "output_text", "text": "어떤 뉴스였어요?", "annotations": []}]}],
                })
            if request.url.path == "/v1/audio/speech":
                return httpx.Response(200, content=b"mock mp3", headers={"content-type": "audio/mpeg"})
            self.fail(f"Unexpected API route {request.url.path}")

        with OpenAI(api_key="test-placeholder", max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
            self.voice.client = client
            self.assertTrue(self.voice.run_turn())
        self.assertEqual([request.url.path for request in seen], [
            "/v1/audio/speech", "/v1/audio/transcriptions", "/v1/responses", "/v1/audio/speech",
        ])
        self.assertEqual(json.loads(seen[0].content)["input"], LISTEN_PROMPT)
        self.assertIn(b"gpt-4o-transcribe", seen[1].content)
        self.assertIn(b'name="language"\r\n\r\nko\r\n', seen[1].content)
        self.assertFalse(json.loads(seen[2].content)["store"])
        self.assertEqual(json.loads(seen[2].content)["instructions"], INSTRUCTIONS)
        self.assertEqual(json.loads(seen[3].content)["input"], "어떤 뉴스였어요?")
        for request in (seen[0], seen[3]):
            speech = json.loads(request.content)
            self.assertEqual(speech["model"], "gpt-4o-mini-tts")
            self.assertEqual(speech["instructions"], TTS_INSTRUCTIONS)
            self.assertEqual((speech["speed"], speech["voice"], speech["response_format"]),
                             (1.15, "marin", "mp3"))


class NativeVadTests(unittest.TestCase):
    def test_real_native_gate_rejects_silence_without_network(self):
        try:
            import webrtcvad
        except ImportError:
            self.skipTest("Install webrtcvad-wheels to verify the native detector")
        self.assertTrue(webrtcvad.valid_rate_and_frame_length(16000, 480))
        self.assertFalse(webrtcvad.Vad(3).is_speech(b"\x00\x00" * 480, 16000))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "silence.wav"
            with wave.open(str(path), "wb") as recording:
                recording.setnchannels(1)
                recording.setsampwidth(2)
                recording.setframerate(16000)
                recording.writeframes(b"\x00\x00" * 16000)
            client = Mock()
            voice = VoiceConversation(client, VoiceSettings(), Mock(), threading.Lock(), directory)
            with redirect_stdout(io.StringIO()):
                self.assertFalse(voice.has_meaningful_speech(path))
            self.assertEqual(client.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
