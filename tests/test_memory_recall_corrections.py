"""Preference corrections through extraction, RTDB, cache and LLM dispatch.

Only the extraction model and HTTP transport are doubles. Assertions inspect
the messages received by the synchronous LLM callback, never select() alone.
"""

from copy import deepcopy
import hashlib
import itertools
import json
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "raspberry-pi-voice-hub"))
from memory_protocol_fake import ProtocolFirebase
from persistent_memory import (INITIAL_GENERATION, MAX_CONTEXT_CHARS, MAX_MEMORIES,
                               MemoryExtractor, MemoryStore, PersistentMemoryService)


BASE = 1_800_000_000_000
OLD = "나는 트로트 가수 김연자를 좋아해."
CURRENT = "김연자 싫어해."
QUERIES = ("김연자 좋아해?", "김연자 싫어해?", "김연자에 대한 내 취향 기억나?")


class MemoryRecallCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.new_scenario()

    def new_scenario(self):
        if hasattr(self, "service"):
            self.service.stop()
        self.client = Mock()
        self.extractor = MemoryExtractor(self.client, "fake-model")
        self.http = ProtocolFirebase({"_control": {"generation": INITIAL_GENERATION, "activeIds": {}}})
        self.store = MemoryStore("https://example.invalid", "elder_1", self.http)
        self.service = PersistentMemoryService(self.store, self.extractor)
        self.addCleanup(self.service.stop)
        self.serial = 0

    def publish(self, subject, content, timestamp, kind="preference", importance=3):
        self.serial += 1
        self.client.responses.create.return_value = Mock(status="completed", output_text=json.dumps({
            "memories": [{"type": kind, "subject": subject, "evidence": content,
                          "messageIndex": 0, "importance": importance}],
        }, ensure_ascii=False))
        with patch("persistent_memory.time.time", return_value=(BASE + 10000 + self.serial) / 1000):
            candidates = self.extractor.extract([{"text": content, "createdAt": BASE + timestamp}])
            self.assertEqual(len(candidates), 1, "fixture must pass real extraction validation")
            self.assertEqual(candidates[0]["content"], content)
            self.assertEqual(candidates[0]["lastMentionedAt"], BASE + timestamp)
            self.assertEqual(candidates[0]["evidenceKey"], hashlib.sha256(content.encode()).hexdigest())
            self.assertTrue(self.store.save(candidates, "session_" + str(self.serial), INITIAL_GENERATION))
        self.assert_protocol()

    def records(self):
        return self.store.active_read(self.http.root["_control"])

    def assert_protocol(self):
        self.assertFalse(any(url == self.store.url for url, _ in self.http.get_calls))
        self.assertFalse(any(url == self.store.url for url, *_ in self.http.put_calls))
        self.assertTrue(any("/active/" in url for url, *_ in self.http.put_calls))
        self.assertTrue(any("/_control.json" in url and "if-match" in headers
                            for url, _body, headers, _kwargs in self.http.put_calls))
        self.assertEqual(self.http.root["_control"]["generation"], INITIAL_GENERATION)

    def warm(self):
        before = deepcopy(self.http.root)
        self.service.prefetch(force=True)
        self.assertTrue(self.service.wait_prefetch(2))
        self.assertEqual(self.service._read_cache.session_generation(), INITIAL_GENERATION)
        self.assertTrue(self.service._read_cache.records)
        self.assertEqual(self.http.root, before, "cache loading must not mutate saved history")
        self.assert_protocol()

    def dispatch(self, query):
        before = deepcopy(self.http.root)
        calls_before = (len(self.http.get_calls), len(self.http.put_calls), len(self.http.patch_calls))
        callback = Mock(side_effect=deepcopy)
        with self.service.request_context(query, INITIAL_GENERATION) as request:
            messages = request.dispatch(callback)
        callback.assert_called_once()
        self.assertEqual(callback.call_args.args[0], messages)
        self.assertEqual(self.http.root, before, "recall must preserve IDs, bodies, evidence and timestamps")
        self.assertEqual((len(self.http.get_calls), len(self.http.put_calls), len(self.http.patch_calls)),
                         calls_before, "warm request dispatch must remain local")
        rows = []
        if messages:
            self.assertEqual(len(messages), 1)
            self.assertEqual(messages[0]["role"], "user")
            match = re.search(r"\n(\[.*\])\n</user_memories>", messages[0]["content"])
            self.assertIsNotNone(match, messages)
            rows = json.loads(match.group(1))
            self.assertLessEqual(len(rows), MAX_MEMORIES)
            self.assertLessEqual(len(match.group(1)), MAX_CONTEXT_CHARS)
        return messages, rows

    def assert_current(self, query, current, previous, timestamp=200):
        messages, rows = self.dispatch(query)
        contents = {row["content"] for row in rows}
        self.assertIn(current, contents, json.dumps(messages, ensure_ascii=False))
        self.assertNotIn(previous, contents, json.dumps(messages, ensure_ascii=False))
        selected = next(row for row in rows if row["content"] == current)
        self.assertEqual(selected["lastMentionedAt"], BASE + timestamp)
        return messages, rows

    def test_original_correction_reaches_actual_llm_callback_for_all_queries(self):
        self.publish("김연자", OLD, 100)
        self.publish("김연자", CURRENT, 200)
        self.assertEqual({row["content"] for row in self.records().values()}, {OLD, CURRENT})
        self.warm()
        for query in QUERIES:
            with self.subTest(query=query):
                self.assert_current(query, CURRENT, OLD)

    def test_opposite_polarity_correction_is_not_a_negative_preference_bias(self):
        previous, current = "나는 트로트 가수 김연자를 싫어해.", "김연자 좋아해."
        self.publish("김연자", previous, 100)
        self.publish("김연자", current, 200)
        self.warm()
        for query in QUERIES:
            with self.subTest(query=query):
                self.assert_current(query, current, previous)

    def test_other_named_subjects_follow_the_same_correction_rule(self):
        cases = (("박완서", "나는 소설가 박완서를 좋아해.", "박완서 싫어해."),
                 ("박완서", "나는 소설 작가 박완서를 좋아해.", "박완서 싫어해."),
                 ("안성기", "나는 영화 배우 안성기를 좋아해.", "안성기 싫어해."),
                 ("아이유", "나는 가수 아이유를 좋아해.", "아이유 싫어해."))
        for subject, previous, current in cases:
            with self.subTest(previous=previous):
                self.new_scenario()
                self.publish(subject, previous, 100)
                self.publish(subject, current, 200)
                self.warm()
                self.assert_current(f"{subject} 좋아해?", current, previous)

    def test_length_and_arrival_order_cannot_override_utterance_time(self):
        cases = ((OLD, CURRENT),
                 ("김연자 좋아해.", "나는 트로트 가수 김연자를 싫어해."),
                 ("나는 트로트 가수 김연자를 싫어해.", "김연자 좋아해."),
                 ("김연자 싫어해.", OLD),
                 (OLD, "가수 김연자를 싫어해."),
                 ("가수 김연자를 좋아해.", "트로트 가수 김연자를 싫어해."))
        for previous, current in cases:
            for order in itertools.permutations(((previous, 100), (current, 200))):
                with self.subTest(previous=previous, current=current, arrival=[row[1] for row in order]):
                    self.new_scenario()
                    for content, timestamp in order:
                        self.publish("김연자", content, timestamp)
                    self.warm()
                    self.assert_current("김연자 좋아해?", current, previous)

    def test_competing_roles_do_not_assume_that_shared_names_are_one_person(self):
        described = ("배우 김민수를 좋아해.", "축구 선수 김민수를 싫어해.")
        for include_bare in (False, True):
            with self.subTest(include_bare=include_bare):
                self.new_scenario()
                for index, content in enumerate(described):
                    self.publish("김민수", content, 100 + index * 100)
                if include_bare:
                    self.publish("김민수", "김민수 좋아해.", 300)
                self.publish("바둑", "바둑을 좋아해.", 50)
                self.warm()
                _messages, rows = self.dispatch("김민수 취향")
                self.assertEqual({row["content"] for row in rows}, {"바둑을 좋아해."},
                                 "competing roles need disambiguation before a name-only correction")

    def test_independent_people_food_and_walk_conditions_all_reach_dispatch(self):
        cases = (
            (("김연자", "김연자를 좋아해."), ("나훈아", "나훈아를 싫어해."), "김연자 나훈아 취향"),
            (("음식", "매운 음식을 좋아해."), ("음식", "단 음식을 싫어해."), "음식 취향"),
            (("산책", "아침 산책 좋아해."), ("산책", "저녁 산책 싫어해."), "산책 취향"),
            (("그림", "수채화 그림을 좋아해."), ("그림", "유화 그림을 싫어해."), "그림 취향"),
            (("김연자", "김연자의 노래를 좋아해."), ("김연자", "김연자의 목소리를 싫어해."), "김연자 취향"),
        )
        for first, second, query in cases:
            with self.subTest(query=query):
                self.new_scenario()
                self.publish(*first, 100)
                self.publish(*second, 200)
                self.assertEqual(len(self.records()), 2)
                self.warm()
                _messages, rows = self.dispatch(query)
                self.assertEqual({row["content"] for row in rows}, {first[1], second[1]})

    def test_conflict_resolution_precedes_query_importance_top_k_and_character_budget(self):
        self.publish("김연자", OLD, 100, importance=5)
        self.publish("김연자", CURRENT, 200, importance=1)
        for index in range(16):
            subject = f"연주회{index}"
            content = (f"{subject}에서 " + ("김연자 이야기를 했어. " if index % 2 else "바둑 이야기를 했어. ") * 18).strip()
            self.publish(subject, content, 300 + index, kind="profile", importance=5)
        self.warm()
        self.assertGreater(len(self.service._read_cache.records), MAX_MEMORIES)
        self.assertGreater(sum(len(json.dumps(row, ensure_ascii=False))
                               for row in self.service._read_cache.records.values()), MAX_CONTEXT_CHARS)
        for budget in (MAX_CONTEXT_CHARS, 220, 80):
            with self.subTest(budget=budget), patch("persistent_memory.MAX_CONTEXT_CHARS", budget):
                messages, rows = self.dispatch("김연자 좋아해?")
                self.assertNotIn(OLD, {row["content"] for row in rows})
                if budget >= 220:
                    self.assertIn(CURRENT, {row["content"] for row in rows}, messages)
                for message in messages:
                    payload = re.search(r"\n(\[.*\])\n</user_memories>", message["content"]).group(1)
                    self.assertLessEqual(len(payload), budget)

    def test_equal_timestamp_descriptive_conflict_is_withheld_for_both_arrival_orders(self):
        for order in itertools.permutations((OLD, CURRENT)):
            with self.subTest(order=order):
                self.new_scenario()
                for content in order:
                    self.publish("김연자", content, 100)
                self.publish("바둑", "바둑을 좋아해.", 50)
                self.warm()
                _messages, rows = self.dispatch("김연자 취향")
                contents = {row["content"] for row in rows}
                self.assertFalse(contents & {OLD, CURRENT})
                self.assertIn("바둑을 좋아해.", contents)

    def test_unresolved_compound_conflict_cannot_send_only_the_old_preference(self):
        cases = (("음식", "매운 음식과 단 음식을 좋아해.", "단 음식", "단 음식을 싫어해."),
                 ("장미", "붉은 장미 대신 하얀 장미를 좋아해.", "하얀 장미", "하얀 장미를 싫어해."),
                 ("단 음식", "단 음식을 좋아해.", "음식", "매운 음식과 단 음식을 싫어해."))
        for broad, previous, specific, current in cases:
            for order in itertools.permutations(((broad, previous, 100), (specific, current, 200))):
                with self.subTest(previous=previous, arrival=[row[2] for row in order]):
                    self.new_scenario()
                    for event in order:
                        self.publish(*event)
                    self.publish("바둑", "바둑을 좋아해.", 50)
                    self.warm()
                    _messages, rows = self.dispatch(f"{specific} 취향")
                    contents = {row["content"] for row in rows}
                    self.assertFalse(contents & {previous, current}, "unresolved related evidence must be withheld")
                    self.assertIn("바둑을 좋아해.", contents)

    def test_uncertain_condition_is_not_erased_as_a_name_description(self):
        for previous in ("아침에 김연자를 좋아해.", "아침에 가수 김연자를 좋아해.",
                         "아침 가수 김연자를 좋아해.",
                         "신나는 노래의 김연자를 좋아해."):
            for order in itertools.permutations(((previous, 100), (CURRENT, 200))):
                with self.subTest(previous=previous, arrival=[row[1] for row in order]):
                    self.new_scenario()
                    for content, timestamp in order:
                        self.publish("김연자", content, timestamp)
                    self.publish("바둑", "바둑을 좋아해.", 50)
                    self.warm()
                    _messages, rows = self.dispatch(QUERIES[0])
                    contents = {row["content"] for row in rows}
                    self.assertFalse(contents & {previous, CURRENT})
                    self.assertIn("바둑을 좋아해.", contents)

    def test_commute_conditions_are_not_collapsed_as_role_descriptions(self):
        first, second = "출근길 가수 김연자를 좋아해.", "퇴근길 가수 김연자를 싫어해."
        for order in itertools.permutations(((first, 100), (second, 200))):
            with self.subTest(arrival=[row[1] for row in order]):
                self.new_scenario()
                for content, timestamp in order:
                    self.publish("김연자", content, timestamp)
                self.assertEqual(len(self.records()), 2)
                self.warm()
                _messages, rows = self.dispatch("김연자 취향")
                self.assertEqual({row["content"] for row in rows}, {first, second})

    def test_warm_cache_allowed_delay_ends_after_explicit_refresh(self):
        self.publish("김연자", OLD, 100)
        self.warm()
        _before_messages, rows = self.dispatch(QUERIES[0])
        self.assertIn(OLD, {row["content"] for row in rows})
        self.publish("김연자", CURRENT, 200)
        # Direct storage publication does not invalidate an already loaded
        # snapshot. The bounded refresh interval permits this stale dispatch.
        _stale_messages, rows = self.dispatch(QUERIES[0])
        self.assertIn(OLD, {row["content"] for row in rows})
        self.assertNotIn(CURRENT, {row["content"] for row in rows})
        self.warm()
        self.assert_current(QUERIES[0], CURRENT, OLD)

    def test_profile_preference_and_recent_event_remain_available(self):
        expected = {"딸 민지는 부산에 살아.", "바둑을 좋아해.", "민지와 공원에서 산책했어."}
        self.publish("민지", "딸 민지는 부산에 살아.", 100, kind="profile")
        self.publish("바둑", "바둑을 좋아해.", 200)
        self.publish("민지", "민지와 공원에서 산책했어.", 300, kind="episodic")
        self.warm()
        _messages, rows = self.dispatch("민지와 바둑 이야기 기억나?")
        self.assertEqual({row["content"] for row in rows}, expected)
        self.assertEqual({row["type"] for row in rows}, {"profile", "preference", "episodic"})

    def test_prior_broad_duplicate_and_independent_storage_case_reaches_payload(self):
        first = "매운 음식을 좋아해."
        self.publish("음식", first, 100)
        original_id = next(iter(self.records().values()))["id"]
        self.publish("매운 음식", first, 200)
        self.publish("단 음식", "단 음식을 싫어해.", 300)
        records = list(self.records().values())
        self.assertEqual(len(records), 2)
        first_row = next(row for row in records if row["content"] == first)
        self.assertEqual(first_row["id"], original_id)
        self.assertEqual(first_row["lastMentionedAt"], BASE + 200)
        self.warm()
        _messages, rows = self.dispatch("음식 취향")
        self.assertEqual({row["content"] for row in rows}, {first, "단 음식을 싫어해."})

    def test_prior_spacing_particle_and_lexical_names_keep_current_payload(self):
        cases = (("매운 음식", "매운 음식을 좋아해.", "매운음식", "매운음식은 싫어해."),
                 ("요가", "요가를 좋아해.", "요가", "요가는 싫어해."),
                 ("오이", "오이를 좋아해.", "오이", "오이는 싫어해."),
                 ("산책", "아침 산책을 좋아해.", "아침 산책", "아침 산책을 싫어해."))
        for first_subject, previous, current_subject, current in cases:
            with self.subTest(subject=first_subject):
                self.new_scenario()
                self.publish(first_subject, previous, 100)
                original_id = next(iter(self.records().values()))["id"]
                self.publish(current_subject, current, 200)
                self.publish(first_subject, previous, 100)
                records = list(self.records().values())
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["id"], original_id)
                self.assertEqual(records[0]["content"], current)
                self.warm()
                self.assert_current(f"{current_subject} 취향", current, previous)


if __name__ == "__main__":
    unittest.main()
