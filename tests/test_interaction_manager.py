"""Central interaction ordering and trusted care-prompt framing."""

from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "raspberry-pi-voice-hub"))
from interaction_manager import (
    CARE_PROMPT_REQUESTS, CarePromptService, InteractionManager,
)


class InteractionManagerTests(unittest.TestCase):
    def setUp(self):
        self.manager = InteractionManager()
        self.lock = self.manager.lock

    def use(self, kind):
        return self.manager.use(self.lock, kind, lambda: None)

    def test_first_care_after_conversation_gets_one_shot_bridge(self):
        with self.use("conversation") as context:
            self.assertFalse(context.after_conversation)
        with self.use("medication") as context:
            self.assertTrue(context.after_conversation)
        with self.use("inactivity") as context:
            self.assertFalse(context.after_conversation)

    def test_nested_interaction_keeps_outer_identity(self):
        with self.use("conversation") as outer:
            with self.use("medication") as nested:
                self.assertIs(nested, outer)
                self.assertEqual(nested.kind, "conversation")
                self.assertFalse(nested.after_conversation)

    def test_failed_conversation_does_not_offer_contextual_bridge(self):
        with self.assertRaises(RuntimeError):
            with self.use("conversation"):
                raise RuntimeError("failed")
        with self.use("medication") as context:
            self.assertFalse(context.after_conversation)

    def test_care_waits_for_complete_conversation_before_bridge(self):
        conversation_entered = threading.Event()
        release_conversation = threading.Event()
        care_finished = threading.Event()
        order = []
        observed = []

        def conversation():
            with self.use("conversation"):
                order.append("conversation-start")
                conversation_entered.set()
                release_conversation.wait(2)
                order.append("conversation-end")

        def care():
            conversation_entered.wait(2)
            with self.use("medication") as context:
                order.append("care")
                observed.append(context.after_conversation)
            care_finished.set()

        chat_thread = threading.Thread(target=conversation)
        care_thread = threading.Thread(target=care)
        chat_thread.start()
        care_thread.start()
        self.assertTrue(conversation_entered.wait(2))
        self.assertFalse(care_finished.wait(0.05))
        release_conversation.set()
        chat_thread.join(2)
        care_thread.join(2)
        self.assertEqual(order, ["conversation-start", "conversation-end", "care"])
        self.assertEqual(observed, [True])

    def test_two_pending_care_actions_wait_for_boundary_and_run_serially(self):
        order = []
        contexts = []
        results = []
        threads = []
        phase = ["ai-tts"]
        started_phases = []

        def run_care(kind):
            def action(context):
                contexts.append((kind, context.after_conversation))
                started_phases.append(phase[0])
                order.append(f"{kind}-start")
                time.sleep(0.01)
                order.append(f"{kind}-end")
                return f"{kind}-outcome"

            results.append(self.manager.run_care(
                self.lock, kind, lambda: None, action,
            ))

        with self.use("conversation"):
            for kind in ("medication", "inactivity"):
                thread = threading.Thread(target=run_care, args=(kind,))
                threads.append(thread)
                thread.start()

            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with self.manager._state_lock:
                    if len(self.manager._pending) == 2:
                        break
                time.sleep(0.005)
            else:
                self.fail("care actions were not queued")

            self.assertEqual(order, [])
            phase[0] = "user-capture"
            self.assertEqual(order, [])
            phase[0] = "turn-boundary"
            outcomes = self.manager.drain_pending(lambda: None)
            self.assertEqual(len(outcomes), 2)
            self.assertCountEqual(outcomes, [
                ("medication", "medication-outcome"),
                ("inactivity", "inactivity-outcome"),
            ])

        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(results, ["medication-outcome", "inactivity-outcome"])
        self.assertCountEqual(contexts, [("medication", True), ("inactivity", True)])
        self.assertEqual(started_phases, ["turn-boundary", "turn-boundary"])
        self.assertEqual(len(order), 4)
        self.assertEqual(order[1], order[0].replace("-start", "-end"))
        self.assertEqual(order[3], order[2].replace("-start", "-end"))
        self.assertNotEqual(order[0].split("-")[0], order[2].split("-")[0])


class CarePromptServiceTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.service = CarePromptService(self.client, "test-model")

    def complete(self, text):
        self.client.responses.create.return_value = Mock(
            status="completed", output_text=text,
        )

    def test_medication_contextual_prompt_uses_closed_trusted_request(self):
        text = "아, 그리고 어르신. 지금 약 드실 시간이니 먼저 챙겨 드실까요?"
        self.complete(text)
        self.assertEqual(
            self.service.generate("medication", "fixed medication", True), text,
        )
        call = self.client.responses.create.call_args.kwargs
        self.assertEqual(call["input"], CARE_PROMPT_REQUESTS["medication"])
        self.assertNotIn("history", call)
        self.assertFalse(call["store"])

    def test_inactivity_contextual_prompt_is_separate_from_medication(self):
        text = "그런데 어르신, 한동안 움직임이 없으셨는데 몸은 괜찮으세요?"
        self.complete(text)
        self.assertEqual(
            self.service.generate("inactivity", "fixed inactivity", True), text,
        )
        self.assertEqual(
            self.client.responses.create.call_args.kwargs["input"],
            CARE_PROMPT_REQUESTS["inactivity"],
        )

    def test_llm_failure_or_unsafe_output_returns_existing_fixed_prompt(self):
        self.client.responses.create.side_effect = RuntimeError("unavailable")
        self.assertEqual(
            self.service.generate("medication", "fixed medication", True),
            "fixed medication",
        )
        self.client.responses.create.side_effect = None
        self.complete("어르신, 약을 드셨네요. 다음 이야기를 이어갈까요?")
        self.assertEqual(
            self.service.generate("medication", "fixed medication", True),
            "fixed medication",
        )

    def test_idle_care_uses_fixed_prompt_without_llm(self):
        self.assertEqual(
            self.service.generate("inactivity", "fixed inactivity", False),
            "fixed inactivity",
        )
        self.client.responses.create.assert_not_called()

    def test_care_prompt_request_snapshot_keeps_types_distinct(self):
        self.assertEqual(CARE_PROMPT_REQUESTS, {
            "medication": (
                "복약 시간이 되었음을 알리고, 대화 중 잠깐 약부터 챙긴 뒤 이야기를 "
                "이어가도 될지 어르신께 묻는 질문을 작성한다."
            ),
            "inactivity": (
                "한동안 움직임이 거의 없었던 점을 조심스럽게 말하고, 몸이 "
                "괜찮은지 어르신께 묻는 질문을 작성한다."
            ),
        })


if __name__ == "__main__":
    unittest.main()
