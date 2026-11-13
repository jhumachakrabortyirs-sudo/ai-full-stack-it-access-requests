"""T43: independent scalar counts and separate schema/evidence/routing checks.

Fault injections are authored comparison inputs, not accepted intake results.
The oracle predates those response variants and is bound to original sources.
"""
from copy import deepcopy
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from intake.mock_catalog import CatalogProvider
from intake.state import ProcessingConfig, StateStore
from operation_support import ROOT, MOCKS, POLICY, offline


ORACLE = json.loads((ROOT / "tests/accuracy_expected.json").read_text(encoding="utf-8"))


class AccuracyContract(unittest.TestCase):
    def setUp(self):
        guard = offline()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def api(self):
        try:
            return importlib.import_module("intake.accuracy")
        except ModuleNotFoundError as exc:
            self.fail(f"Step 8 accuracy comparison is absent: {exc}")

    def measured(self):
        provider = CatalogProvider(MOCKS)
        config = ProcessingConfig.capture(provider=provider, policy_path=POLICY)
        paths = [ROOT / row["source_file"] for row in ORACLE["documents"].values()]
        with StateStore(self.directory / "state.sqlite3") as store:
            return store.intake(paths, provider=provider, config=config, operator="accuracy-mock")

    def compare(self, records, oracle=None):
        return self.api().compare(records, deepcopy(ORACLE) if oracle is None else oracle, root=ROOT)

    def assert_counts(self, report, correct):
        self.assertEqual(report["scalar"], {"correct": correct, "denominator": 252})
        self.assertEqual(set(report["by_field"]), set(ORACLE["scalar_fields"]))
        self.assertEqual(set(report["by_document"]), set(ORACLE["documents"]))
        self.assertTrue(all(row["denominator"] == 21 for row in report["by_field"].values()))
        self.assertTrue(all(row["denominator"] == 12 for row in report["by_document"].values()))
        self.assertEqual(sum(row["correct"] for row in report["by_field"].values()), correct)
        self.assertEqual(sum(row["correct"] for row in report["by_document"].values()), correct)

    def test_T43_all_20_messages_and_fresh_paraphrase_match_252_independent_facts(self):
        self.api()
        records = self.measured()
        report = self.compare(records)
        self.assert_counts(report, 252)
        self.assertEqual(report["observation_kind"], "mock-fixture-agreement")
        self.assertRegex(report["interpretation"].casefold(), r"not.*live|does not.*live")
        self.assertEqual(report["factual_errors"], [])
        for check in ("schema", "quotes", "issues"):
            self.assertEqual(report[check]["passed"], 21)
            self.assertEqual(report[check]["denominator"], 21)
            self.assertEqual(report[check]["failures"], [])
        self.assertEqual(report["routing"], {"correct": 21, "denominator": 21, "failures": []})
        self.assertEqual(report["by_document"]["SYN-001.json"]["correct"], 12)
        self.assertEqual(len(report["provenance"]), 21)
        actual = {row["document_id"]: row for row in records}
        for row in report["provenance"]:
            name = row["document_id"]
            self.assertEqual(row["source_sha256"], ORACLE["documents"][name]["source_sha256"])
            for field in ("response_source", "model_configuration", "processing_fingerprint", "policy_sha256", "prompt_sha256", "schema_sha256"):
                self.assertEqual(row[field], actual[name][field])

    def test_T43_known_wrong_values_missing_field_and_lost_conflict_evidence_are_reported(self):
        self.api()
        records = self.measured()
        faults = json.loads((MOCKS / "accuracy-wrong.json").read_text())["responses"]
        for row in records:
            if row["document_id"] in faults:
                envelope = faults[row["document_id"]]
                row["extraction"] = json.loads(envelope["choices"][0]["message"]["content"])
            if row["document_id"] == "IT-001.json":
                row["owner"] = "Security Review"
                row["next_action"] = "review-risk-then-owner"
        report = self.compare(records)
        self.assert_counts(report, 249)
        self.assertEqual(report["schema"]["passed"], 20)
        self.assertEqual(report["quotes"]["passed"], 20)
        self.assertEqual(report["issues"]["passed"], 20)
        self.assertEqual(report["routing"]["correct"], 20)
        errors = report["factual_errors"]
        for name, field in [("IT-001.json", "permission"), ("IT-001.json", "request_date"), ("IT-020.json", "manager")]:
            self.assertTrue(any(row["document_id"] == name and row["field"] == field for row in errors), (name, field))
        self.assertTrue(any(
            row["document_id"] == "IT-020.json" and row["field"] == "supporting_quotes"
            and "Elena Park" in json.dumps(row["expected"])
            for row in errors
        ), "loss of conflicting source evidence needs an explicit factual-error entry")

    def test_T43_absent_failed_and_nonobject_results_keep_the_fixed_denominator(self):
        self.api()
        records = self.measured()
        nonobject = json.loads((MOCKS / "invalid/nonobject.json").read_text())
        malformed = json.loads((MOCKS / "malformed.json").read_text())
        variants = [
            ("absent", [row for row in records if row["document_id"] != "IT-003.json"], "IT-003.json"),
        ]
        for label, payload in [
            ("failed", None),
            ("nonobject", json.loads(nonobject["choices"][0]["message"]["content"])),
            ("malformed", malformed["choices"][0]["message"]["content"]),
        ]:
            changed = deepcopy(records)
            changed[0]["extraction"] = payload
            if payload is None:
                changed[0].update(status="error", owner="IT Service Desk", next_action="retry-extraction", error="Scripted comparison failure")
            variants.append((label, changed, changed[0]["document_id"]))
        for label, changed, affected in variants:
            with self.subTest(kind=label):
                report = self.compare(changed)
                self.assert_counts(report, 240)
                self.assertEqual(report["by_document"][affected]["correct"], 0)
                self.assertEqual(report["schema"]["passed"], 20)

    def test_T43_missing_key_is_not_a_correct_null_and_quotes_issues_have_separate_checks(self):
        self.api()
        records = self.measured()
        missing = deepcopy(records)
        target = next(row for row in missing if row["document_id"] == "IT-020.json")
        del target["extraction"]["manager"]
        report = self.compare(missing)
        self.assert_counts(report, 251)
        error = next(row for row in report["factual_errors"] if row["document_id"] == "IT-020.json" and row["field"] == "manager")
        self.assertIsNone(error["expected"])
        self.assertFalse(error["actual_present"])

        evidence = deepcopy(records)
        fabricated = json.loads((MOCKS / "invalid/fabricated-quote.json").read_text())
        quotes = json.loads(fabricated["choices"][0]["message"]["content"])["supporting_quotes"]
        next(row for row in evidence if row["document_id"] == "IT-001.json")["extraction"]["supporting_quotes"] = quotes
        next(row for row in evidence if row["document_id"] == "IT-020.json")["extraction"]["issues"] = []
        report = self.compare(evidence)
        self.assert_counts(report, 252)
        self.assertEqual(report["schema"]["passed"], 21)
        self.assertEqual(report["quotes"]["passed"], 20)
        self.assertEqual(report["issues"]["passed"], 20)
        self.assertTrue(any(row["reason"] == "not-substring" for row in report["quotes"]["failures"]))

    def test_T43_source_binding_duplicates_and_mixed_provenance_are_explicit(self):
        self.api()
        records = self.measured()
        with self.assertRaisesRegex(ValueError, "(?i)duplicate"):
            self.compare([*records, deepcopy(records[0])])
        wrong_source = deepcopy(ORACLE)
        wrong_source["documents"]["IT-001.json"]["source_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "(?i)source|hash"):
            self.compare(records, wrong_source)
        mixed = deepcopy(records)
        mixed[0]["response_source"] = "fixture-provider/fixture-model-v1"
        mixed[0]["model_configuration"].update(provider="fixture-provider", model="fixture-model-v1")
        report = self.compare(mixed)
        self.assertEqual(report["observation_kind"], "mixed-observation")
        self.assertEqual({row["response_source"] for row in report["provenance"]}, {"mock", "fixture-provider/fixture-model-v1"})

    def test_T43_cli_reads_the_oracle_linked_from_the_expected_document(self):
        api = self.api()
        oracle_path = self.directory / "independent-oracle.json"
        oracle_path.write_text(json.dumps(ORACLE, indent=2), encoding="utf-8")
        document = self.directory / "accuracy.md"
        document.write_text(
            "# Authored comparison input\n\n<!-- accuracy-oracle: independent-oracle.json -->\n"
            "[Source-derived oracle](independent-oracle.json)\n", encoding="utf-8",
        )
        loaded = api.load_oracle(document)
        self.assertEqual(loaded["documents"], ORACLE["documents"])
        environment = dict(os.environ)
        environment.pop("API_KEY", None)
        environment["PYTHONPATH"] = os.pathsep.join([str(ROOT / "tests/offline_site"), str(ROOT)])
        output = self.directory / "comparison.json"
        result = subprocess.run([
            sys.executable, "-m", "intake", "--db", str(self.directory / "benchmark.sqlite3"),
            "accuracy", "--expected", str(document), "--provider", "mock", "--output", str(output),
        ], cwd=ROOT, env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assert_counts(report, 252)
        self.assertEqual(report["observation_kind"], "mock-fixture-agreement")
        self.assertEqual(json.loads(output.read_text()), report)


if __name__ == "__main__":
    unittest.main()
