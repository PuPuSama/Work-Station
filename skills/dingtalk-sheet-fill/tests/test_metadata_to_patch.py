from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPT = SKILL_DIR / "scripts" / "metadata_to_patch.py"
SPEC = importlib.util.spec_from_file_location("dingtalk_metadata_to_patch", SCRIPT)
assert SPEC and SPEC.loader
PARSER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PARSER)


class MetadataPatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = json.loads(
            (SKILL_DIR / "references" / "metadata.example.json").read_text(encoding="utf-8")
        )

    def patch(self):
        return PARSER.build_patch(self.data, Path("metadata.json"))

    def test_seven_fields_use_counts_and_anchor_text_not_urls(self):
        patch = self.patch()
        self.assertEqual(patch["project_id"], "example.com")
        self.assertEqual(patch["match_title"], self.data["title"])
        self.assertEqual(set(patch["cells"]), {"A", "D", "E", "G", "H", "I", "K"})
        self.assertEqual(patch["cells"]["A"]["value"], "9.9")
        self.assertEqual(patch["cells"]["E"]["value"], 4)
        self.assertEqual(patch["cells"]["H"]["value"], 20)
        self.assertEqual(patch["cells"]["K"]["value"], 16)
        self.assertEqual(
            patch["cells"]["I"]["value"], "pump selection guide\nmaintenance checklist"
        )

    def test_primary_keyword_is_matched_by_value_not_entry_order(self):
        self.data["keyword_density"].insert(0, {"keyword": "other", "occurrences": 99})
        self.assertEqual(self.patch()["cells"]["E"]["value"], 4)

    def test_single_keyword_fallback(self):
        self.data.pop("primary_keyword")
        self.data["keywords"] = ["workshop pump"]
        self.assertEqual(self.patch()["cells"]["D"]["value"], "workshop pump")

    def test_ambiguous_or_empty_keyword_fallback_is_rejected(self):
        self.data.pop("primary_keyword")
        for keywords in (["a", "b"], [], [" "], [None]):
            with self.subTest(keywords=keywords):
                self.data["keywords"] = keywords
                with self.assertRaises(PARSER.MetadataError):
                    self.patch()

    def test_missing_or_duplicate_keyword_entry_is_rejected(self):
        entry = self.data["keyword_density"][0]
        for entries in ([], [entry, entry]):
            with self.subTest(entries=entries):
                self.data["keyword_density"] = entries
                with self.assertRaises(PARSER.MetadataError):
                    self.patch()

    def test_missing_text_fields_are_rejected(self):
        for key in ("project_id", "title", "completion_date"):
            with self.subTest(key=key):
                original = self.data.pop(key)
                with self.assertRaises(PARSER.MetadataError):
                    self.patch()
                self.data[key] = original

    def test_invalid_date_is_rejected(self):
        self.data["completion_date"] = "2026-02-30"
        with self.assertRaises(PARSER.MetadataError):
            self.patch()

    def test_unconfirmed_ai_is_rejected_even_with_valid_score(self):
        for confirmed in (False, None, "true", 1):
            with self.subTest(confirmed=confirmed):
                self.data["ai_rate_confirmed"] = confirmed
                with self.assertRaises(PARSER.MetadataError):
                    self.patch()

    def test_unavailable_or_stale_citation_is_rejected(self):
        for status in ("stale", "unavailable", "not_checked", None):
            with self.subTest(status=status):
                self.data["knowledge_base_citation_status"] = status
                with self.assertRaises(PARSER.MetadataError):
                    self.patch()

    def test_percentages_reject_invalid_values(self):
        for key in ("ai_rate_percent", "knowledge_base_citation_rate_percent"):
            original = self.data[key]
            for value in (-1, 101, True, None, "20%", float("nan"), float("inf")):
                with self.subTest(key=key, value=value):
                    self.data[key] = value
                    with self.assertRaises(PARSER.MetadataError):
                        self.patch()
            self.data[key] = original

    def test_zero_and_hundred_are_valid_not_missing(self):
        for value in (0, 100):
            self.data["ai_rate_percent"] = value
            self.data["knowledge_base_citation_rate_percent"] = value
            self.data["keyword_density"][0]["occurrences"] = 0
            self.assertEqual(self.patch()["cells"]["H"]["value"], value)
            self.assertEqual(self.patch()["cells"]["E"]["value"], 0)

    def test_invalid_occurrences_are_rejected(self):
        for value in (-1, True, 1.5, "4", None):
            with self.subTest(value=value):
                self.data["keyword_density"][0]["occurrences"] = value
                with self.assertRaises(PARSER.MetadataError):
                    self.patch()

    def test_empty_or_non_text_anchors_are_rejected(self):
        for values in ([], [" "], [None], ["valid", 42], None):
            with self.subTest(values=values):
                self.data["anchor_text"] = values
                with self.assertRaises(PARSER.MetadataError):
                    self.patch()

    def test_non_object_root_is_rejected(self):
        self.data = []
        with self.assertRaises(PARSER.MetadataError):
            self.patch()

    def run_cli(self, path):
        return subprocess.run(
            [sys.executable, "-X", "utf8", str(SCRIPT), str(path)],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )

    def test_cli_handles_utf8_bom_and_spaced_paths_without_editing_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "文章 metadata.json"
            path.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8-sig")
            original = path.read_bytes()
            result = self.run_cli(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["project_id"], "example.com")
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_cli_rejects_invalid_json_encoding_and_data_without_partial_patch(self):
        invalid_metadata = dict(self.data, ai_rate_confirmed=False)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metadata.json"
            for content in (b"{broken", b"\xff", json.dumps(invalid_metadata).encode()):
                with self.subTest(content=content):
                    path.write_bytes(content)
                    result = self.run_cli(path)
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")
                    self.assertNotIn("Traceback", result.stderr)

    def test_cli_missing_path_returns_validation_error(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_cli(Path(directory) / "missing.json")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
