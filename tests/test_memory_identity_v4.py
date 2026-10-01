"""Fourth-review regressions for conservative preference target resolution."""

from copy import deepcopy
import itertools
import unicodedata
import unittest
from unittest.mock import Mock

from test_memory_identity_v3 import assert_preference_record, candidate
from persistent_memory import INITIAL_GENERATION, MemoryStore


class MemoryIdentityV4Tests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore("https://example.firebaseio.com", "elder_1", Mock())
        self.rows = {}

    def merge(self, *candidates):
        self.rows = self.store._merge(self.rows, list(candidates), "session", INITIAL_GENERATION)
        return [row for row in self.rows.values() if row.get("status") == "active"]

    def test_delayed_broad_target_preserves_both_newer_specifics(self):
        self.merge(candidate("아침 산책", "아침 산책 좋아해.", 100),
                   candidate("저녁 산책", "저녁 산책 싫어해.", 200))
        before = deepcopy(self.rows)
        active = self.merge(candidate("산책", "산책 좋아해.", 0))
        # A delayed category claim is preserved separately, not discarded or
        # used to merge either independently evidenced specific preference.
        self.assertEqual({row["subject"] for row in active}, {"아침 산책", "저녁 산책", "산책"})
        for key, original in before.items():
            self.assertEqual(self.rows[key], original)
        general = next(row for row in active if row["subject"] == "산책")
        self.assertNotIn(general["id"], before)
        assert_preference_record(self, general, subject="산책", content="산책 좋아해.", mentioned=0)
        self.assertEqual({row["content"] for row in self.store.select(self.rows, "산책")},
                         {"아침 산책 좋아해.", "저녁 산책 싫어해."})

    def test_newer_ambiguous_broad_target_cannot_merge_specifics(self):
        self.merge(candidate("아침 산책", "아침 산책 좋아해.", 100),
                   candidate("저녁 산책", "저녁 산책 싫어해.", 200))
        before = deepcopy(self.rows)
        active = self.merge(candidate("산책", "산책 좋아해.", 300))
        self.assertEqual(len(active), 3)
        for key, row in before.items():
            self.assertEqual(self.rows[key], row)

    def test_ambiguous_broad_update_preserves_exact_target_and_specifics(self):
        self.test_newer_ambiguous_broad_target_cannot_merge_specifics()
        specifics = {key: deepcopy(row) for key, row in self.rows.items() if row["subject"] != "산책"}
        active = self.merge(candidate("산책", "이제 산책 싫어해.", 400))
        self.assertEqual(len(active), 3)
        for key, row in specifics.items():
            self.assertEqual(self.rows[key], row)
        self.assertEqual(next(row for row in active if row["subject"] == "산책")["content"], "이제 산책 싫어해.")

    def test_compact_latest_target_uses_evidence_spacing_for_delayed_broad(self):
        self.merge(candidate("매운음식", "이제는 매운 음식 좋아해.", 200))
        before = deepcopy(self.rows)
        active = self.merge(candidate("음식", "매운 음식 싫어해.", 100))
        self.assertEqual(len(active), 1)
        self.assertEqual(self.rows, before)

    def test_spacing_and_broad_scope_keep_latest_for_either_arrival_order(self):
        rows = [candidate("매운음식", "이제는 매운 음식 좋아해.", 200),
                candidate("음식", "매운 음식 싫어해.", 100)]
        for order in itertools.permutations(rows):
            with self.subTest(subjects=[row["subject"] for row in order]):
                self.rows = {}
                active = self.merge(*order)
                self.assertEqual(len(active), 1)
                self.assertEqual(active[0]["content"], "이제는 매운 음식 좋아해.")
                self.assertEqual(active[0]["lastMentionedAt"], 200)

    def test_compact_multiple_specifics_are_ambiguous_using_evidence(self):
        self.merge(candidate("아침산책", "아침 산책 좋아해.", 100),
                   candidate("저녁산책", "저녁 산책 싫어해.", 200))
        before = deepcopy(self.rows)
        active = self.merge(candidate("산책", "산책 좋아해.", 0))
        # Spacing normalization cannot turn a category into either target.
        self.assertEqual(len(active), 3)
        for key, original in before.items():
            self.assertEqual(self.rows[key], original)
        general = next(row for row in active if row["subject"] == "산책")
        self.assertNotIn(general["id"], before)
        assert_preference_record(self, general, subject="산책", content="산책 좋아해.", mentioned=0)
        self.assertEqual({row["content"] for row in self.store.select(self.rows, "산책")},
                         {"아침 산책 좋아해.", "저녁 산책 싫어해."})

    def test_named_entity_substring_is_not_a_broad_subject(self):
        active = self.merge(candidate("김연자", "김연자 좋아해.", 100),
                            candidate("연자", "친구 연자 싫어해.", 200))
        self.assertEqual({row["subject"] for row in active}, {"김연자", "연자"})

    def test_normalization_handles_unicode_punctuation_and_whitespace(self):
        self.merge(candidate(unicodedata.normalize("NFD", "매운·음식"), "매운 음식 좋아해.", 100))
        active = self.merge(candidate("매운 \t 음식", "매운 음식 싫어해.", 200))
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["content"], "매운 음식 싫어해.")

    def test_identical_evidence_deduplicates_without_absorbing_other_specific(self):
        self.merge(candidate("아침 산책", "아침 산책 좋아해.", 100),
                   candidate("저녁 산책", "저녁 산책 싫어해.", 200))
        evening = deepcopy(next(row for row in self.rows.values() if row["subject"] == "저녁 산책"))
        active = self.merge(candidate("산책", "아침 산책 좋아해.", 100))
        self.assertEqual(len(active), 2)
        self.assertEqual({row["subject"] for row in active}, {"아침 산책", "저녁 산책"})
        self.assertEqual(self.rows[evening["id"]], evening)

    def test_delayed_duplicate_cannot_bridge_ambiguous_exact_and_specific_targets(self):
        self.merge(candidate("아침 산책", "아침 산책 좋아해.", 100),
                   candidate("저녁 산책", "저녁 산책 싫어해.", 200),
                   candidate("산책", "요즘은 산책 좋아해.", 300))
        before = deepcopy(self.rows)
        self.merge(candidate("산책", "아침 산책 좋아해.", 100))
        self.assertEqual(self.rows, before)


if __name__ == "__main__":
    unittest.main()
