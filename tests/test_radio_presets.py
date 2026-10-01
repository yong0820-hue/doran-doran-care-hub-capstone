import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HUB_DIR = Path(__file__).resolve().parents[1] / "raspberry-pi-voice-hub"
sys.path.insert(0, str(HUB_DIR))

from radio_presets import (DEFAULT_CONFIG_PATH, MAX_CONFIG_BYTES, MAX_NAME_CHARS,
                           MAX_PRESETS, MAX_URL_CHARS, load_radio_presets)


class RadioPresetsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "radio_presets.json"

    def write(self, value, encoding="utf-8"):
        self.path.write_text(json.dumps(value, ensure_ascii=False), encoding=encoding)

    def test_shipped_configuration_is_an_explicit_empty_state(self):
        self.assertEqual(load_radio_presets(DEFAULT_CONFIG_PATH),
                         {"presets": [], "status": "empty", "rejected_count": 0})

    def test_missing_file_is_a_distinct_empty_response(self):
        self.assertEqual(load_radio_presets(self.path),
                         {"presets": [], "status": "missing", "rejected_count": 0})

    def test_valid_presets_preserve_order_and_only_expose_public_fields(self):
        presets = [
            {"name": "  우리 라디오  ", "url": "https://radio.example/live", "extra": "hidden"},
            {"name": "Local test", "url": "http://127.0.0.1:9000/test.wav"},
        ]
        self.write({"presets": presets, "extra": "hidden"})
        result = load_radio_presets(self.path)
        self.assertEqual(result, {"status": "ready", "rejected_count": 0, "presets": [
            {"name": "우리 라디오", "url": "https://radio.example/live"}, presets[1]]})

    def test_utf8_bom_from_powershell_is_supported(self):
        self.write({"presets": [{"name": "테스트", "url": "http://localhost/test.wav"}]},
                   encoding="utf-8-sig")
        self.assertEqual(load_radio_presets(self.path)["status"], "ready")

    def test_empty_array_is_empty_but_bad_schema_is_invalid(self):
        self.write({"presets": []})
        self.assertEqual(load_radio_presets(self.path)["status"], "empty")
        for config in ({}, [], None, {"presets": None}, {"presets": {}}, {"presets": "url"}):
            with self.subTest(config=config):
                self.write(config)
                self.assertEqual(load_radio_presets(self.path)["status"], "invalid")

    def test_malformed_json_and_encoding_are_contained(self):
        for raw in (b"{", b"", b"\xff\xfe", b"[" * 1500):
            with self.subTest(raw=raw[:10]):
                self.path.write_bytes(raw)
                self.assertEqual(load_radio_presets(self.path),
                                 {"presets": [], "status": "invalid", "rejected_count": 0})

    def test_read_failure_does_not_expose_file_paths_or_error_details(self):
        with patch.object(Path, "open", side_effect=PermissionError("PRIVATE_DATA")):
            result = load_radio_presets(self.path)
        self.assertEqual(result, {"presets": [], "status": "invalid", "rejected_count": 0})
        self.assertNotIn("PRIVATE_DATA", json.dumps(result))

    def test_invalid_entries_are_skipped_without_removing_valid_station(self):
        valid = {"name": "Demo", "url": "https://radio.example/live"}
        self.write({"presets": [None, "bad", {}, {"name": "Missing URL"}, valid]})
        self.assertEqual(load_radio_presets(self.path),
                         {"status": "ready", "presets": [valid], "rejected_count": 4})

    def test_all_invalid_entries_produce_configuration_error(self):
        self.write({"presets": [{"name": "Bad", "url": "javascript:alert(1)"}]})
        self.assertEqual(load_radio_presets(self.path),
                         {"status": "invalid", "presets": [], "rejected_count": 1})

    def test_name_must_be_nonempty_bounded_and_have_no_control_characters(self):
        for name in (None, 42, "", "   ", "a" * (MAX_NAME_CHARS + 1), "a\nb", "a\x7fb"):
            with self.subTest(name=name):
                self.write({"presets": [{"name": name, "url": "https://radio.example/live"}]})
                self.assertEqual(load_radio_presets(self.path)["status"], "invalid")

    def test_unsupported_credentialed_or_malformed_urls_are_rejected(self):
        urls = [None, 12, "", "/relative.mp3", "//radio.example/live", "file:///radio.mp3",
                "data:audio/wav;base64,AA", "ftp://radio.example/live",
                "https://user:password@radio.example/live", "https://user@radio.example/live",
                "https:///live", "http://", "https://radio.example:bad/live",
                "https://radio.example:65536/live", "https://radio.example:0/live",
                "https://radio.example:/live", "https://radio.example/live#player",
                "https://radio.example/live#", "https://radio.example/live music",
                "https://radio.example/\nlive", "https://radio.example/\x00live",
                "https://radio.example\\evil/live", "https://bad_host/live",
                "https://-invalid.example/live", "https://a..example/live",
                "https://[invalid]/live", "https://radio.example/" + "x" * MAX_URL_CHARS]
        for url in urls:
            with self.subTest(url=url):
                self.write({"presets": [{"name": "Test", "url": url}]})
                self.assertEqual(load_radio_presets(self.path)["status"], "invalid")

    def test_http_https_ipv6_and_query_stream_urls_are_supported(self):
        urls = ["http://radio.example/stream", "https://radio.example/stream?format=mp3",
                "http://[::1]:9000/test.wav", "https://라디오.example/live"]
        for url in urls:
            with self.subTest(url=url):
                self.write({"presets": [{"name": "Test", "url": url}]})
                self.assertEqual(load_radio_presets(self.path)["presets"][0]["url"], url)

    def test_large_file_is_rejected_before_json_parsing(self):
        self.path.write_bytes(b" " * (MAX_CONFIG_BYTES + 1))
        with patch("radio_presets.json.loads") as parser:
            self.assertEqual(load_radio_presets(self.path)["status"], "invalid")
            parser.assert_not_called()

    def test_only_first_bounded_entries_are_loaded_in_order(self):
        entries = [{"name": str(i), "url": f"https://radio.example/{i}"}
                   for i in range(MAX_PRESETS + 3)]
        self.write({"presets": entries})
        result = load_radio_presets(self.path)
        self.assertEqual(result["presets"], entries[:MAX_PRESETS])
        self.assertEqual(result["rejected_count"], 3)

    def test_loader_reads_changes_without_server_restart(self):
        self.write({"presets": []})
        self.assertEqual(load_radio_presets(self.path)["status"], "empty")
        self.write({"presets": [{"name": "New", "url": "http://localhost/test.wav"}]})
        self.assertEqual(load_radio_presets(self.path)["status"], "ready")


if __name__ == "__main__":
    unittest.main()
