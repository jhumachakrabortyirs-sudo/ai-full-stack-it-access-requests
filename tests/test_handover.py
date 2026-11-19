"""T44: auditable handover documents and exact runnable mock commands.

The full unittest command is checked as documentation and run by the outer
suite, avoiding recursive suite execution. Operator commands are executed
verbatim, without a shell, in a temporary checkout with network/key guards.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from intake.http_provider import HttpProvider, ProviderSettings
from operation_support import ROOT, MOCKS, offline
from test_http_provider import FixtureTransport


ORACLE = json.loads((ROOT / "tests/accuracy_expected.json").read_text(encoding="utf-8"))


class HandoverContract(unittest.TestCase):
    def setUp(self):
        guard = offline()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)

    def document(self, name):
        path = ROOT / name
        self.assertTrue(path.is_file(), f"Step 8 handover document is absent: {name}")
        return path.read_text(encoding="utf-8")

    def json_block(self, text, marker):
        found = re.search(r"<!-- " + re.escape(marker) + r" -->\s*```json\s*(.*?)```", text, flags=re.DOTALL)
        self.assertIsNotNone(found, f"Missing auditable JSON block: {marker}")
        return json.loads(found.group(1))

    def test_T44_existing_request_evidence_matches_the_actual_builder_without_a_real_key(self):
        stored = json.loads(self.document("deliverables/request-evidence.json"))
        settings = ProviderSettings(**json.loads((ROOT / "config/provider.json").read_text()))
        transport = FixtureTransport(__import__("intake.http_provider", fromlist=["HttpResponse"]), "success")
        secret = json.loads((MOCKS / "http/mock-secret.json").read_text())["value"]
        credentials = mock.Mock(return_value=secret)
        provider = HttpProvider(settings=settings, transport=transport, getenv=credentials)
        message = json.loads((ROOT / "materials/requests/IT-001.json").read_text())["message"]
        generated = provider.request_evidence(message=message)
        credentials.assert_not_called()
        provider.complete(message=message)
        credentials.assert_called_once_with("API_KEY")
        self.assertEqual(len(transport.calls), 1)
        actual = transport.calls[0]["request"]
        self.assertEqual(stored["request"]["url"], actual.url)
        self.assertEqual(stored["request"]["method"], actual.method)
        self.assertEqual(stored["request"]["body"], json.loads(actual.body.decode("utf-8")))
        for field in ("request", "configuration", "response_handling"):
            self.assertEqual(stored[field], generated[field])
        self.assertFalse(stored["observation"]["provider_call_performed"])
        self.assertEqual(stored["observation"]["kind"], "request-example-only")
        self.assertNotIn(secret, json.dumps(stored))

    def test_T44_accuracy_document_contains_all_expected_fields_and_source_evidence(self):
        text = self.document("deliverables/accuracy.md")
        self.assertRegex(text, r"<!-- accuracy-oracle:\s*[^>]+ -->")
        self.assertIn("accuracy_expected.json", text)
        sections = dict(re.findall(
            r"^### (IT-\d{3}\.json|SYN-001\.json)\s*\n(.*?)(?=^### |\Z)",
            text, flags=re.MULTILINE | re.DOTALL,
        ))
        self.assertEqual(set(sections), set(ORACLE["documents"]))
        for name, expected in ORACLE["documents"].items():
            with self.subTest(document=name):
                section = sections[name]
                block = re.search(r"```json\s*(.*?)```", section, flags=re.DOTALL)
                self.assertIsNotNone(block)
                self.assertEqual(json.loads(block.group(1)), expected["scalars"])
                self.assertIn(expected["source_file"], section)
                self.assertIn(expected["source_sha256"], section)
                for quote in expected["required_quotes"]:
                    self.assertIn(quote, section)
        for pattern in (
            r"252", r"exact.match", r"denominator", r"missing", r"null",
            r"schema", r"supporting_quotes|supporting quotes", r"issues", r"routing",
            r"mock", r"not.*live|does not.*live", r"configuration", r"factual.error",
        ):
            self.assertRegex(text, "(?is)" + pattern)

    def test_T44_source_inventory_readme_and_verification_describe_supported_limits(self):
        readme = self.document("README.md")
        verification = self.document("deliverables/verification.md")
        for name in (
            "requirements.lock", "snapshot.schema.json", "config/provider.json",
            "intake/extraction-prompt.txt", "intake/http_provider.py", "intake/accuracy.py",
            "intake/cli.py", "intake/state.py", "intake/workers.py", "intake/exports.py",
        ):
            self.assertTrue((ROOT / name).is_file(), f"Missing submission source: {name}")
        self.assertIn("env -u API_KEY python3 -m unittest discover -s tests", readme)
        self.assertRegex(readme, r"python3 -m intake[^\n]*accuracy")
        for value in ("API_KEY", "--provider", "--expected", "queue", "pending", "history", "retry", "resume"):
            self.assertIn(value, readme)
        for pattern in (
            r"one.*host|single.*host", r"SQLite", r"WAL", r"200.*400", r"2,?000",
            r"lease", r"fenc", r"interrupt|crash", r"capture.*once|once.*capture",
            r"retry", r"human", r"untested.*live|unmeasured.*live|no live|not.*live",
            r"network.filesystem|multi.host", r"R-78", r"R-79", r"R-80",
            r"Classroom", r"not performed|not submitted|external.*submission",
        ):
            self.assertRegex(verification, "(?is)" + pattern)

    def test_T44_recorded_observations_are_labelled_configured_and_link_to_evidence(self):
        verification = self.document("deliverables/verification.md")
        observations = self.json_block(verification, "verification-observations")["observations"]
        self.assertTrue(observations)
        names = {row["name"] for row in observations}
        self.assertTrue({"mock-intake", "mock-accuracy"} <= names)
        for row in observations:
            with self.subTest(observation=row["name"]):
                self.assertTrue(row["response_source"])
                self.assertTrue(row["configuration"]["provider"])
                self.assertTrue(row["configuration"]["model"])
                self.assertTrue(row["observed"])
                self.assertFalse(Path(row["evidence"]).is_absolute())
                self.assertTrue((ROOT / row["evidence"]).is_file())
        accuracy = next(row for row in observations if row["name"] == "mock-accuracy")
        self.assertEqual(accuracy["response_source"], "mock")
        self.assertEqual(accuracy["observed"]["scalar_correct"], 252)
        self.assertEqual(accuracy["observed"]["scalar_denominator"], 252)
        measured = json.loads((ROOT / accuracy["evidence"]).read_text())
        self.assertEqual(measured["scalar"], {"correct": 252, "denominator": 252})
        self.assertEqual(measured["observation_kind"], "mock-fixture-agreement")

    def test_T44_exact_documented_mock_commands_work_in_a_temporary_checkout(self):
        verification = self.document("deliverables/verification.md")
        match = re.search(
            r"<!-- runnable-mock-commands:start -->\s*```bash\s*(.*?)```\s*<!-- runnable-mock-commands:end -->",
            verification, flags=re.DOTALL,
        )
        self.assertIsNotNone(match, "Verification needs one labelled block of executable mock commands")
        lines = [line.strip() for line in match.group(1).splitlines() if line.strip() and not line.lstrip().startswith("#")]
        self.assertTrue(lines)
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder) / "checkout"
            workspace.mkdir()
            for name in ("intake", "tools", "materials", "tests", "deliverables", "config", "planning"):
                shutil.copytree(ROOT / name, workspace / name, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
            for name in ("README.md", "snapshot.schema.json", "requirements.lock"):
                shutil.copyfile(ROOT / name, workspace / name)
            environment = dict(os.environ)
            environment.pop("API_KEY", None)
            environment["PYTHONPATH"] = str(workspace / "tests/offline_site")
            environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", "")
            seen = set()
            run_database = None
            for line in lines:
                tokens = shlex.split(line)
                self.assertEqual(tokens[:5], ["env", "-u", "API_KEY", "python3", "-m"])
                self.assertFalse(any(token in {";", "&&", "|", ">", ">>"} for token in tokens))
                self.assertIn(tokens[5], {"intake", "tools.request_evidence"})
                result = subprocess.run(tokens, cwd=workspace, env=environment, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, f"{line}\n{result.stdout}\n{result.stderr}")
                if tokens[5] == "tools.request_evidence":
                    seen.add("request-evidence")
                    evidence = json.loads((workspace / "deliverables/request-evidence.json").read_text())
                    self.assertFalse(evidence["observation"]["provider_call_performed"])
                    continue
                commands = {"run", "queue", "pending", "history", "retry", "resume", "export", "accuracy"}
                command = next(token for token in tokens[6:] if token in commands)
                seen.add(command)
                output = json.loads(result.stdout)
                if command in {"run", "queue"}:
                    self.assertEqual(len(output), 20)
                    self.assertTrue(all(row["response_source"] == "mock" for row in output))
                    if command == "run":
                        self.assertIn("--provider", tokens)
                        self.assertEqual(tokens[tokens.index("--provider") + 1], "mock")
                        run_database = tokens[tokens.index("--db") + 1]
                elif command == "pending":
                    self.assertEqual(output, [])
                elif command in {"history", "retry"}:
                    self.assertTrue(output)
                    self.assertTrue(all(row["document_id"] == "IT-001.json" for row in output))
                elif command == "accuracy":
                    self.assertEqual(output["scalar"], {"correct": 252, "denominator": 252})
                    self.assertEqual(output["observation_kind"], "mock-fixture-agreement")
                    self.assertNotEqual(tokens[tokens.index("--db") + 1], run_database)
                elif command == "export":
                    if "--read" in tokens:
                        self.assertEqual(len(output["queue"]), 20)
                        self.assertEqual(len(output["extractions"]), 20)
                    else:
                        self.assertEqual(output["records"], 20)
            self.assertTrue({"run", "queue", "pending", "history", "retry", "resume", "export", "accuracy", "request-evidence"} <= seen)


if __name__ == "__main__":
    unittest.main()
