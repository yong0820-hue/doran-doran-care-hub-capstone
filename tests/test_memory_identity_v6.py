"""M2 identity regressions through extraction validation and RTDB publication.

Only the model response and HTTP transport are fakes. Every candidate passes
MemoryExtractor.extract(), MemoryStore.save(), manifest CAS, and a body read.
"""

from copy import deepcopy
import hashlib
import itertools
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "raspberry-pi-voice-hub"))
from memory_protocol_fake import ProtocolFirebase
from persistent_memory import INITIAL_GENERATION, MemoryExtractor, MemoryStore


BASE = 1_800_000_000_000
FIXTURES = (
    ("음식", "매운 음식", "단 음식", "매운 음식을 좋아해.", "단 음식을 싫어해.", "매운 음식을 싫어해."),
    ("음악", "클래식 음악", "재즈 음악", "클래식 음악을 좋아해.", "재즈 음악을 싫어해.", "클래식 음악을 싫어해."),
)


class MemoryIdentityV6Tests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.extractor = MemoryExtractor(self.client, "fake-model")
        self.http = ProtocolFirebase({"_control": {"generation": INITIAL_GENERATION, "activeIds": {}}})
        self.store = MemoryStore("https://example.invalid", "elder_1", self.http)
        self.serial = 0
        self.foreign = {}
        self.unrelated = None
        self.publish("바둑", "바둑을 좋아해.", 50)
        self.unrelated = deepcopy(next(iter(self.records().values())))
        # These records deliberately resemble the affected preference. Neither
        # evidence deduplication nor target correction owns their source/elder.
        for identity, source, elder in (("manual", "user", "elder_1"),
                                        ("assistant", "ai", "elder_1"),
                                        ("other_elder", "conversation", "elder_2")):
            self.foreign[identity] = {
                "id": identity, "elderId": elder, "source": source, "type": "preference",
                "subject": "음식", "content": "매운 음식을 좋아해.", "status": "active",
                "importance": 3, "createdAt": BASE + 25, "lastMentionedAt": BASE + 25,
                "evidenceKey": hashlib.sha256("매운 음식을 좋아해.".encode()).hexdigest(),
            }
        self.http.root["active"].update(deepcopy(self.foreign))
        self.http.root["_control"]["activeIds"].update(dict.fromkeys(self.foreign, True))

    def records(self):
        bodies = self.store.active_read(self.http.root["_control"])
        result = {row["id"]: row for row in bodies.values()}
        self.assertEqual(len(result), len(bodies), "logical IDs must remain unique")
        return result

    def relevant(self, kind="preference"):
        unrelated_id = self.unrelated["id"] if self.unrelated else None
        return {key: value for key, value in self.records().items()
                if key != unrelated_id and value["type"] == kind}

    def publish(self, subject, content, timestamp, kind="preference"):
        self.serial += 1
        self.client.responses.create.return_value = Mock(status="completed", output_text=json.dumps({
            "memories": [{"type": kind, "subject": subject, "evidence": content,
                          "messageIndex": 0, "importance": 3}],
        }, ensure_ascii=False))
        with patch("persistent_memory.time.time", return_value=(BASE + 10000 + self.serial) / 1000):
            candidates = self.extractor.extract([{"text": content, "createdAt": BASE + timestamp}])
            self.assertEqual(len(candidates), 1, "fixture must pass real extraction validation")
            self.assertEqual(candidates[0]["content"], content)
            self.assertEqual(candidates[0]["lastMentionedAt"], BASE + timestamp)
            self.assertEqual(candidates[0]["evidenceKey"], hashlib.sha256(content.encode()).hexdigest())
            self.assertTrue(self.store.save(candidates, "session_" + str(self.serial), INITIAL_GENERATION))
        self.assert_preserved()
        return self.records()

    def assert_preserved(self):
        for storage_id, row in self.foreign.items():
            self.assertTrue(self.http.root["_control"]["activeIds"].get(storage_id))
            self.assertEqual(self.http.root["active"].get(storage_id), row)
        if self.unrelated:
            self.assertEqual(self.records().get(self.unrelated["id"]), self.unrelated)
        self.assertEqual(self.http.root["_control"]["generation"], INITIAL_GENERATION)
        self.assertFalse(any(url == self.store.url for url, _ in self.http.get_calls))
        self.assertFalse(any(url == self.store.url for url, *_ in self.http.put_calls))
        self.assertTrue(any("/active/" in url for url, *_ in self.http.put_calls))
        self.assertTrue(any("/_control.json" in url and "if-match" in headers
                            for url, _body, headers, _kwargs in self.http.put_calls))

    def assert_row(self, row, *, subject, content, timestamp, identity=None, created=None, kind="preference"):
        self.assertIsNotNone(row)
        if identity is not None:
            self.assertEqual(row["id"], identity)
        self.assertEqual(row["subject"], subject)
        self.assertEqual(row["content"], content)
        self.assertEqual(row["evidenceKey"], hashlib.sha256(content.encode()).hexdigest())
        self.assertEqual(row["lastMentionedAt"], BASE + timestamp)
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["type"], kind)
        self.assertEqual(row["elderId"], "elder_1")
        self.assertEqual(row["source"], "conversation")
        if created is not None:
            self.assertEqual(row["createdAt"], BASE + created)
        if kind == "preference":
            self.assertEqual(row["topicKey"], subject.replace(" ", ""))
        self.assertEqual(self.records()[row["id"]], row)

    def by_content(self, content, kind="preference"):
        matches = [row for row in self.relevant(kind).values() if row["content"] == content]
        self.assertEqual(len(matches), 1, content)
        return matches[0]

    def test_original_broad_repeat_refines_owner_and_preserves_independent_target(self):
        for broad, first, second, positive, negative, _correction in FIXTURES:
            with self.subTest(broad=broad):
                self.setUp()
                self.publish(broad, positive, 100)
                original = self.by_content(positive)
                self.publish(first, positive, 200)
                self.assertEqual(len(self.relevant()), 1)
                self.assert_row(self.by_content(positive), identity=original["id"], subject=first,
                                content=positive, timestamp=200, created=100)
                self.publish(second, negative, 300)
                self.assertEqual(len(self.relevant()), 2)
                self.assert_row(self.by_content(positive), identity=original["id"], subject=first,
                                content=positive, timestamp=200, created=100)
                self.assert_row(self.by_content(negative), subject=second, content=negative, timestamp=300, created=300)

    def test_no_intermediate_refinement_cannot_lose_first_target(self):
        for broad, _first, second, positive, negative, _correction in FIXTURES:
            with self.subTest(broad=broad):
                self.setUp()
                self.publish(broad, positive, 100)
                original = deepcopy(self.by_content(positive))
                self.publish(second, negative, 300)
                self.assertEqual(len(self.relevant()), 2)
                self.assertEqual(self.records().get(original["id"]), original)
                self.assert_row(self.by_content(negative), subject=second, content=negative, timestamp=300)

    def test_all_six_arrival_orders_preserve_both_targets_and_latest_evidence(self):
        for broad, first, second, positive, negative, _correction in FIXTURES:
            events = ((broad, positive, 100), (first, positive, 200), (second, negative, 300))
            for order in itertools.permutations(events):
                with self.subTest(broad=broad, order=[row[2] for row in order]):
                    self.setUp()
                    first_id = second_id = None
                    created = next(event[2] for event in order if event[1] == positive)
                    for subject, content, timestamp in order:
                        self.publish(subject, content, timestamp)
                        if content == positive and first_id is None:
                            first_id = self.by_content(positive)["id"]
                        if content == negative and second_id is None:
                            second_id = self.by_content(negative)["id"]
                    self.assertEqual(len(self.relevant()), 2)
                    self.assertNotEqual(first_id, second_id)
                    self.assert_row(self.by_content(positive), identity=first_id, subject=first,
                                    content=positive, timestamp=200, created=created)
                    self.assert_row(self.by_content(negative), identity=second_id, subject=second,
                                    content=negative, timestamp=300, created=300)

    def test_latest_same_target_correction_only_changes_its_owner(self):
        for broad, first, second, positive, negative, correction in FIXTURES:
            with self.subTest(broad=broad):
                self.setUp()
                self.publish(broad, positive, 100)
                original_id = self.by_content(positive)["id"]
                self.publish(first, positive, 200)
                self.publish(second, negative, 300)
                independent = deepcopy(self.by_content(negative))
                self.publish(first, correction, 400)
                self.assertEqual(len(self.relevant()), 2)
                self.assert_row(self.by_content(correction), identity=original_id, subject=first,
                                content=correction, timestamp=400, created=100)
                self.assertEqual(self.records()[independent["id"]], independent)
                before = deepcopy(self.records())
                self.publish(broad, positive, 100)
                self.publish(first, positive, 200)
                self.assertEqual(self.records(), before)

    def test_same_broad_subject_with_different_explicit_modifiers_stays_separate(self):
        for broad, _first, _second, positive, negative, _correction in FIXTURES:
            with self.subTest(broad=broad):
                self.setUp()
                self.publish(broad, positive, 100)
                original = deepcopy(self.by_content(positive))
                self.publish(broad, negative, 300)
                self.assertEqual(len(self.relevant()), 2)
                self.assertEqual(self.records().get(original["id"]), original)
                self.assert_row(self.by_content(negative), subject=broad, content=negative, timestamp=300)

    def test_broad_subject_correction_without_refinement_preserves_independent_target(self):
        for broad, _first, second, positive, negative, correction in FIXTURES:
            with self.subTest(broad=broad):
                self.setUp()
                self.publish(broad, positive, 100)
                original_id = self.by_content(positive)["id"]
                self.publish(second, negative, 300)
                independent = deepcopy(self.by_content(negative))
                self.publish(broad, correction, 400)
                self.assertEqual(len(self.relevant()), 2)
                self.assert_row(self.by_content(correction), identity=original_id, subject=broad,
                                content=correction, timestamp=400, created=100)
                self.assertEqual(self.records()[independent["id"]], independent)
                before = deepcopy(self.records())
                self.publish(broad, positive, 100)
                self.assertEqual(self.records(), before)

    def test_repeated_broad_and_specific_evidence_does_not_steal_general_preference(self):
        for broad, first, second, positive, negative, _correction in FIXTURES:
            with self.subTest(broad=broad):
                self.setUp()
                self.publish(first, positive, 100)
                original_id = self.by_content(positive)["id"]
                self.publish(second, negative, 200)
                general_content = broad + "을 좋아해."
                self.publish(broad, general_content, 300)
                general = deepcopy(self.by_content(general_content))
                independent = deepcopy(self.by_content(negative))
                for index in range(12):
                    self.publish(broad if index % 2 == 0 else first, positive, 400 + index)
                    self.assertEqual(len(self.relevant()), 3)
                    self.assertEqual(self.records()[general["id"]], general)
                    self.assertEqual(self.records()[independent["id"]], independent)
                    self.assert_row(self.by_content(positive), identity=original_id, subject=first,
                                    content=positive, timestamp=400 + index, created=100)

    def test_named_singers_never_merge_via_category(self):
        self.publish("김연자", "트로트 가수 김연자를 좋아해.", 100)
        original = deepcopy(self.by_content("트로트 가수 김연자를 좋아해."))
        self.publish("나훈아", "트로트 가수 나훈아를 싫어해.", 200)
        self.assertEqual(len(self.relevant()), 2)
        self.assertEqual(self.records()[original["id"]], original)
        self.assert_row(self.by_content("트로트 가수 나훈아를 싫어해."), subject="나훈아",
                        content="트로트 가수 나훈아를 싫어해.", timestamp=200)

    def test_named_singers_stay_independent_when_first_subject_is_category(self):
        self.publish("트로트", "트로트 가수 김연자를 좋아해.", 100)
        original = deepcopy(self.by_content("트로트 가수 김연자를 좋아해."))
        self.publish("나훈아", "트로트 가수 나훈아를 싫어해.", 200)
        self.assertEqual(len(self.relevant()), 2)
        self.assertEqual(self.records()[original["id"]], original)
        self.assert_row(self.by_content("트로트 가수 나훈아를 싫어해."), subject="나훈아",
                        content="트로트 가수 나훈아를 싫어해.", timestamp=200)

    def test_yoga_and_cucumber_keep_lexical_subject_and_correct_in_place(self):
        for subject in ("요가", "오이"):
            with self.subTest(subject=subject):
                self.setUp()
                positive, negative = subject + "를 좋아해.", subject + " 싫어해."
                self.publish(subject, positive, 100)
                identity = self.by_content(positive)["id"]
                self.publish(subject, negative, 200)
                self.assertEqual(len(self.relevant()), 1)
                self.assert_row(self.by_content(negative), identity=identity, subject=subject,
                                content=negative, timestamp=200, created=100)

    def test_compact_latest_and_delayed_particle_broad_keep_latest_in_both_orders(self):
        events = (("매운음식", "이제는 매운 음식을 좋아해.", 200),
                  ("음식", "매운 음식을 싫어해.", 100))
        for order in itertools.permutations(events):
            with self.subTest(order=[event[2] for event in order]):
                self.setUp()
                for subject, content, timestamp in order:
                    self.publish(subject, content, timestamp)
                self.assertEqual(len(self.relevant()), 1)
                self.assert_row(self.by_content(events[0][1]), subject="매운음식",
                                content=events[0][1], timestamp=200)

    def test_walk_specifics_and_general_preserve_each_id_through_general_correction(self):
        self.publish("아침 산책", "아침 산책 좋아해.", 100)
        self.publish("저녁 산책", "저녁 산책 싫어해.", 200)
        specifics = deepcopy(self.relevant())
        self.publish("산책", "요즘은 산책 좋아해.", 300)
        general_id = self.by_content("요즘은 산책 좋아해.")["id"]
        self.publish("산책", "이제 산책 싫어해.", 400)
        self.assertEqual(len(self.relevant()), 3)
        for identity, row in specifics.items():
            self.assertEqual(self.records()[identity], row)
        self.assert_row(self.by_content("이제 산책 싫어해."), identity=general_id, subject="산책",
                        content="이제 산책 싫어해.", timestamp=400, created=300)

    def test_duplicate_walk_evidence_updates_owner_and_preserves_general(self):
        self.publish("아침 산책", "아침 산책 좋아해.", 100)
        owner_id = self.by_content("아침 산책 좋아해.")["id"]
        self.publish("저녁 산책", "저녁 산책 싫어해.", 200)
        self.publish("산책", "요즘은 산책 좋아해.", 300)
        preserved = {key: deepcopy(row) for key, row in self.relevant().items() if key != owner_id}
        self.publish("산책", "아침 산책 좋아해.", 400)
        self.assertEqual(len(self.relevant()), 3)
        for identity, row in preserved.items():
            self.assertEqual(self.records()[identity], row)
        self.assert_row(self.by_content("아침 산책 좋아해."), identity=owner_id, subject="아침 산책",
                        content="아침 산책 좋아해.", timestamp=400, created=100)

    def test_delayed_broad_cannot_archive_or_remove_multiple_specifics(self):
        for compact in (False, True):
            with self.subTest(compact=compact):
                self.setUp()
                self.publish("아침산책" if compact else "아침 산책", "아침 산책을 좋아해.", 200)
                self.publish("저녁산책" if compact else "저녁 산책", "저녁 산책을 싫어해.", 300)
                before = deepcopy(self.records())
                self.publish("산책", "산책을 좋아해.", 100)
                self.assertEqual(len(self.relevant()), 3)
                for identity, row in before.items():
                    self.assertEqual(self.records()[identity], row)
                self.assert_row(self.by_content("산책을 좋아해."), subject="산책",
                                content="산책을 좋아해.", timestamp=100, created=100)
                recalled = self.store.select(self.records(), "산책", now_ms=BASE + 1000)
                recalled_content = {row["content"] for row in recalled}
                self.assertNotIn("산책을 좋아해.", recalled_content)
                self.assertTrue({"아침 산책을 좋아해.", "저녁 산책을 싫어해."} <= recalled_content)

    def test_unknown_same_subject_conflicts_are_preserved_but_not_both_recalled(self):
        # The narrow target parser cannot establish identity for these forms.
        # Store both observations, and withhold unresolved contradictory scope
        # from the conversation context instead of converting either into truth.
        positive, negative = "요가가 마음에 들어.", "요가가 마음에 들지 않아."
        self.publish("요가", positive, 100)
        original = deepcopy(self.by_content(positive))
        self.publish("요가", negative, 200)
        self.assertEqual(len(self.relevant()), 2)
        self.assertEqual(self.records()[original["id"]], original)
        self.assert_row(self.by_content(negative), subject="요가", content=negative, timestamp=200, created=200)
        recalled = self.store.select(self.records(), "요가", now_ms=BASE + 1000)
        self.assertLessEqual(len({positive, negative} & {row["content"] for row in recalled}), 1)

    def test_clear_independent_targets_remain_available_together_in_recall(self):
        for broad, first, second, positive, negative, _correction in FIXTURES:
            with self.subTest(broad=broad):
                self.setUp()
                self.publish(broad, positive, 100)
                first_id = self.by_content(positive)["id"]
                self.publish(first, positive, 200)
                self.publish(second, negative, 300)
                self.assertEqual(len(self.relevant()), 2)
                self.assert_row(self.by_content(positive), identity=first_id, subject=first,
                                content=positive, timestamp=200, created=100)
                self.assert_row(self.by_content(negative), subject=second, content=negative, timestamp=300)
                recalled = self.store.select(self.records(), broad, now_ms=BASE + 1000)
                self.assertTrue({positive, negative} <= {row["content"] for row in recalled})

    def test_compound_evidence_cannot_refine_owner_to_one_partial_target(self):
        content = "매운 음식은 싫고 단 음식은 좋아해."
        self.publish("매운 음식", "매운 음식을 좋아해.", 300)
        independent = deepcopy(self.by_content("매운 음식을 좋아해."))
        self.publish("음식", content, 100)
        owner_id = self.by_content(content)["id"]
        self.publish("단 음식", content, 200)
        self.assertEqual(len(self.relevant()), 2)
        self.assertEqual(self.records()[independent["id"]], independent)
        self.assert_row(self.by_content(content), identity=owner_id, subject="음식",
                        content=content, timestamp=200, created=100)

    def test_compact_evidence_keeps_full_target_across_spacing_correction(self):
        self.publish("장미", "붉은장미를 좋아해.", 100)
        first_id = self.by_content("붉은장미를 좋아해.")["id"]
        self.publish("하얀 장미", "하얀 장미를 좋아해.", 200)
        independent = deepcopy(self.by_content("하얀 장미를 좋아해."))
        self.publish("붉은 장미", "붉은 장미를 싫어해.", 300)
        self.assertEqual(len(self.relevant()), 2)
        self.assert_row(self.by_content("붉은 장미를 싫어해."), identity=first_id,
                        subject="붉은 장미", content="붉은 장미를 싫어해.", timestamp=300, created=100)
        self.assertEqual(self.records()[independent["id"]], independent)
        self.publish("장미", "붉은장미를 좋아해.", 100)
        self.assertEqual(len(self.relevant()), 2)
        self.assert_row(self.by_content("붉은 장미를 싫어해."), identity=first_id,
                        subject="붉은 장미", content="붉은 장미를 싫어해.", timestamp=300, created=100)

    def test_coordinated_targets_cannot_refine_to_one_member_or_hide_its_correction(self):
        cases = (
            ("음식", "단 음식", "매운 음식과 단 음식을 좋아해.", "단 음식을 싫어해."),
            ("음악", "재즈 음악", "클래식 음악과 재즈 음악을 좋아해.", "재즈 음악을 싫어해."),
            ("물", "물", "차와 물을 좋아해.", "물을 싫어해."),
            ("풀", "풀", "꽃과 풀을 좋아해.", "풀을 싫어해."),
            ("장미", "하얀 장미", "붉은 장미 대신 하얀 장미를 좋아해.", "하얀 장미를 싫어해."),
            ("장미", "하얀 장미", "붉은 장미 말고 하얀 장미를 좋아해.", "하얀 장미를 싫어해."),
        )
        for broad, specific, group, correction in cases:
            with self.subTest(group=group):
                self.setUp()
                self.publish(broad, group, 100)
                owner = self.by_content(group)["id"]
                self.publish(specific, group, 200)
                self.assert_row(self.by_content(group), identity=owner, subject=broad,
                                content=group, timestamp=200, created=100)
                self.publish(specific, correction, 300)
                self.assertEqual(len(self.relevant()), 2)
                self.assert_row(self.by_content(correction), subject=specific,
                                content=correction, timestamp=300, created=300)
                recalled = {row["content"] for row in self.store.select(self.records(), specific, now_ms=BASE + 1000)}
                self.assertNotIn(group, recalled)

    def test_topic_particle_lookalike_name_cannot_merge_or_bridge_evidence_owners(self):
        for first_label in ("가수", "이상은"):
            with self.subTest(first_label=first_label):
                self.setUp()
                self.publish(first_label, "가수 이상은 좋아해.", 100)
                first_id = self.by_content("가수 이상은 좋아해.")["id"]
                self.publish("이상", "가수 이상 싫어해.", 200)
                other = deepcopy(self.by_content("가수 이상 싫어해."))
                self.assertEqual(len(self.relevant()), 2)
                self.publish("가수", "가수 이상은 좋아해.", 300)
                self.assertEqual(len(self.relevant()), 2)
                self.assertEqual(self.records()[other["id"]], other)
                self.assert_row(self.by_content("가수 이상은 좋아해."), identity=first_id,
                                subject=first_label, content="가수 이상은 좋아해.", timestamp=300, created=100)

    def test_partial_word_extractor_label_cannot_refine_evidence_owner(self):
        self.publish("음식", "매운 음식을 좋아해.", 100)
        owner = self.by_content("매운 음식을 좋아해.")["id"]
        self.publish("운 음식", "매운 음식을 좋아해.", 200)
        self.assertEqual(len(self.relevant()), 1)
        self.assert_row(self.by_content("매운 음식을 좋아해."), identity=owner,
                        subject="음식", content="매운 음식을 좋아해.", timestamp=200, created=100)

    def test_evidence_ownership_never_crosses_profile_episodic_preference(self):
        content = "아침 산책 좋아해."
        originals = {}
        for kind in ("episodic", "profile", "preference"):
            self.publish("아침 산책", content, 100, kind)
            originals[kind] = deepcopy(self.by_content(content, kind))
        self.publish("산책", content, 200)
        self.assertEqual(len(self.records()), 4)
        self.assertEqual(len({row["id"] for row in originals.values()}), 3)
        for kind, original in originals.items():
            if kind != "preference":
                self.assertEqual(self.records()[original["id"]], original)
            self.assert_row(self.by_content(content, kind), identity=original["id"], subject="아침 산책",
                            content=content, timestamp=200 if kind == "preference" else 100, created=100, kind=kind)

    def test_same_captured_evidence_replay_is_noop_even_with_changed_subject(self):
        self.publish("매운 음식", "매운 음식을 좋아해.", 200)
        before = deepcopy(self.records())
        for subject in ("음식", "매운음식", "매운 음식"):
            self.publish(subject, "매운 음식을 좋아해.", 200)
            self.assertEqual(self.records(), before)


if __name__ == "__main__":
    unittest.main()
