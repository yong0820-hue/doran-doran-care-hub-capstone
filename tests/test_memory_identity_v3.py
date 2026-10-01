"""Preference identity regressions, independent of Firebase's storage layout."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "raspberry-pi-voice-hub"))

from persistent_memory import INITIAL_GENERATION, MemoryExtractor, MemoryStore


def candidate(subject, content, timestamp, kind="preference"):
    return {"type": kind, "subject": subject, "content": content,
            "lastMentionedAt": timestamp, "importance": 3, "tags": [],
            "evidenceKey": hashlib.sha256(content.encode("utf-8")).hexdigest()}


def assert_preference_record(testcase, row, *, subject, content, mentioned,
                             memory_id=None, created=None):
    """Assert preserved evidence and ownership, not merely active row counts."""
    if memory_id is not None:
        testcase.assertEqual(row["id"], memory_id)
    testcase.assertEqual(row["subject"], subject)
    testcase.assertEqual(row["content"], content)
    testcase.assertEqual(row["evidenceKey"], hashlib.sha256(content.encode("utf-8")).hexdigest())
    testcase.assertEqual(row["lastMentionedAt"], mentioned)
    testcase.assertEqual(row["createdAt"], mentioned if created is None else created)
    testcase.assertEqual(row["status"], "active")
    testcase.assertEqual(row["type"], "preference")
    testcase.assertEqual(row["source"], "conversation")
    testcase.assertEqual(row["elderId"], "elder_1")
    testcase.assertEqual(row["sourceSessionId"], "session")


class MemoryIdentityV3Tests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore("https://example.firebaseio.com", "elder_1", Mock())
        self.rows = {}

    def merge(self, *candidates):
        self.rows = self.store._merge(self.rows, list(candidates), "session", INITIAL_GENERATION)
        return [row for row in self.rows.values() if row.get("status") == "active"]

    def test_singers_do_not_merge_through_shared_trot_content(self):
        active = self.merge(candidate("김연자", "나는 트로트 가수 김연자를 좋아해.", 1000),
                            candidate("나훈아", "나는 트로트 가수 나훈아를 싫어해.", 2000))
        self.assertEqual({row["subject"] for row in active}, {"김연자", "나훈아"})

    def test_yoga_lexical_final_syllable_is_not_stripped(self):
        self.merge(candidate("요가", "나는 요가를 좋아해.", 1000))
        active = self.merge(candidate("요가", "요가 싫어해.", 2000))
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["content"], "요가 싫어해.")
        self.assertEqual(active[0]["topicKey"], "요가")

    def test_cucumber_lexical_final_syllable_is_not_stripped(self):
        self.merge(candidate("오이", "나는 오이를 좋아해.", 1000))
        active = self.merge(candidate("오이", "오이 싫어해.", 2000))
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["content"], "오이 싫어해.")
        self.assertEqual(active[0]["topicKey"], "오이")

    def test_identical_evidence_with_broad_and_specific_subject_is_one_record(self):
        content = "나는 매운 음식 좋아해."
        active = self.merge(candidate("음식", content, 1000), candidate("매운 음식", content, 1000))
        self.assertEqual(len(active), 1)

    def test_delayed_broad_subject_cannot_reverse_newer_specific_preference(self):
        self.merge(candidate("매운 음식", "나는 매운 음식 좋아해.", 2000))
        before = deepcopy(self.rows)
        active = self.merge(candidate("음식", "음식 싫어해.", 1000))
        # A category is not a stale alias: preserve both quoted facts, while
        # the general statement stays out of recall beside a specific target.
        self.assertEqual(len(active), 2)
        for key, original in before.items():
            self.assertEqual(self.rows[key], original)
        general = next(row for row in active if row["subject"] == "음식")
        self.assertNotIn(general["id"], before)
        assert_preference_record(self, general, subject="음식", content="음식 싫어해.", mentioned=1000)
        self.assertEqual([row["content"] for row in self.store.select(self.rows, "음식")],
                         ["나는 매운 음식 좋아해."])

    def test_newer_broad_subject_preserves_independent_specific_preference(self):
        self.merge(candidate("매운 음식", "나는 매운 음식 좋아해.", 1000))
        before = deepcopy(self.rows)
        active = self.merge(candidate("음식", "음식 싫어해.", 2000))
        # Supersedes the old category-overwrite expectation: recency cannot
        # prove that a general preference corrects this specific preference.
        self.assertEqual(len(active), 2)
        for key, original in before.items():
            self.assertEqual(self.rows[key], original)
        general = next(row for row in active if row["subject"] == "음식")
        self.assertNotIn(general["id"], before)
        assert_preference_record(self, general, subject="음식", content="음식 싫어해.", mentioned=2000)
        # The newer general statement cannot prove a same-scope correction,
        # but recall must not present only the older specific as current truth.
        self.assertEqual(self.store.select(self.rows, "음식"), [])

    def test_shared_token_without_subset_does_not_merge(self):
        active = self.merge(candidate("아침 산책", "아침 산책 좋아해.", 1000),
                            candidate("저녁 산책", "저녁 산책 싫어해.", 2000))
        self.assertEqual(len(active), 2)

    def test_spacing_only_subject_change_keeps_existing_identity(self):
        self.merge(candidate("매운 음식", "나는 매운 음식 좋아해.", 1000))
        old_id = next(iter(self.rows))
        active = self.merge(candidate("매운음식", "매운 음식 싫어해.", 2000))
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], old_id)

    def test_same_evidence_cannot_merge_across_memory_types(self):
        active = self.merge(*(candidate("산책", "산책 좋아해.", 1000, kind)
                              for kind in ("episodic", "profile", "preference")))
        self.assertEqual({row["type"] for row in active}, {"episodic", "profile", "preference"})

    def test_legacy_category_record_is_not_archived_as_a_specific_duplicate(self):
        self.merge(candidate("매운 음식", "나는 매운 음식 좋아해.", 2000))
        before = deepcopy(self.rows)
        legacy = deepcopy(next(iter(self.rows.values())))
        legacy.update(id="old_duplicate", subject="음식", content="음식 싫어해.",
                      evidenceKey="b" * 64, lastMentionedAt=500)
        self.rows["old_duplicate"] = legacy
        active = self.merge(candidate("음식", "음식 싫어해.", 1000))
        # Different evidence for a category is an independent owner. Its
        # replay may update that owner only; category-based archival loses it.
        self.assertEqual(len(active), 2)
        self.assertEqual(set(self.rows), set(before) | {"old_duplicate"})
        for key, original in before.items():
            self.assertEqual(self.rows[key], original)
        assert_preference_record(self, self.rows["old_duplicate"], memory_id="old_duplicate",
                                 subject="음식", content="음식 싫어해.", mentioned=1000,
                                 created=legacy["createdAt"])
        self.assertNotIn("archivedAt", self.rows["old_duplicate"])
        self.assertNotIn("archiveReason", self.rows["old_duplicate"])
        self.assertEqual([row["content"] for row in self.store.select(self.rows, "음식")],
                         ["나는 매운 음식 좋아해."])

    def test_extractor_topic_is_supported_subject_without_content_alias(self):
        client = Mock()
        text = "나는 트로트 가수 김연자를 좋아해."
        client.responses.create.return_value = Mock(status="completed", output_text=json.dumps({
            "memories": [{"type": "preference", "subject": "김연자", "evidence": text,
                          "messageIndex": 0, "importance": 3, "tags": []}]}))
        extracted = MemoryExtractor(client, "test").extract([{"text": text, "createdAt": 1000}])
        self.assertEqual(extracted[0]["topicKey"], "김연자")


if __name__ == "__main__":
    unittest.main()
