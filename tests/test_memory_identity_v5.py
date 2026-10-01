"""Evidence ownership and conservative Korean preference identity regressions."""

from copy import deepcopy
import hashlib
import unittest
from unittest.mock import Mock, patch

from test_memory_identity_v3 import candidate
from persistent_memory import INITIAL_GENERATION, MemoryStore


class MemoryIdentityV5Tests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore("https://example.invalid", "elder_1", Mock())
        self.rows = {}
        self.wall_time = 10

    def merge(self, *candidates, session="session"):
        with patch("persistent_memory.time.time", return_value=self.wall_time):
            self.rows = self.store._merge(self.rows, list(candidates), session, INITIAL_GENERATION)
        return [row for row in self.rows.values() if row.get("status") == "active"]

    def assert_record(self, row, *, subject, content, mentioned, created=None, memory_id=None,
                      updated=10000, kind="preference", session="session"):
        self.assertEqual(self.rows[row["id"]], row)
        if memory_id is not None:
            self.assertEqual(row["id"], memory_id)
        self.assertEqual(row["subject"], subject)
        self.assertEqual(row["content"], content)
        self.assertEqual(row["evidenceKey"], hashlib.sha256(content.encode("utf-8")).hexdigest())
        self.assertEqual(row["lastMentionedAt"], mentioned)
        self.assertEqual(row["createdAt"], mentioned if created is None else created)
        self.assertEqual(row["updatedAt"], updated)
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["type"], kind)
        self.assertEqual(row["sourceSessionId"], session)
        self.assertEqual(row["source"], "conversation")
        self.assertEqual(row["elderId"], "elder_1")

    def test_a_particle_and_compact_subject_reject_delayed_conflict(self):
        current = self.merge(candidate("매운음식", "이제는 매운 음식을 좋아해.", 200))[0]
        before = deepcopy(self.rows)
        self.wall_time = 20
        active = self.merge(candidate("음식", "매운 음식을 싫어해.", 100))
        self.assertEqual(self.rows, before)
        self.assertEqual(len(active), 1)
        self.assert_record(active[0], memory_id=current["id"], subject="매운음식",
                           content="이제는 매운 음식을 좋아해.", mentioned=200)

    def test_b_duplicate_evidence_cannot_overwrite_existing_broad_preference(self):
        self.merge(candidate("아침 산책", "아침 산책 좋아해.", 100),
                   candidate("저녁 산책", "저녁 산책 싫어해.", 200),
                   candidate("산책", "요즘은 산책 좋아해.", 300))
        before = {row["subject"]: deepcopy(row) for row in self.rows.values()}
        self.wall_time = 20
        active = self.merge(candidate("산책", "아침 산책 좋아해.", 400), session="reextract")
        self.assertEqual(len(active), 3)
        self.assertEqual(len({row["evidenceKey"] for row in active}), 3)
        self.assertEqual(self.rows[before["산책"]["id"]], before["산책"])
        self.assertEqual(self.rows[before["저녁 산책"]["id"]], before["저녁 산책"])
        self.assert_record(self.rows[before["아침 산책"]["id"]],
                           memory_id=before["아침 산책"]["id"], subject="아침 산책",
                           content="아침 산책 좋아해.", mentioned=400, created=100,
                           updated=20000, session="reextract")
        self.assert_record(self.rows[before["저녁 산책"]["id"]], subject="저녁 산책",
                           content="저녁 산책 싫어해.", mentioned=200)
        self.assert_record(self.rows[before["산책"]["id"]], subject="산책",
                           content="요즘은 산책 좋아해.", mentioned=300)

    def test_c_distinct_named_entities_stay_separate(self):
        first = self.merge(candidate("김연자", "트로트 가수 김연자를 좋아해.", 100))[0]
        active = self.merge(candidate("나훈아", "트로트 가수 나훈아를 싫어해.", 200))
        self.assertEqual(len(active), 2)
        by_subject = {row["subject"]: row for row in active}
        self.assertNotEqual(by_subject["김연자"]["id"], by_subject["나훈아"]["id"])
        self.assert_record(by_subject["김연자"], memory_id=first["id"], subject="김연자",
                           content="트로트 가수 김연자를 좋아해.", mentioned=100)
        self.assert_record(by_subject["나훈아"], subject="나훈아",
                           content="트로트 가수 나훈아를 싫어해.", mentioned=200)

    def test_d_yoga_keeps_lexical_name_and_latest_identity(self):
        self.check_lexical_name("요가", "요가를 좋아해.", "요가 싫어해.")

    def test_e_cucumber_keeps_lexical_name_and_latest_identity(self):
        self.check_lexical_name("오이", "오이를 좋아해.", "오이 싫어해.")

    def check_lexical_name(self, subject, old, new):
        first = self.merge(candidate(subject, old, 100))[0]
        self.wall_time = 20
        active = self.merge(candidate(subject, new, 200))
        self.assertEqual(len(active), 1)
        self.assertEqual(set(self.rows), {first["id"]})
        self.assertEqual(active[0]["topicKey"], subject)
        self.assert_record(active[0], memory_id=first["id"], subject=subject,
                           content=new, mentioned=200, created=100, updated=20000)

    def test_f_same_evidence_keeps_owner_and_refines_supported_subject(self):
        content = "매운 음식을 좋아해."
        for first_subject, repeated_subject in (("음식", "매운 음식"), ("매운 음식", "음식")):
            with self.subTest(first_subject=first_subject):
                self.rows = {}
                first = self.merge(candidate(first_subject, content, 100))[0]
                active = self.merge(candidate(repeated_subject, content, 200))
                self.assertEqual(len(active), 1)
                self.assertEqual(set(self.rows), {first["id"]})
                # Evidence ownership fixes identity, not an inaccurate broad
                # label. Refinement changes neither the owner nor its fact.
                self.assertEqual(active[0]["topicKey"], "매운음식")
                self.assert_record(active[0], memory_id=first["id"], subject="매운 음식",
                                   content=content, mentioned=200, created=100)

    def test_g_delayed_broad_cannot_destroy_multiple_specific_targets(self):
        self.merge(candidate("아침산책", "아침 산책을 좋아해.", 200),
                   candidate("저녁산책", "저녁 산책을 싫어해.", 300))
        before = deepcopy(self.rows)
        self.wall_time = 20
        active = self.merge(candidate("산책", "산책을 좋아해.", 100))
        # The former discard policy also lost the general evidence. Preserve
        # it separately and withhold its unresolved scope only from recall.
        self.assertEqual(len(active), 3)
        for key, original in before.items():
            self.assertEqual(self.rows[key], original)
        for row in before.values():
            self.assert_record(row, memory_id=row["id"], subject=row["subject"],
                               content=row["content"], mentioned=row["lastMentionedAt"])
        general = next(row for row in active if row["subject"] == "산책")
        self.assertNotIn(general["id"], before)
        self.assert_record(general, subject="산책", content="산책을 좋아해.",
                           mentioned=100, updated=20000)
        self.assertEqual({row["content"] for row in self.store.select(self.rows, "산책")},
                         {"아침 산책을 좋아해.", "저녁 산책을 싫어해."})

    def test_evidence_ownership_cannot_cross_memory_types(self):
        content = "아침 산책 좋아해."
        originals = {}
        for kind in ("episodic", "profile", "preference"):
            self.merge(candidate("아침 산책", content, 100, kind))
            originals[kind] = next(row["id"] for row in self.rows.values() if row["type"] == kind)
        active = self.merge(candidate("산책", content, 200))
        self.assertEqual(len(active), 3)
        self.assertEqual(len(set(originals.values())), 3)
        for row in active:
            self.assert_record(row, memory_id=originals[row["type"]], subject="아침 산책",
                               content=content, mentioned=200 if row["type"] == "preference" else 100,
                               created=100, kind=row["type"])

    def test_normalized_duplicate_routes_to_owner_despite_different_raw_digest(self):
        self.test_b_duplicate_evidence_cannot_overwrite_existing_broad_preference()
        broad = deepcopy(next(row for row in self.rows.values() if row["subject"] == "산책"))
        owner = deepcopy(next(row for row in self.rows.values() if row["subject"] == "아침 산책"))
        self.wall_time = 30
        active = self.merge(candidate("산책", "아침  산책 좋아해!", 500))
        self.assertEqual(len(active), 3)
        self.assertEqual(self.rows[broad["id"]], broad)
        self.assert_record(self.rows[owner["id"]], memory_id=owner["id"], subject="아침 산책",
                           content="아침  산책 좋아해!", mentioned=500, created=100, updated=30000)

    def test_same_captured_evidence_replay_is_idempotent_after_wall_clock_advances(self):
        self.merge(candidate("아침 산책", "아침 산책 좋아해.", 100),
                   candidate("저녁 산책", "저녁 산책 싫어해.", 200),
                   candidate("산책", "요즘은 산책 좋아해.", 300))
        before = deepcopy(self.rows)
        self.wall_time = 20
        self.merge(candidate("산책", "아침 산책 좋아해.", 100), session="retry")
        self.assertEqual(self.rows, before)

    def test_replaying_evidence_does_not_absorb_general_record_when_only_one_specific_exists(self):
        # Imported rows may already contain both a true general statement and
        # a specific statement. The general statement is not an evidence alias.
        self.merge(candidate("아침 산책", "아침 산책 좋아해.", 100))
        broad_rows = self.store._merge({}, [candidate("산책", "요즘은 산책 좋아해.", 300)],
                                      "session", INITIAL_GENERATION)
        self.rows.update(broad_rows)
        before = deepcopy(self.rows)
        owner = next(row for row in self.rows.values() if row["subject"] == "아침 산책")
        active = self.merge(candidate("산책", "아침 산책 좋아해.", 400))
        self.assertEqual(len(active), 2)
        self.assert_record(self.rows[owner["id"]], memory_id=owner["id"], subject="아침 산책",
                           content="아침 산책 좋아해.", mentioned=400, created=100)
        broad = next(row for row in before.values() if row["subject"] == "산책")
        self.assertEqual(self.rows[broad["id"]], broad)


if __name__ == "__main__":
    unittest.main()
