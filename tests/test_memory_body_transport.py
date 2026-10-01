"""Malformed 2 MiB metadata cannot be buffered on the voice retrieval path."""
import json
import unittest
from unittest.mock import Mock

from test_memory_protocol_v3 import row
from persistent_memory import MemoryStore
from memory_storage import MAX_RECORD_BYTES


class BodyTransportTests(unittest.TestCase):
    def response(self, body):
        payload = json.dumps(body, ensure_ascii=False).encode('utf-8')
        response = Mock(status_code=200, headers={})
        response.json.return_value = body
        self.transferred = 0

        def chunks(chunk_size):
            for offset in range(0, len(payload), chunk_size):
                chunk = payload[offset:offset + chunk_size]
                self.transferred += len(chunk)
                yield chunk

        response.iter_content.side_effect = chunks
        return response

    def test_unknown_metadata_is_cut_off_before_json_parse(self):
        response = self.response({**row(), 'unknownMetadata': 'x' * (2 * 1024 * 1024)})
        http = Mock()
        http.get.return_value = response
        store = MemoryStore('https://example.invalid', 'elder_1', http)
        self.assertEqual(store.active_read({'activeIds': {'old': True}}), {})
        self.assertGreater(self.transferred, 0)
        self.assertLessEqual(self.transferred, MAX_RECORD_BYTES + 1024)
        response.json.assert_not_called()
        response.close.assert_called_once()
        self.assertTrue(http.get.call_args.kwargs['stream'])

    def test_valid_bounded_body_remains_retrievable(self):
        body = row()
        response = self.response(body)
        http = Mock()
        http.get.return_value = response
        store = MemoryStore('https://example.invalid', 'elder_1', http)
        self.assertEqual(store.active_read({'activeIds': {'old': True}}), {'old': body})
        response.close.assert_called_once()
