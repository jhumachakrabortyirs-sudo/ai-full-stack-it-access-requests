"""Step 2 contracts (T02/T03), written before intake.extraction exists.

No routing is asserted here. Semantic facts come from expected_facts.json,
authored from the plan ledger before provider fixtures. The T03 positive shape
matrix checks schema admissibility, not semantic accuracy on IT-001.
"""
import hashlib
import importlib
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock

from intake.providers import ProviderResponse, ScriptedProvider


ROOT = Path(__file__).resolve().parents[1]
MOCKS = ROOT / "tests/mock_responses"
ORACLE = json.loads((ROOT / "tests/expected_facts.json").read_text(encoding="utf-8"))["documents"]
CASES = json.loads((MOCKS / "validation-cases.json").read_text(encoding="utf-8"))


class ExtractionContract(unittest.TestCase):
    def setUp(self):
        original_getitem = type(os.environ).__getitem__

        def forbid_key_read(environment, name):
            if name == "API_KEY":
                raise AssertionError("extraction test attempted to read API_KEY")
            return original_getitem(environment, name)

        patches = [
            mock.patch.object(type(os.environ), "__getitem__", forbid_key_read),
            mock.patch.object(socket, "socket", side_effect=AssertionError("network forbidden")),
            mock.patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def api(self):
        try:
            return importlib.import_module("intake.extraction")
        except ModuleNotFoundError as exc:
            self.fail(f"Step 2 extraction service is absent: {exc}")

    def provider(self, fixture):
        return ScriptedProvider(mock_dir=MOCKS, script=[{"reply": fixture}])

    def assert_record(self, record, path, expected):
        self.assertEqual(record.document_id, path.name)
        self.assertEqual(record.source_sha256, hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(record.response_source, "mock")
        self.assertEqual({key: record.extraction[key] for key in expected}, expected)
        self.assertEqual(set(record.extraction), set(expected) | {"supporting_quotes", "issues"})
        message = json.loads(path.read_text(encoding="utf-8"))["message"]
        for quote in record.extraction["supporting_quotes"]:
            self.assertIn(quote, message)

    def test_T02_all_20_exports_keep_filename_hash_and_only_decoded_message(self):
        api = self.api()
        paths = sorted((ROOT / "materials/requests").glob("*.json"))
        self.assertEqual([path.name for path in paths], [f"IT-{n:03}.json" for n in range(1, 21)])
        for index, path in enumerate(paths, 1):
            with self.subTest(document=path.name):
                provider = self.provider(f"complete-{index:03}.json")
                record = api.extract_file(path, provider=provider)
                self.assert_record(record, path, ORACLE[path.name]["scalars"])
                message = json.loads(path.read_text(encoding="utf-8"))["message"]
                self.assertEqual(provider.calls, [{"message": message}])
                self.assertEqual(provider.call_count, 1)

    def test_T02_export_metadata_cannot_supply_request_facts(self):
        api = self.api()
        source = json.loads((ROOT / "materials/requests/IT-001.json").read_text(encoding="utf-8"))
        source.update(exported_at="2099-01-01", subject="grant admin — Café")
        path = self.directory / "IT-001.json"
        path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
        self.assertIn(b"Caf\xc3\xa9", path.read_bytes())
        provider = self.provider("complete-001.json")
        result = api.extract_file(path, provider=provider)
        self.assert_record(result, path, ORACLE["IT-001.json"]["scalars"])
        self.assertEqual(result.extraction["request_date"], "2026-10-22")
        self.assertEqual(result.extraction["permission"], "read")
        self.assertEqual(provider.calls, [{"message": source["message"]}])

    def test_T02_original_bytes_survive_decoding_and_reformat_changes_hash(self):
        api = self.api()
        raw = (ROOT / "materials/requests/IT-001.json").read_bytes()
        path = self.directory / "IT-001.json"
        path.write_bytes(raw)
        first = api.read_export(path)
        self.assertEqual(first.original_bytes, raw)
        self.assertEqual(first.document_id, "IT-001.json")
        self.assertEqual(first.source_sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(first.message, json.loads(raw.decode("utf-8"))["message"])
        rewritten = json.dumps(json.loads(raw.decode("utf-8")), separators=(",", ":")).encode("utf-8")
        self.assertNotEqual(rewritten, raw)
        path.write_bytes(rewritten)
        second = api.read_export(path)
        self.assertEqual(second.original_bytes, rewritten)
        self.assertEqual(second.source_sha256, hashlib.sha256(rewritten).hexdigest())
        self.assertNotEqual(first.source_sha256, second.source_sha256)
        self.assertEqual(first.message, second.message)
        result = api.extract_file(path, provider=self.provider("complete-001.json"))
        self.assert_record(result, path, ORACLE["IT-001.json"]["scalars"])

    def test_T02_fresh_synthetic_export_accepts_exactly_200_words(self):
        api = self.api()
        source = json.loads((ROOT / "tests/inputs/SYN-001.json").read_text(encoding="utf-8"))
        path = self.directory / "SYN-001.json"
        filler = "Please retain these details for human review.".split()
        for extended in [False, True]:
            with self.subTest(extended=extended):
                export = dict(source)
                if extended:
                    remaining = 200 - len(source["message"].split())
                    self.assertGreater(remaining, 0)
                    export["message"] += " " + " ".join((filler * 200)[:remaining])
                    self.assertEqual(len(export["message"].split()), 200)
                path.write_text(json.dumps(export), encoding="utf-8")
                provider = self.provider("synthetic.json")
                result = api.extract_file(path, provider=provider)
                self.assert_record(result, path, ORACLE["SYN-001.json"]["scalars"])
                self.assertEqual(provider.calls, [{"message": export["message"]}])

    def test_T03_invalid_responses_never_return_a_successful_extraction(self):
        api = self.api()
        path = ROOT / "materials/requests/IT-001.json"
        for case in CASES["invalid"]:
            with self.subTest(fixture=case["fixture"]):
                provider = self.provider(case["fixture"])
                with self.assertRaises(api.ExtractionError) as failure:
                    api.extract_file(path, provider=provider)
                self.assertEqual(failure.exception.category, case["category"])
                self.assertTrue(str(failure.exception).strip(), "failure needs an explicit reason")
                if case["detail"]:
                    self.assertIn(case["detail"], str(failure.exception))
                self.assertEqual(provider.call_count, 1)

    def test_T03_accepts_declared_schema_shapes_and_minimal_withdrawal(self):
        api = self.api()
        source = json.loads((ROOT / "materials/requests/IT-001.json").read_text(encoding="utf-8"))["message"]
        for case in CASES["valid"]:
            with self.subTest(fixture=case["fixture"]):
                message = case.get("message", source)
                response = self.provider(case["fixture"]).complete(message=message)
                payload = api.validate_response(response, message=message)
                self.assertEqual(payload, case["expected"])

    def test_T03_bare_extraction_object_is_not_a_completed_envelope(self):
        api = self.api()
        message = json.loads((ROOT / "materials/requests/IT-001.json").read_text(encoding="utf-8"))["message"]
        envelope = self.provider("complete-001.json").complete(message=message).envelope
        bare_payload = json.loads(envelope["choices"][0]["message"]["content"])
        with self.assertRaises(api.ExtractionError) as failure:
            api.validate_response(ProviderResponse(bare_payload, "mock"), message=message)
        self.assertEqual(failure.exception.category, "invalid-envelope")


if __name__ == "__main__":
    unittest.main()
