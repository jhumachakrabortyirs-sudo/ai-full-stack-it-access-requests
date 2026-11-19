"""T42: current exports from one DB snapshot and atomic generation publication.

Contract: publish(database, output, checkpoint=None) returns a snapshot with
generation/directory attributes. capture(output) resolves the current reference
once; snapshot.read() returns (queue_rows, extraction_rows). Readers keep that
snapshot for both files. Checkpoints are an API test seam, not CLI fault flags.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib
import json
import multiprocessing
from pathlib import Path
import tempfile
import threading
import unittest

from jsonschema import Draft202012Validator

from intake.providers import ScriptedProvider
from intake.state import ProcessingConfig, StateStore
from operation_support import ROOT, MOCKS, POLICY, FixtureProvider, offline
from submission_support import export_crash_process


SCHEMA = json.loads((ROOT / "snapshot.schema.json").read_text())
FACTS = json.loads((ROOT / "tests/expected_facts.json").read_text())["documents"]
REQUIRED = {
    "document_id", "source_sha256", "extraction", "status", "owner",
    "next_action", "error", "response_source", "attempt_id",
}
LINKED = (
    "document_id", "source_sha256", "attempt_id", "status", "owner", "next_action",
    "error", "response_source", "processing_fingerprint", "policy_sha256",
    "extraction_attempt_id", "routing_attempt_id",
)


class ExportGenerationContract(unittest.TestCase):
    def setUp(self):
        guard = offline()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.database = self.directory / "state.sqlite3"
        self.output = self.directory / "deliverables"
        self.output.mkdir()
        # Publishing must coexist with earlier handover artifacts.
        self.evidence = self.output / "request-evidence.json"
        self.evidence.write_text('{"retained": true}\n')
        self.paths = [ROOT / "materials/requests/IT-001.json", ROOT / "materials/requests/IT-002.json"]
        provider = FixtureProvider()
        self.config = ProcessingConfig.capture(provider=provider, policy_path=POLICY)
        with StateStore(self.database, clock=lambda: 1000.0) as store:
            self.initial = store.intake(self.paths, provider=provider, config=self.config, operator="initial")

    def api(self):
        try:
            return importlib.import_module("intake.exports")
        except ModuleNotFoundError as exc:
            self.fail(f"Step 7 export publication is absent: {exc}")

    def fail_first(self):
        provider = ScriptedProvider(mock_dir=MOCKS, script=[{"error": "timeout.json"}])
        with StateStore(self.database, clock=lambda: 1000.0) as store:
            failed = store.intake([self.paths[0]], provider=provider, config=self.config, operator="retry", force=True)[0]
            self.assertEqual(failed["status"], "error")
            self.assertEqual(len(store.history("IT-001.json")), 2)
            return store.current()

    def assert_pair(self, snapshot, expected):
        queue_rows, extraction_rows = snapshot.read()
        self.assertTrue(snapshot.generation)
        self.assertIsInstance(snapshot.directory, Path)
        expected_by_id = {row["document_id"]: row for row in expected}
        queue_by_id = {row["document_id"]: row for row in queue_rows}
        extraction_by_id = {row["document_id"]: row for row in extraction_rows}
        self.assertEqual(len(extraction_rows), len(expected_by_id))
        self.assertEqual(len(queue_rows), len(expected_by_id))
        self.assertEqual(set(queue_by_id), set(expected_by_id))
        self.assertEqual(set(extraction_by_id), set(expected_by_id))
        for name, expected_record in expected_by_id.items():
            extraction = extraction_by_id[name]
            queue = queue_by_id[name]
            self.assertTrue(REQUIRED <= set(extraction))
            for field in LINKED:
                self.assertEqual(queue[field], extraction[field], (name, field))
                self.assertEqual(extraction[field], expected_record[field], (name, field))
            if extraction["status"] == "error":
                self.assertIsNone(extraction["extraction"])
                self.assertTrue(extraction["error"])
                self.assertEqual([extraction["owner"], extraction["next_action"]], ["IT Service Desk", "retry-extraction"])
            else:
                self.assertIsNone(extraction["error"])
                Draft202012Validator(SCHEMA).validate(extraction["extraction"])
                facts = FACTS[name]["scalars"]
                self.assertEqual({key: extraction["extraction"][key] for key in facts}, facts)
        return queue_rows, extraction_rows

    def test_T42_current_files_link_by_source_attempt_and_provenance_and_exclude_history(self):
        exports = self.api()
        old = exports.publish(self.database, self.output)
        self.assert_pair(old, self.initial)
        old_bytes = {name: (old.directory / name).read_bytes() for name in ("queue.json", "extractions.jsonl")}
        updated = self.fail_first()
        new = exports.publish(self.database, self.output)
        self.assertNotEqual(new.generation, old.generation)
        self.assertNotEqual(new.directory, old.directory)
        self.assert_pair(new, updated)
        self.assert_pair(old, self.initial)
        for name, raw in old_bytes.items():
            self.assertEqual((old.directory / name).read_bytes(), raw, "previous generation was rewritten")
        current = exports.capture(self.output)
        self.assertEqual(current.generation, new.generation)
        self.assert_pair(current, updated)
        for name in ("queue.json", "extractions.jsonl"):
            self.assertEqual((self.output / name).resolve().parent, current.directory.resolve())
        extracted = [json.loads(line) for line in (self.output / "extractions.jsonl").read_text().splitlines()]
        self.assertEqual(len(extracted), 2)
        self.assertEqual(sum(row["document_id"] == "IT-001.json" for row in extracted), 1)
        self.assertEqual(next(row for row in extracted if row["document_id"] == "IT-001.json")["status"], "error")
        self.assertEqual(json.loads(self.evidence.read_text()), {"retained": True})

    def test_T42_one_database_snapshot_survives_a_write_after_capture(self):
        exports = self.api()
        captured, release = threading.Event(), threading.Event()

        def checkpoint(stage, generation):
            if stage == "snapshot-captured":
                captured.set()
                if not release.wait(timeout=15):
                    raise AssertionError("snapshot capture was not released")

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(exports.publish, self.database, self.output, checkpoint=checkpoint)
            try:
                self.assertTrue(captured.wait(timeout=10))
                updated = self.fail_first()
            finally:
                release.set()
            snapshot = future.result(timeout=10)
        self.assert_pair(snapshot, self.initial)
        latest = exports.publish(self.database, self.output)
        self.assert_pair(latest, updated)

    def test_T42_reader_pins_reference_once_before_a_writer_switches_generations(self):
        exports = self.api()
        first = exports.publish(self.database, self.output)
        queue_opened, continue_reading = threading.Event(), threading.Event()

        def reader():
            snapshot = exports.capture(self.output)
            rows = json.loads((snapshot.directory / "queue.json").read_text())
            queue_opened.set()
            if not continue_reading.wait(timeout=15):
                raise AssertionError("reader barrier was not released")
            extractions = [json.loads(line) for line in (snapshot.directory / "extractions.jsonl").read_text().splitlines()]
            return snapshot, rows, extractions

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(reader)
            try:
                self.assertTrue(queue_opened.wait(timeout=10))
                updated = self.fail_first()
                second = exports.publish(self.database, self.output)
                self.assertNotEqual(first.generation, second.generation)
            finally:
                continue_reading.set()
            snapshot, queue_rows, extraction_rows = future.result(timeout=10)
        self.assertEqual(snapshot.generation, first.generation)
        expected_ids = {row["document_id"]: row["attempt_id"] for row in self.initial}
        self.assertEqual({row["document_id"]: row["attempt_id"] for row in queue_rows}, expected_ids)
        self.assertEqual({row["document_id"]: row["attempt_id"] for row in extraction_rows}, expected_ids)
        self.assert_pair(exports.capture(self.output), updated)

    def test_T42_failed_generation_write_leaves_the_prior_generation_readable(self):
        exports = self.api()
        old = exports.publish(self.database, self.output)
        self.fail_first()

        def checkpoint(stage, generation):
            if stage == "queue-written":
                raise OSError("Authored publication failure before both files are ready")

        with self.assertRaises(OSError):
            exports.publish(self.database, self.output, checkpoint=checkpoint)
        pinned = exports.capture(self.output)
        self.assertEqual(pinned.generation, old.generation)
        self.assert_pair(pinned, self.initial)

    def test_T42_actual_publisher_crashes_expose_old_or_new_whole_generation(self):
        exports = self.api()
        old = exports.publish(self.database, self.output)
        updated = self.fail_first()
        context = multiprocessing.get_context("spawn")
        stages = [
            "snapshot-captured", "generation-created", "queue-written", "extractions-written",
            "generation-ready", "before-publish", "published",
        ]
        for stage in stages:
            with self.subTest(stage=stage):
                ready, release = context.Event(), context.Event()
                process = context.Process(target=export_crash_process, args=(self.database, self.output, stage, ready, release))
                process.start()
                try:
                    self.assertTrue(ready.wait(timeout=10), f"publisher never reached {stage}")
                    process.kill()
                    process.join(timeout=10)
                    self.assertIsNotNone(process.exitcode)
                    self.assertNotEqual(process.exitcode, 0)
                finally:
                    if process.is_alive():
                        process.kill()
                        process.join(timeout=5)
                snapshot = exports.capture(self.output)
                if stage == "published":
                    self.assertNotEqual(snapshot.generation, old.generation)
                    self.assert_pair(snapshot, updated)
                else:
                    self.assertEqual(snapshot.generation, old.generation)
                    self.assert_pair(snapshot, self.initial)


if __name__ == "__main__":
    unittest.main()
