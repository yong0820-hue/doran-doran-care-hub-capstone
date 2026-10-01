"""Recall label invariance through the real extractor/storage/cache/dispatch path.

The shared helpers mock only the extraction response and the RTDB HTTP transport.
Every positive fixture must pass the unchanged extractor validator before save.
"""

from copy import deepcopy
import itertools
import json
import re
import unittest
from unittest.mock import Mock, patch

import test_memory_recall_corrections as corrections


BASE = corrections.BASE
QUERIES = corrections.QUERIES
PAST_LABELS = ("김연자", "가수 김연자", "트로트 가수 김연자")
LATEST_FORMS = (("김연자 {polarity}.", ("김연자",)),
                ("가수 김연자를 {polarity}.", ("김연자", "가수 김연자")),
                ("나는 트로트 가수 김연자를 {polarity}.", PAST_LABELS))
UNRELATED = "바둑을 좋아해."


class MemoryRecallLabelTests(unittest.TestCase):
    # Reuse path assertions without inheriting the old suite's test methods.
    setUp = corrections.MemoryRecallCorrectionTests.setUp
    new_scenario = corrections.MemoryRecallCorrectionTests.new_scenario
    publish = corrections.MemoryRecallCorrectionTests.publish
    records = corrections.MemoryRecallCorrectionTests.records
    assert_protocol = corrections.MemoryRecallCorrectionTests.assert_protocol
    warm = corrections.MemoryRecallCorrectionTests.warm
    dispatch = corrections.MemoryRecallCorrectionTests.dispatch
    assert_current = corrections.MemoryRecallCorrectionTests.assert_current

    def test_valid_label_polarity_arrival_query_matrix_reaches_callback(self):
        payload_cases = 0
        for old_label, latest_form, polarities, latest_first in itertools.product(
                PAST_LABELS, LATEST_FORMS, (("좋아해", "싫어해"), ("싫어해", "좋아해")), (False, True)):
            current_template, current_labels = latest_form
            previous = f"나는 트로트 가수 김연자를 {polarities[0]}."
            current = current_template.format(polarity=polarities[1])
            for current_label in current_labels:
                with self.subTest(old_label=old_label, current_label=current_label,
                                  current=current, latest_first=latest_first):
                    self.new_scenario()
                    events = [(old_label, previous, 100), (current_label, current, 200)]
                    for event in reversed(events) if latest_first else events:
                        self.publish(*event)
                    self.publish("바둑", UNRELATED, 50)
                    stored = deepcopy(self.http.root)
                    self.warm()
                    for query in QUERIES:
                        with self.subTest(query=query):
                            payload_cases += 1
                            _messages, rows = self.assert_current(query, current, previous)
                            self.assertIn(UNRELATED, {row["content"] for row in rows})
                            self.assertEqual(self.http.root, stored,
                                             "all saved IDs/bodies/evidence/times must survive recall")
        self.assertEqual(payload_cases, 216)

    def test_invalid_subject_evidence_combinations_remain_rejected(self):
        cases = (("가수 김연자", "김연자 싫어해."),
                 ("트로트 가수 김연자", "김연자 싫어해."),
                 ("트로트 가수 김연자", "가수 김연자를 싫어해."),
                 ("영화 배우 안성기", "안성기 좋아해."),
                 ("소설 작가 박완서", "박완서 싫어해."))
        for subject, content in cases:
            with self.subTest(subject=subject, content=content):
                before = deepcopy(self.http.root)
                self.client.responses.create.return_value = Mock(status="completed", output_text=json.dumps({
                    "memories": [{"type": "preference", "subject": subject, "evidence": content,
                                  "messageIndex": 0, "importance": 3}],
                }, ensure_ascii=False))
                candidates = self.extractor.extract([{"text": content, "createdAt": BASE + 200}])
                self.assertEqual(candidates, [])
                self.assertEqual(self.http.root, before)
                self.assertEqual(self.http.put_calls, [])

    def test_other_supported_roles_are_label_invariant(self):
        cases = (("안성기", "영화 배우", "안성기를"),
                 ("박완서", "소설 작가", "박완서를"),
                 ("아이유", "가수", "아이유를"))
        for name, description, object_form in cases:
            labels = (name, f"{description.split()[-1]} {name}", f"{description} {name}")
            for label, polarities, latest_first in itertools.product(
                    dict.fromkeys(labels), (("좋아해", "싫어해"), ("싫어해", "좋아해")), (False, True)):
                with self.subTest(label=label, polarity=polarities, latest_first=latest_first):
                    self.new_scenario()
                    previous = f"나는 {description} {object_form} {polarities[0]}."
                    current = f"{name} {polarities[1]}."
                    events = [(label, previous, 100), (name, current, 200)]
                    for event in reversed(events) if latest_first else events:
                        self.publish(*event)
                    self.publish("바둑", UNRELATED, 50)
                    self.warm()
                    _messages, rows = self.assert_current(f"{name} 취향", current, previous)
                    self.assertIn(UNRELATED, {row["content"] for row in rows})

    def test_same_description_different_people_are_independent(self):
        cases = (("트로트 가수 김연자", "나는 트로트 가수 김연자를 좋아해.",
                  "가수 나훈아", "나는 트로트 가수 나훈아를 싫어해."),
                 ("영화 배우 안성기", "나는 영화 배우 안성기를 좋아해.",
                  "배우 송강호", "나는 영화 배우 송강호를 싫어해."),
                 ("소설 작가 박완서", "나는 소설 작가 박완서를 좋아해.",
                  "작가 한강", "나는 소설 작가 한강을 싫어해."),
                 ("가수 김민", "가수 김민을 좋아해.",
                  "가수 김민수", "가수 김민수를 싫어해."),
                 ("가수 존 레논", "가수 존 레논을 좋아해.",
                  "가수 레논", "가수 레논을 싫어해."),
                 ("가수 김연자", "가수 김연자를 좋아해.",
                  "가수 연자", "가수 연자를 싫어해."),
                 ("가수", "가수 김연자를 좋아해.",
                  "가수", "가수 나훈아를 싫어해."))
        for first_label, first, second_label, second in cases:
            for latest_first in (False, True):
                with self.subTest(first=first, second=second, latest_first=latest_first):
                    self.new_scenario()
                    events = [(first_label, first, 100), (second_label, second, 200)]
                    for event in reversed(events) if latest_first else events:
                        self.publish(*event)
                    self.assertEqual(len(self.records()), 2)
                    self.warm()
                    _messages, rows = self.dispatch(f"{first_label} {second_label} 취향")
                    self.assertEqual({row["content"] for row in rows}, {first, second})

    def test_full_multitoken_and_particle_ending_names_keep_current_correction(self):
        cases = (("가수 존 레논", "가수 존 레논을 좋아해.", "존 레논", "존 레논 싫어해."),
                 ("가수 이상은", "가수 이상은을 좋아해.", "이상은", "이상은 싫어해."))
        for old_label, previous, current_label, current in cases:
            for latest_first in (False, True):
                with self.subTest(old_label=old_label, latest_first=latest_first):
                    self.new_scenario()
                    events = [(old_label, previous, 100), (current_label, current, 200)]
                    for event in reversed(events) if latest_first else events:
                        self.publish(*event)
                    self.publish("바둑", UNRELATED, 50)
                    self.warm()
                    _messages, rows = self.assert_current(f"{current_label} 취향", current, previous)
                    self.assertIn(UNRELATED, {row["content"] for row in rows})

    def test_incomplete_name_label_does_not_prove_a_role_name_identity(self):
        # The existing lexical validator accepts this substring. It is not one
        # of the complete role/name spans supported by recall disambiguation.
        self.publish("연자", "트로트 가수 김연자를 좋아해.", 100)
        self.publish("김연자", "김연자 싫어해.", 200)
        self.publish("바둑", UNRELATED, 50)
        self.assertEqual(len(self.records()), 3)
        self.warm()
        _messages, rows = self.dispatch(QUERIES[0])
        self.assertEqual({row["content"] for row in rows}, {UNRELATED})

    def test_competing_roles_and_conditions_still_need_disambiguation(self):
        cases = (("배우 김민수", "배우 김민수를 좋아해.",
                  "축구 선수 김민수", "축구 선수 김민수를 싫어해."),
                 ("아침 가수 김연자", "아침 가수 김연자를 좋아해.",
                  "김연자", "김연자 싫어해."))
        for first_label, previous, latest_label, current in cases:
            with self.subTest(previous=previous):
                self.new_scenario()
                self.publish(first_label, previous, 100)
                self.publish(latest_label, current, 200)
                self.publish("바둑", UNRELATED, 50)
                self.warm()
                _messages, rows = self.dispatch(f"{latest_label} 취향")
                self.assertEqual({row["content"] for row in rows}, {UNRELATED})

    def test_conditions_categories_and_attributes_stay_independent(self):
        cases = (("매운 음식", "매운 음식을 좋아해.", "단 음식", "단 음식을 싫어해."),
                 ("아침 산책", "아침 산책을 좋아해.", "저녁 산책", "저녁 산책을 싫어해."),
                 ("출근길 가수 김연자", "출근길 가수 김연자를 좋아해.",
                  "퇴근길 가수 김연자", "퇴근길 가수 김연자를 싫어해."),
                 ("김연자의 노래", "김연자의 노래를 좋아해.",
                  "김연자의 목소리", "김연자의 목소리를 싫어해."))
        for first_label, first, second_label, second in cases:
            with self.subTest(first=first, second=second):
                self.new_scenario()
                self.publish(first_label, first, 100)
                self.publish(second_label, second, 200)
                self.assertEqual(len(self.records()), 2)
                self.warm()
                _messages, rows = self.dispatch(f"{first_label} {second_label} 취향")
                self.assertEqual({row["content"] for row in rows}, {first, second})

    def test_short_lexical_nouns_keep_latest_corrections(self):
        for subject in ("요가", "오이"):
            with self.subTest(subject=subject):
                self.new_scenario()
                previous, current = f"{subject}를 좋아해.", f"{subject}는 싫어해."
                self.publish(subject, previous, 100)
                original_id = next(iter(self.records().values()))["id"]
                self.publish(subject, current, 200)
                self.publish(subject, previous, 100)
                records = list(self.records().values())
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["id"], original_id)
                self.assertEqual(records[0]["subject"], subject)
                self.warm()
                self.assert_current(f"{subject} 좋아해?", current, previous)

    def test_selection_limits_never_restore_old_descriptive_preference(self):
        previous = "나는 트로트 가수 김연자를 좋아해."
        current = "김연자 싫어해."
        self.publish("트로트 가수 김연자", previous, 100, importance=5)
        self.publish("김연자", current, 200, importance=1)
        self.publish("바둑", UNRELATED, 50)
        for index in range(16):
            subject = f"연주회{index}"
            self.publish(subject, (f"{subject}에서 " + "김연자와 바둑 이야기를 했어. " * 15).strip(),
                         300 + index, kind="profile", importance=5)
        self.warm()
        self.assertGreater(len(self.service._read_cache.records), corrections.MAX_MEMORIES)
        for budget, top_k in itertools.product((corrections.MAX_CONTEXT_CHARS, 220, 80), (1, 8)):
            with self.subTest(budget=budget, top_k=top_k), \
                    patch("persistent_memory.MAX_CONTEXT_CHARS", budget), \
                    patch("persistent_memory.MAX_MEMORIES", top_k):
                messages, rows = self.dispatch(QUERIES[0])
                self.assertNotIn(previous, {row["content"] for row in rows})
                if budget >= 220:
                    self.assertIn(current, {row["content"] for row in rows}, messages)
                self.assertLessEqual(len(rows), top_k)
                for message in messages:
                    payload = re.search(r"\n(\[.*\])\n</user_memories>", message["content"]).group(1)
                    self.assertLessEqual(len(payload), budget)

    def test_warm_cache_refresh_exposes_latest_descriptive_correction(self):
        previous = "나는 트로트 가수 김연자를 좋아해."
        current = "김연자 싫어해."
        self.publish("트로트 가수 김연자", previous, 100)
        self.publish("바둑", UNRELATED, 50)
        self.warm()
        _messages, rows = self.dispatch(QUERIES[0])
        self.assertEqual({row["content"] for row in rows}, {previous, UNRELATED})
        self.publish("김연자", current, 200)
        _messages, rows = self.dispatch(QUERIES[0])
        self.assertEqual({row["content"] for row in rows}, {previous, UNRELATED},
                         "direct storage save keeps the existing bounded cache delay")
        self.warm()
        _messages, rows = self.assert_current(QUERIES[0], current, previous)
        self.assertIn(UNRELATED, {row["content"] for row in rows})

    def test_uncontested_normal_memory_stays_available_with_current_correction(self):
        previous = "나는 트로트 가수 김연자를 좋아해."
        current = "김연자 싫어해."
        self.publish("가수 김연자", previous, 100)
        self.publish("김연자", current, 200)
        self.publish("바둑", UNRELATED, 50)
        self.publish("민지", "딸 민지는 부산에 살아.", 60, kind="profile")
        self.publish("민지", "민지와 공원에서 산책했어.", 70, kind="episodic")
        self.warm()
        _messages, rows = self.assert_current("민지와 바둑, 김연자 취향 기억나?", current, previous)
        self.assertEqual({row["content"] for row in rows},
                         {current, UNRELATED, "딸 민지는 부산에 살아.", "민지와 공원에서 산책했어."})
        self.assertEqual({row["type"] for row in rows}, {"preference", "profile", "episodic"})


if __name__ == "__main__":
    unittest.main()
