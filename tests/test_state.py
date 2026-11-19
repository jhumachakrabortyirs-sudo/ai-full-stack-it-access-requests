"""Step 5, T31–T36: durable current results, immutable history and refreshes.

StateStore returns dictionaries suitable for later CLI/export serialization.
history(document_id) includes all completed attempts, including the current
one; pending() is separate. Source/configuration snapshots are retrievable by
attempt ID. Multi-process races, lease renewal/reclamation and stale fencing
are step 6 tests; T36 only requires a durable claim and close/reopen behavior.
"""
from contextlib import ExitStack
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import tempfile
import unittest
from unittest import mock

from intake import extraction, http_provider, prompts
from intake.providers import ScriptedProvider
from test_http_provider import FixtureTransport


ROOT = Path(__file__).resolve().parents[1]
MOCKS = ROOT / "tests/mock_responses"
ORACLE = json.loads((ROOT / "tests/expected_facts.json").read_text(encoding="utf-8"))["documents"]
REVISIONS = json.loads((ROOT / "tests/expected_revisions.json").read_text(encoding="utf-8"))


class DurableStateContract(unittest.TestCase):
    def setUp(self):
        original_getitem = type(os.environ).__getitem__

        def forbid_key_read(environment, name):
            if name == "API_KEY":
                raise AssertionError("state test read the real credential environment")
            return original_getitem(environment, name)

        for patch in [
            mock.patch.object(type(os.environ), "__getitem__", forbid_key_read),
            mock.patch.object(socket, "socket", side_effect=AssertionError("network forbidden")),
            mock.patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")),
        ]:
            patch.start()
            self.addCleanup(patch.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.inputs = self.directory / "inputs"
        self.inputs.mkdir()
        for number in (1, 2, 3):
            name = f"IT-{number:03}.json"
            shutil.copyfile(ROOT / "materials/requests" / name, self.inputs / name)
        self.policy = self.directory / "office-policy.md"
        shutil.copyfile(ROOT / "materials/office-policy.md", self.policy)
        self.clock = lambda: 1000.0

    def api(self):
        try:
            return importlib.import_module("intake.state")
        except ModuleNotFoundError as exc:
            self.fail(f"Step 5 durable state is absent: {exc}")

    def store(self, name="state.sqlite3"):
        store = self.api().StateStore(self.directory / name, clock=self.clock)
        self.addCleanup(store.close)
        return store

    def provider(self, *replies):
        script = [{"error": name} if name == "timeout.json" else {"reply": name} for name in replies]
        return ScriptedProvider(mock_dir=MOCKS, script=script)

    def config(self, provider):
        return self.api().ProcessingConfig.capture(provider=provider, policy_path=self.policy)

    def intake(self, store, provider, paths, *, config=None, operator="operator-a", force=False):
        return store.intake(
            paths, provider=provider, config=config or self.config(provider),
            operator=operator, force=force,
        )

    def assert_metadata(self, record, config, operator="operator-a"):
        self.assertTrue(record["attempt_id"])
        self.assertGreater(record["source_revision"], 0)
        self.assertGreater(record["ingestion_sequence"], 0)
        self.assertEqual(record["state"], "completed")
        self.assertEqual(record["operator"], operator)
        for field in ["extraction_fingerprint", "processing_fingerprint", "prompt_sha256", "schema_sha256", "policy_sha256"]:
            self.assertRegex(record[field], r"^[a-f0-9]{64}$")
            self.assertEqual(record[field], getattr(config, field))
        self.assertEqual(record["model_configuration"], config.model_configuration)
        self.assertEqual(record["routing_attempt_id"], record["attempt_id"])
        self.assertTrue(record["extraction_attempt_id"])

    def assert_facts(self, record, name="IT-001.json", *, facts=None, raw=None, owner="System Owner", response_source="mock"):
        expected = ORACLE[name]["scalars"] if facts is None else facts
        self.assertEqual(record["document_id"], name)
        self.assertEqual({key: record["extraction"][key] for key in expected}, expected)
        self.assertEqual(record["response_source"], response_source)
        self.assertIsNone(record["error"])
        self.assertEqual(record["status"], "review")
        self.assertEqual(record["owner"], owner)
        action = {"Security Review": "review-risk-then-owner", "IT Service Desk": "confirm-withdrawal"}.get(owner, "review-request")
        self.assertEqual(record["next_action"], action)
        if raw is not None:
            self.assertEqual(record["source_sha256"], hashlib.sha256(raw).hexdigest())

    def test_T31_unchanged_successes_are_reused_across_reopen_without_provider_calls(self):
        store = self.store()
        provider = self.provider("complete-001.json", "complete-002.json")
        config = self.config(provider)
        paths = [self.inputs / "IT-001.json", self.inputs / "IT-002.json"]
        first = self.intake(store, provider, paths, config=config)
        self.assertEqual(provider.call_count, 2)
        for record, path in zip(first, paths):
            self.assert_metadata(record, config)
            self.assertEqual(record["attempt_kind"], "extraction")
            self.assertEqual(record["extraction_attempt_id"], record["attempt_id"])
            self.assertEqual(store.source(record["attempt_id"]).original_bytes, path.read_bytes())
            self.assertEqual(len(store.history(path.name)), 1)
        self.assert_facts(first[0], raw=paths[0].read_bytes())
        self.assert_facts(first[1], "IT-002.json", raw=paths[1].read_bytes(), owner="Security Review")
        store.close()
        reopened = self.store()
        forbidden = self.provider()
        with mock.patch.object(forbidden, "complete", side_effect=AssertionError("unchanged success called provider")) as complete:
            second = self.intake(reopened, forbidden, paths, config=config, operator="operator-b")
            complete.assert_not_called()
        self.assertEqual(second, first, "reuse must retain original attempt/operator provenance")
        current = reopened.current()
        self.assertEqual(len(current), 2)
        self.assertEqual({row["document_id"] for row in current}, {path.name for path in paths})
        for record in second:
            self.assertEqual(reopened.history(record["document_id"]), [record])

    def test_T32_changed_source_creates_a_new_revision_and_keeps_original_bytes(self):
        store = self.store()
        path = self.inputs / "IT-001.json"
        original_raw = path.read_bytes()
        provider = self.provider("complete-001.json", "changed-001.json")
        config = self.config(provider)
        first = self.intake(store, provider, [path], config=config)[0]
        revised_raw = (ROOT / "tests/inputs/revisions/IT-001.json").read_bytes()
        path.write_bytes(revised_raw)
        second = self.intake(store, provider, [path], config=config)[0]
        self.assert_facts(first, raw=original_raw)
        self.assert_facts(second, facts=REVISIONS["changed-001"]["scalars"], raw=revised_raw)
        self.assertNotEqual(first["attempt_id"], second["attempt_id"])
        self.assertNotEqual(first["source_sha256"], second["source_sha256"])
        self.assertGreater(second["source_revision"], first["source_revision"])
        self.assertGreater(second["ingestion_sequence"], first["ingestion_sequence"])
        self.assertEqual(store.current(), [second])
        self.assertEqual(store.history(path.name), [first, second])
        self.assertEqual(store.source(first["attempt_id"]).original_bytes, original_raw)
        self.assertEqual(store.source(second["attempt_id"]).original_bytes, revised_raw)
        self.assertEqual(provider.call_count, 2)

    def test_T32_json_reformat_alone_changes_source_identity_and_invalidates_reuse(self):
        store = self.store()
        path = self.inputs / "IT-001.json"
        original_raw = path.read_bytes()
        provider = self.provider("complete-001.json", "complete-001.json")
        first = self.intake(store, provider, [path])[0]
        rewritten = json.dumps(json.loads(original_raw.decode("utf-8")), separators=(",", ":")).encode("utf-8")
        self.assertNotEqual(original_raw, rewritten)
        path.write_bytes(rewritten)
        second = self.intake(store, provider, [path])[0]
        self.assert_facts(second, raw=rewritten)
        self.assertEqual(first["extraction"], second["extraction"])
        self.assertNotEqual(first["source_sha256"], second["source_sha256"])
        self.assertNotEqual(first["attempt_id"], second["attempt_id"])
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(store.history(path.name), [first, second])

    def test_T33_forced_failed_retry_is_current_and_prior_success_is_only_history(self):
        for changed in (False, True):
            with self.subTest(changed=changed):
                store = self.store(f"retry-{changed}.sqlite3")
                path = self.inputs / "IT-001.json"
                shutil.copyfile(ROOT / "materials/requests/IT-001.json", path)
                original_raw = path.read_bytes()
                successful = self.provider("complete-001.json")
                config = self.config(successful)
                first = self.intake(store, successful, [path], config=config)[0]
                if changed:
                    shutil.copyfile(ROOT / "tests/inputs/revisions/IT-001.json", path)
                failed_provider = self.provider("timeout.json")
                failed = self.intake(store, failed_provider, [path], config=config, operator="operator-b", force=True)[0]
                self.assert_metadata(failed, config, "operator-b")
                self.assertEqual((failed["status"], failed["owner"], failed["next_action"]), ("error", "IT Service Desk", "retry-extraction"))
                self.assertIsNone(failed["extraction"])
                self.assertEqual(failed["error_category"], "timeout")
                self.assertIn("IT-001.json", failed["error"])
                self.assertIn("retry", failed["error"])
                self.assertNotEqual(failed["attempt_id"], first["attempt_id"])
                self.assertEqual(failed["source_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
                self.assertEqual(failed["source_sha256"] == first["source_sha256"], not changed)
                self.assertEqual(store.current(), [failed])
                self.assertEqual(store.history(path.name), [first, failed])
                self.assert_facts(first, raw=original_raw)
                self.assertEqual(store.source(first["attempt_id"]).original_bytes, original_raw)
                self.assertEqual(failed_provider.call_count, 1)
                # A latest failure is not a reusable earlier success. A normal
                # rerun retries and appends a new attempt rather than hiding it.
                recovery = self.provider("changed-001.json" if changed else "complete-001.json")
                recovered = self.intake(store, recovery, [path], config=config)[0]
                expected = REVISIONS["changed-001"]["scalars"] if changed else ORACLE[path.name]["scalars"]
                self.assert_facts(recovered, facts=expected, raw=path.read_bytes())
                self.assertEqual(recovery.call_count, 1)
                self.assertEqual(store.current(), [recovered])
                self.assertEqual(store.history(path.name), [first, failed, recovered])

    def http(self, **overrides):
        values = json.loads((ROOT / "tests/inputs/provider-settings.json").read_text(encoding="utf-8"))
        values.update(overrides)
        transport = FixtureTransport(http_provider, "success")
        sentinel = json.loads((MOCKS / "http/mock-secret.json").read_text(encoding="utf-8"))["value"]
        provider = http_provider.HttpProvider(
            settings=http_provider.ProviderSettings(**values), transport=transport,
            getenv=mock.Mock(return_value=sentinel),
            sleep=mock.Mock(side_effect=AssertionError("successful fixture must not retry")),
        )
        return provider, transport

    def test_T34_model_generation_prompt_and_schema_refresh_record_actual_configuration(self):
        for change in ("model", "generation", "prompt", "schema"):
            with self.subTest(change=change):
                store = self.store(f"settings-{change}.sqlite3")
                path = self.inputs / "IT-001.json"
                provider, transport = self.http()
                before_config = self.config(provider)
                first = self.intake(store, provider, [path], config=before_config)[0]
                self.assertEqual(len(transport.calls), 1)
                overrides = {}
                if change == "model":
                    overrides["model"] = "fixture-model-v2"
                elif change == "generation":
                    overrides["generation"] = {"temperature": 0.25, "top_p": 1, "max_tokens": 1024}
                refreshed_provider, refreshed_transport = self.http(**overrides)
                with ExitStack() as patches:
                    prompt_path = self.directory / f"{change}-prompt.txt"
                    schema_path = self.directory / f"{change}-schema.json"
                    if change == "prompt":
                        prompt_path.write_text(prompts.PROMPT_PATH.read_text(encoding="utf-8") + "\nPreserve exact source spelling.\n", encoding="utf-8")
                        patches.enter_context(mock.patch.object(prompts, "PROMPT_PATH", prompt_path))
                    if change == "schema":
                        schema = json.loads(prompts.SCHEMA_PATH.read_text(encoding="utf-8"))
                        schema["description"] = "Authored revision used to verify schema refresh provenance"
                        schema_path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
                        for module in (prompts, http_provider, extraction):
                            patches.enter_context(mock.patch.object(module, "SCHEMA_PATH", schema_path))
                    after_config = self.config(refreshed_provider)
                    second = self.intake(store, refreshed_provider, [path], config=after_config)[0]
                self.assert_metadata(first, before_config)
                self.assert_metadata(second, after_config)
                self.assert_facts(second, raw=path.read_bytes(), response_source=refreshed_provider.response_source)
                self.assertNotEqual(first["attempt_id"], second["attempt_id"])
                self.assertNotEqual(first["extraction_fingerprint"], second["extraction_fingerprint"])
                self.assertNotEqual(first["processing_fingerprint"], second["processing_fingerprint"])
                self.assertEqual(first["source_revision"], second["source_revision"])
                self.assertEqual(first["policy_sha256"], second["policy_sha256"])
                self.assertEqual(second["attempt_kind"], "extraction")
                self.assertEqual(second["extraction_attempt_id"], second["attempt_id"])
                self.assertEqual(len(refreshed_transport.calls), 1)
                actual = json.loads(refreshed_transport.calls[0]["request"].body.decode("utf-8"))
                saved = store.configuration(second["attempt_id"])
                self.assertEqual(saved["model_configuration"], after_config.model_configuration)
                self.assertEqual(actual["model"], second["model_configuration"]["model"])
                for key, value in second["model_configuration"]["generation"].items():
                    self.assertEqual(actual[key], value)
                if change == "prompt":
                    self.assertIn("Preserve exact source spelling.", actual["messages"][0]["content"])
                    self.assertEqual(saved["prompt_text"], prompt_path.read_text(encoding="utf-8"))
                    self.assertEqual(second["prompt_sha256"], hashlib.sha256(prompt_path.read_bytes()).hexdigest())
                if change == "schema":
                    self.assertEqual(actual["response_format"]["json_schema"]["schema"], schema)
                    self.assertEqual(saved["schema_text"], schema_path.read_text(encoding="utf-8"))
                    self.assertEqual(second["schema_sha256"], hashlib.sha256(schema_path.read_bytes()).hexdigest())
                self.assertEqual(store.history(path.name), [first, second])
                self.assertEqual(store.current(), [second])

    def test_T34_policy_only_refresh_reuses_extraction_with_separate_routing_provenance(self):
        store = self.store()
        path = self.inputs / "IT-001.json"
        provider = self.provider("complete-001.json")
        original_policy = self.policy.read_text(encoding="utf-8")
        before_config = self.config(provider)
        first = self.intake(store, provider, [path], config=before_config)[0]
        self.assertEqual(provider.call_count, 1)
        self.policy.write_text(original_policy.replace("System Owner", "Staging Review"), encoding="utf-8")
        forbidden = self.provider()
        after_config = self.config(forbidden)
        self.assertEqual(before_config.extraction_fingerprint, after_config.extraction_fingerprint)
        self.assertNotEqual(before_config.processing_fingerprint, after_config.processing_fingerprint)
        with mock.patch.object(forbidden, "complete", side_effect=AssertionError("policy-only refresh called model")) as complete:
            second = self.intake(store, forbidden, [path], config=after_config)[0]
            complete.assert_not_called()
        self.assert_facts(second, owner="Staging Review", raw=path.read_bytes())
        self.assert_metadata(second, after_config)
        self.assertEqual(second["attempt_kind"], "routing")
        self.assertEqual(second["extraction_attempt_id"], first["attempt_id"])
        self.assertEqual(second["source_revision"], first["source_revision"])
        self.assertNotEqual(second["routing_attempt_id"], first["routing_attempt_id"])
        self.assertEqual(second["extraction"], first["extraction"])
        self.assertEqual(store.configuration(first["attempt_id"])["policy_text"], original_policy)
        self.assertEqual(store.configuration(second["attempt_id"])["policy_text"], self.policy.read_text(encoding="utf-8"))
        self.assertEqual(store.history(path.name), [first, second])
        self.assertEqual(store.current(), [second])
        # Mutating a returned historical dictionary must not mutate the DB.
        external = store.history(path.name)
        external[0]["extraction"]["permission"] = "admin"
        self.assertEqual(store.history(path.name), [first, second])

    def test_T35_incremental_batches_preserve_absent_documents_and_duplicate_request_ids(self):
        store = self.store()
        initial_provider = self.provider("complete-001.json", "complete-002.json")
        first = self.intake(store, initial_provider, [self.inputs / "IT-001.json", self.inputs / "IT-002.json"])
        third = self.intake(store, self.provider("complete-003.json"), [self.inputs / "IT-003.json"])[0]
        self.assertEqual(third["status"], "needs-information")
        self.assertEqual({row["document_id"]: row for row in store.current()}, {row["document_id"]: row for row in [*first, third]})
        thread_paths = [ROOT / "tests/inputs/revisions" / name for name in REVISIONS["thread-withdrawal"]["document_ids"]]
        provider = self.provider("thread-withdrawal.json", "thread-withdrawal.json")
        threads = self.intake(store, provider, thread_paths)
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(len({row["attempt_id"] for row in threads}), 2)
        self.assertEqual(len({row["source_sha256"] for row in threads}), 1)
        for row, path in zip(threads, thread_paths):
            self.assert_facts(row, path.name, facts=REVISIONS["thread-withdrawal"]["scalars"], raw=path.read_bytes(), owner="IT Service Desk")
            self.assertEqual(row["next_action"], "confirm-withdrawal")
            self.assertEqual(row["extraction"]["request_id"], "IT-THREAD")
            issues = "\n".join(row["extraction"]["issues"])
            self.assertIn("IT-THREAD", issues)
            self.assertIn(path.stem, issues)
            self.assertEqual(store.history(path.name), [row])
        self.assertEqual(len(store.current()), 5)
        for row in first:
            self.assertEqual(store.history(row["document_id"]), [row])

    def test_T36_claim_and_source_configuration_snapshots_survive_close_and_reopen(self):
        store = self.store()
        provider = self.provider("complete-001.json", "complete-002.json")
        config = self.config(provider)
        completed = self.intake(store, provider, [self.inputs / "IT-001.json"], config=config)[0]
        path = self.inputs / "IT-002.json"
        original_raw = path.read_bytes()
        claim = store.claim(path, config=config, operator="operator-a", lease_seconds=60)
        self.assertEqual(provider.call_count, 1, "claim must not call the provider")
        self.assertEqual(claim["document_id"], path.name)
        self.assertEqual(claim["source_sha256"], hashlib.sha256(original_raw).hexdigest())
        self.assertEqual(claim["lease_owner"], "operator-a")
        self.assertEqual(claim["lease_expires_at"], 1060.0)
        self.assertGreater(claim["fencing_token"], 0)
        self.assertTrue(claim["attempt_id"])
        self.assertEqual(claim["processing_fingerprint"], config.processing_fingerprint)
        store.close()
        # Claims are self-contained: recovery must not depend on the source
        # file or policy file still having the same contents on disk.
        path.unlink()
        self.policy.write_text("Policy file replaced after claim", encoding="utf-8")
        reopened = self.store()
        self.assertEqual(reopened.current(), [completed])
        self.assertEqual(reopened.pending(), [claim])
        self.assertEqual(reopened.history("IT-001.json"), [completed])
        self.assertEqual(reopened.history("IT-002.json"), [])
        self.assertEqual(reopened.source(claim["attempt_id"]).original_bytes, original_raw)
        saved = reopened.configuration(claim["attempt_id"])
        self.assertEqual(saved["model_configuration"], config.model_configuration)
        self.assertEqual(hashlib.sha256(saved["policy_text"].encode("utf-8")).hexdigest(), config.policy_sha256)
        with sqlite3.connect(self.directory / "state.sqlite3") as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        finished = reopened.run_claim(claim, provider=provider)
        self.assert_facts(finished, "IT-002.json", raw=original_raw, owner="Security Review")
        self.assertEqual(finished["attempt_id"], claim["attempt_id"])
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(reopened.pending(), [])
        self.assertEqual(reopened.history("IT-001.json"), [completed])
        self.assertEqual(len(reopened.current()), 2)


if __name__ == "__main__":
    unittest.main()
