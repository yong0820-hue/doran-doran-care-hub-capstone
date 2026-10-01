"""Review regressions for extraction bounds, preference identity and dates."""

from datetime import datetime
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "raspberry-pi-voice-hub"))
from persistent_memory import INITIAL_GENERATION, MAX_INPUT_CHARS, MemoryExtractor, MemoryStore, SEOUL


NOW = int(datetime(2026, 9, 27, 12, tzinfo=SEOUL).timestamp() * 1000)


class MemoryExtractionRegressionTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.extractor = MemoryExtractor(self.client, "test-model")
        self.store = MemoryStore("https://example.invalid", "elder", Mock())

    def extract(self, text, subject="매운 음식", mentioned=NOW, kind="preference", **extra):
        self.client.responses.create.return_value = Mock(status="completed", output_text=json.dumps({
            "memories": [{"type": kind, "subject": subject, "evidence": text,
                          "messageIndex": 0, "importance": 3, **extra}]}))
        return self.extractor.extract([{"text": text, "createdAt": mentioned}])

    def merge(self, data, candidates):
        return self.store._merge(data, candidates, "session", INITIAL_GENERATION)

    def active(self, data):
        return [row for row in data.values() if isinstance(row, dict) and row.get("status") == "active"]

    def test_same_evidence_with_broad_or_specific_subject_has_one_identity(self):
        evidence = "나는 매운 음식 별로 안 좋아해."
        data = self.merge({}, self.extract(evidence))
        original_id = self.active(data)[0]["id"]
        data = self.merge(data, self.extract(evidence, subject="음식"))
        self.assertEqual(len(self.active(data)), 1)
        self.assertEqual(self.active(data)[0]["id"], original_id)

    def test_normalized_content_deduplicates_across_profile_subjects(self):
        data = self.merge({}, self.extract("영수는 군대 때부터 친구야.", "영수", kind="profile"))
        data = self.merge(data, self.extract("영수는 군대 때부터 친구야!", "친구", kind="profile"))
        self.assertEqual(len(self.active(data)), 1)

    def test_older_conflicting_preference_with_broad_subject_cannot_reverse_latest(self):
        data = self.merge({}, self.extract("매운 음식 좋아해.", mentioned=NOW + 1000))
        data = self.merge(data, self.extract("매운 음식 싫어해.", "음식", mentioned=NOW))
        self.assertEqual(len(self.active(data)), 1)
        self.assertEqual(self.active(data)[0]["content"], "매운 음식 좋아해.")
        self.assertEqual(self.active(data)[0]["lastMentionedAt"], NOW + 1000)

    def test_newer_preference_with_broad_subject_replaces_existing(self):
        data = self.merge({}, self.extract("매운 음식 싫어해."))
        original_id = self.active(data)[0]["id"]
        data = self.merge(data, self.extract("매운 음식 좋아해.", "음식", mentioned=NOW + 1000))
        self.assertEqual(len(self.active(data)), 1)
        self.assertEqual(self.active(data)[0]["id"], original_id)
        self.assertEqual(self.active(data)[0]["content"], "매운 음식 좋아해.")

    def test_general_preference_object_is_derived_from_evidence(self):
        data = self.merge({}, self.extract("나는 클래식 음악을 좋아해.", "클래식 음악"))
        data = self.merge(data, self.extract("클래식 음악 싫어해.", "음악", mentioned=NOW - 1000))
        self.assertEqual(len(self.active(data)), 1)
        self.assertEqual(self.active(data)[0]["content"], "나는 클래식 음악을 좋아해.")

    def test_model_provided_identity_cannot_merge_unrelated_preferences(self):
        data = self.merge({}, self.extract("매운 음식 좋아해.", topicKey="same", evidenceKey="same"))
        data = self.merge(data, self.extract("트로트 좋아해.", "트로트", topicKey="same", evidenceKey="same"))
        self.assertEqual(len(self.active(data)), 2)
        self.assertTrue(all(row.get("topicKey") != "same" for row in self.active(data)))

    def test_latest_correction_survives_budget_and_payload_is_chronological(self):
        correction = "이제 매운 음식 좋아해."
        messages = [{"text": str(index) + "가" * 1500, "createdAt": NOW + index} for index in range(10)]
        messages.append({"text": correction, "createdAt": NOW + 20})
        self.client.responses.create.return_value = Mock(status="completed", output_text='{"memories": []}')
        self.extractor.extract(messages)
        payload = json.loads(self.client.responses.create.call_args.kwargs["input"][0]["content"])
        self.assertEqual(payload[-1]["text"], correction)
        self.assertEqual([row["capturedAt"] for row in payload], sorted(row["capturedAt"] for row in payload))
        self.assertLessEqual(sum(len(row["text"]) for row in payload), MAX_INPUT_CHARS)

    def test_day_after_tomorrow_long_expression_is_not_partially_replaced(self):
        row = self.extract("내일모레 영수를 만나요", "영수", kind="episodic")[0]
        self.assertIn(row["content"], ("내일모레 영수를 만나요", "2026-09-29 영수를 만나요"))
        if "eventDate" in row:
            self.assertEqual(row["eventDate"], "2026-09-29")

    def test_unsupported_next_weekend_is_preserved_without_invented_date(self):
        row = self.extract("다음 주말 영수를 만나요", "영수", kind="episodic")[0]
        self.assertEqual(row["content"], "다음 주말 영수를 만나요")
        self.assertNotIn("eventDate", row)

    def test_supported_relative_date_with_particle_is_normalized(self):
        row = self.extract("내일은 영수를 만나요", "영수", kind="episodic")[0]
        self.assertEqual(row["content"], "2026-09-28은 영수를 만나요")
        self.assertEqual(row["eventDate"], "2026-09-28")

    def test_date_substring_inside_name_is_preserved(self):
        row = self.extract("오늘이네 가게에서 영수를 만나요", "영수", kind="episodic")[0]
        self.assertEqual(row["content"], "오늘이네 가게에서 영수를 만나요")
        self.assertNotIn("eventDate", row)


if __name__ == "__main__":
    unittest.main()
