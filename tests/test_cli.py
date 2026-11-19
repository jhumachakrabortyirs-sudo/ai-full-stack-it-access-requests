"""T41: native CLI walkthrough in isolated, credential/network-blocked processes."""
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

from operation_support import ROOT, MOCKS, offline
from submission_support import cli_interrupt_process, prepare_wheelhouse


FACTS = json.loads((ROOT / "tests/expected_facts.json").read_text())["documents"]
ROUTES = json.loads((ROOT / "tests/cli_expected.json").read_text())["routes"]


class OperatorCLIContract(unittest.TestCase):
    def setUp(self):
        guard = offline()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.database = self.directory / "state.sqlite3"
        self.inputs = self.directory / "requests"
        shutil.copytree(ROOT / "materials/requests", self.inputs)
        self.mocks = self.directory / "mock_responses"
        shutil.copytree(MOCKS, self.mocks)
        self.env = dict(os.environ)
        self.env.pop("API_KEY", None)
        self.env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "tests/offline_site"), str(ROOT)])
        self.env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + self.env.get("PATH", "")
        self.env["PIP_NO_INDEX"] = "1"
        self.env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
        self.env["PIP_NO_CACHE_DIR"] = "1"

    def require_cli(self):
        self.assertIsNotNone(
            importlib.util.find_spec("intake.__main__"),
            "Step 7 CLI entry point intake.__main__ is absent",
        )

    def command(self, args, *, expected_code=0, executable=None, include_db=True):
        self.require_cli()
        prefix = ["--db", str(self.database)] if include_db else []
        result = subprocess.run(
            [executable or sys.executable, "-m", "intake", *prefix, *map(str, args)],
            cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, expected_code, f"stdout={result.stdout}\nstderr={result.stderr}")
        return result

    def cli(self, *args, expected_code=0, executable=None):
        result = self.command(args, expected_code=expected_code, executable=executable)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            self.fail(f"CLI stdout must be JSON; got {result.stdout!r}")

    def assert_twenty(self, records):
        self.assertIsInstance(records, list)
        self.assertEqual(len(records), 20)
        self.assertEqual({row["document_id"] for row in records}, set(ROUTES))
        for row in records:
            facts = FACTS[row["document_id"]]["scalars"]
            self.assertEqual({key: row["extraction"][key] for key in facts}, facts)
            self.assertEqual([row["status"], row["owner"], row["next_action"]], ROUTES[row["document_id"]])
            self.assertEqual(row["response_source"], "mock")
            self.assertIsNone(row["error"])
            self.assertTrue(row["attempt_id"])

    def test_T41_native_run_queue_pending_history_and_unchanged_rerun(self):
        records = self.cli("run", self.inputs, "--provider", "mock", "--operator", "operator-a", "--workers", "3")
        self.assert_twenty(records)
        current = self.cli("queue")
        self.assert_twenty(current)
        self.assertEqual(self.cli("pending"), [])
        first_ids = {row["document_id"]: row["attempt_id"] for row in current}
        rerun = self.cli("run", self.inputs, "--provider", "mock", "--operator", "operator-b")
        self.assertEqual({row["document_id"]: row["attempt_id"] for row in rerun}, first_ids)
        for name in ROUTES:
            history = self.cli("history", "--document", name)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["attempt_id"], first_ids[name])

    def test_T41_targeted_retry_command_is_replayable_for_the_same_db_and_mock_configuration(self):
        initial = self.cli("run", self.inputs, "--provider", "mock", "--operator", "operator-a")
        self.assert_twenty(initial)
        previous = {row["document_id"]: row for row in initial}
        (self.mocks / "catalog.json").write_bytes((MOCKS / "cli/failure-catalog.json").read_bytes())
        # Retry must use the recorded source, not require the input export.
        shutil.rmtree(self.inputs)
        failed = self.cli(
            "retry", "--document", "IT-001.json", "--provider", "mock",
            "--mock-dir", self.mocks, "--operator", "retry-operator", expected_code=1,
        )
        self.assertEqual(len(failed), 1)
        error = failed[0]
        self.assertEqual([error["status"], error["owner"], error["next_action"]], ["error", "IT Service Desk", "retry-extraction"])
        self.assertEqual(error["error_category"], "timeout")
        self.assertIsNone(error["extraction"])
        tokens = shlex.split(error["retry_command"])
        self.assertEqual(tokens[:3], ["python3", "-m", "intake"])
        self.assertIn("retry", tokens)
        self.assertEqual(tokens[tokens.index("--db") + 1], str(self.database))
        self.assertEqual(tokens[tokens.index("--document") + 1], "IT-001.json")
        self.assertEqual(tokens[tokens.index("--mock-dir") + 1], str(self.mocks))
        self.assertIn(error["retry_command"], error["error"])
        current = {row["document_id"]: row for row in self.cli("queue")}
        self.assertEqual(current["IT-001.json"]["attempt_id"], error["attempt_id"])
        for name in set(ROUTES) - {"IT-001.json"}:
            self.assertEqual(current[name]["attempt_id"], previous[name]["attempt_id"])
        history = self.cli("history", "--document", "IT-001.json")
        self.assertEqual([row["status"] for row in history], ["review", "error"])
        self.assertEqual(history[0]["extraction"]["permission"], "read")
        # Repair the explicit mock catalog, then execute exactly the emitted
        # shell-tokenized command using the activated-runtime PATH.
        (self.mocks / "catalog.json").write_bytes((MOCKS / "catalog.json").read_bytes())
        repaired = subprocess.run(tokens, cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        recovered = json.loads(repaired.stdout)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["document_id"], "IT-001.json")
        self.assertIsNone(recovered[0]["error"])
        self.assert_twenty(self.cli("queue"))
        self.assertEqual(len(self.cli("history", "--document", "IT-001.json")), 3)

    def test_T41_native_queue_and_pending_are_available_during_interrupted_cli_intake(self):
        self.require_cli()
        # This child injects event-controlled fixtures into the real parser;
        # inspection and recovery run through native `python -m intake`.
        folder = self.directory / "interrupt-inputs"
        folder.mkdir()
        for number in (1, 2):
            shutil.copyfile(self.inputs / f"IT-{number:03}.json", folder / f"IT-{number:03}.json")
        context = multiprocessing.get_context("spawn")
        ready, release = context.Event(), context.Event()
        process = context.Process(target=cli_interrupt_process, args=(self.database, folder, ready, release))
        process.start()

        def cleanup():
            if process.is_alive():
                process.kill()
            process.join(timeout=5)

        self.addCleanup(cleanup)
        self.assertTrue(ready.wait(timeout=10))
        current = self.cli("queue")
        self.assertEqual([row["document_id"] for row in current], ["IT-001.json"])
        pending = self.cli("pending")
        self.assertEqual([row["document_id"] for row in pending], ["IT-002.json"])
        self.assertEqual(pending[0]["lease_owner"], "interrupted-cli")
        process.kill()
        process.join(timeout=10)
        shutil.rmtree(folder)
        recovered = self.cli("resume", "--provider", "mock", "--operator", "recovered-cli", "--workers", "2")
        self.assertEqual([row["document_id"] for row in recovered], ["IT-002.json"])
        current_after = {row["document_id"]: row for row in self.cli("queue")}
        self.assertEqual(current_after["IT-001.json"]["attempt_id"], current[0]["attempt_id"])
        self.assertEqual(current_after["IT-002.json"]["owner"], "Security Review")
        self.assertEqual(self.cli("pending"), [])

    def test_T41_unknown_mock_input_never_falls_back_to_a_live_provider(self):
        records = self.cli("run", ROOT / "tests/inputs/cli", "--provider", "mock", expected_code=1)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["document_id"], "UNKNOWN-001.json")
        self.assertEqual(records[0]["status"], "error")
        self.assertEqual(records[0]["response_source"], "mock")
        self.assertEqual(records[0]["error_category"], "mock-exhausted")
        self.assertIsNone(records[0]["extraction"])

    def test_T41_help_exposes_operator_commands_and_bad_arguments_are_actionable(self):
        help_result = self.command(["--help"], include_db=False)
        for command in ["run", "queue", "pending", "history", "retry", "resume", "export"]:
            self.assertIn(command, help_result.stdout)
        bad = self.command(["not-a-command"], expected_code=2)
        self.assertIn("usage", bad.stderr.casefold())
        self.assertIn("not-a-command", bad.stderr)

    def test_T41_locked_dependencies_install_from_a_prepared_offline_wheelhouse(self):
        self.require_cli()
        wheelhouse = self.directory / "wheelhouse"
        prepare_wheelhouse(ROOT / "requirements.lock", wheelhouse)
        environment = self.directory / "fresh-runtime"
        base = sys._base_executable
        created = subprocess.run(
            [base, "-m", "venv", "--without-pip", str(environment)],
            cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        installed = subprocess.run([
            base, "-m", "pip", "--python", str(environment), "install", "--no-index",
            "--find-links", str(wheelhouse), "-r", str(ROOT / "requirements.lock"),
        ], cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        # Use the application with those installed packages and real entrypoint.
        records = self.cli("run", self.inputs, "--provider", "mock", executable=str(environment / "bin/python3"))
        self.assert_twenty(records)

    def test_T41_export_and_read_commands_use_one_captured_generation(self):
        self.assert_twenty(self.cli("run", self.inputs, "--provider", "mock"))
        output = self.directory / "deliverables"
        published = self.cli("export", "--output", output)
        self.assertTrue(published["generation"])
        captured = self.cli("export", "--output", output, "--read")
        self.assertEqual(captured["generation"], published["generation"])
        self.assertEqual(len(captured["queue"]), 20)
        self.assert_twenty(captured["extractions"])
        queue = {row["document_id"]: row for row in captured["queue"]}
        for extraction in captured["extractions"]:
            row = queue[extraction["document_id"]]
            self.assertEqual(row["attempt_id"], extraction["attempt_id"])
            self.assertEqual(row["source_sha256"], extraction["source_sha256"])


if __name__ == "__main__":
    unittest.main()
