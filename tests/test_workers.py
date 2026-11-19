"""Step 6, T37–T40: bounded workers, real processes and durable recovery.

run_intake/resume use a provider_factory, bounded max_workers and an injectable
clock. on_result observes committed results; stop_event stops starting work but
drains in-flight calls and retains accepted unfinished sources. Automatic
heartbeats use heartbeat_interval. Tests use barriers instead of timing sleeps.
Existing claim/revision guards are tested directly and may already pass.
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib
import json
import multiprocessing
from pathlib import Path
import queue
import shutil
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from intake import http_provider
from intake.providers import ScriptedProvider
from intake.state import ClaimBusy, ProcessingConfig, StateStore, StaleClaimError
from operation_support import (
    FixtureProvider, POLICY, ROOT, MOCKS, SharedClock, offline,
    operator_process, interrupt_process, delayed_claim_process, commit_boundary_process,
)
from test_http_provider import FixtureTransport


EXPECTED = json.loads((ROOT / "tests/operating_expected.json").read_text())
FACTS = json.loads((ROOT / "tests/expected_facts.json").read_text())["documents"]
REVISIONS = json.loads((ROOT / "tests/expected_revisions.json").read_text())


class WorkerContract(unittest.TestCase):
    def setUp(self):
        self.guard = offline()
        self.guard.__enter__()
        self.addCleanup(self.guard.__exit__, None, None, None)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.database = self.directory / "state.sqlite3"
        self.paths = []
        for number in (1, 2, 3):
            path = self.directory / f"IT-{number:03}.json"
            shutil.copyfile(ROOT / "materials/requests" / path.name, path)
            self.paths.append(path)
        self.context = multiprocessing.get_context("spawn")
        self.value = self.context.Value("d", 1000.0)
        self.clock = SharedClock(self.value)
        with StateStore(self.database, clock=self.clock):
            pass

    def workers(self):
        try:
            return importlib.import_module("intake.workers")
        except ModuleNotFoundError as exc:
            self.fail(f"Step 6 worker orchestration is absent: {exc}")

    def require_method(self, instance, name):
        method = getattr(instance, name, None)
        self.assertTrue(callable(method), f"Step 6 method {type(instance).__name__}.{name} is absent")
        return method

    def provider_config(self, provider=None):
        return ProcessingConfig.capture(provider=provider or FixtureProvider(), policy_path=POLICY)

    def child(self, target, *args):
        process = self.context.Process(target=target, args=args)
        process.start()

        def cleanup():
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)

        self.addCleanup(cleanup)
        return process

    def reports(self):
        reports = self.context.Queue()
        self.addCleanup(reports.close)
        return reports

    def read_report(self, reports):
        try:
            return reports.get(timeout=10)
        except queue.Empty:
            self.fail("child did not report its controlled outcome")

    def assert_ordinary(self, row, name):
        expected = FACTS[name]["scalars"]
        self.assertEqual(row["document_id"], name)
        self.assertEqual({key: row["extraction"][key] for key in expected}, expected)
        self.assertEqual([row["status"], row["owner"], row["next_action"]], EXPECTED["ordinary_routes"][name])
        self.assertIsNone(row["error"])
        self.assertEqual(row["state"], "completed")
        self.assertEqual(row["response_source"], "mock")

    def test_T37_slow_message_keeps_other_results_visible_and_renews_its_lease(self):
        workers = self.workers()
        entered, release = threading.Event(), threading.Event()
        visible, renewed = threading.Event(), threading.Event()
        provider = FixtureProvider(enter={"IT-001": entered}, release={"IT-001": release})
        config = self.provider_config(provider)
        completed = set()
        lock = threading.Lock()

        def on_result(row):
            with lock:
                completed.add(row["document_id"])
                if {"IT-002.json", "IT-003.json"} <= completed:
                    visible.set()

        original = self.require_method(StateStore, "renew_claim")

        def trace_renew(store, claim, *, lease_seconds=300):
            result = original(store, claim, lease_seconds=lease_seconds)
            now = self.clock()
            # A clock advance after COMMIT must not make an older renewal
            # acknowledge the new time. Wait for a durably extended expiry.
            if now >= 1011.0 and result["lease_expires_at"] >= now + lease_seconds:
                renewed.set()
            return result

        with mock.patch.object(StateStore, "renew_claim", trace_renew):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    workers.run_intake, self.database, self.paths, provider_factory=lambda: provider,
                    config=config, operator="slow-operator", max_workers=3,
                    lease_seconds=30, heartbeat_interval=0.01, clock=self.clock,
                    on_result=on_result,
                )
                try:
                    self.assertTrue(entered.wait(timeout=10))
                    self.assertTrue(visible.wait(timeout=10), "delay blocked unrelated completions")
                    with StateStore(self.database, clock=self.clock) as store:
                        current = {row["document_id"]: row for row in store.current()}
                        self.assertEqual(set(current), {"IT-002.json", "IT-003.json"})
                        self.assert_ordinary(current["IT-002.json"], "IT-002.json")
                        self.assert_ordinary(current["IT-003.json"], "IT-003.json")
                        pending = store.pending()
                        self.assertEqual([row["document_id"] for row in pending], ["IT-001.json"])
                        self.assertEqual(pending[0]["lease_owner"], "slow-operator")
                        initial_expiry = pending[0]["lease_expires_at"]
                        self.value.value = 1011.0
                        self.assertTrue(renewed.wait(timeout=10), "blocked worker did not renew its lease")
                        renewed.clear()
                        self.value.value = initial_expiry + 1
                        self.assertTrue(renewed.wait(timeout=10))
                        pending = store.pending()
                        self.assertGreater(pending[0]["lease_expires_at"], self.clock())
                        with self.assertRaises(ClaimBusy):
                            store.claim(self.paths[0], config=config, operator="other-operator", lease_seconds=30)
                finally:
                    release.set()
                future.result(timeout=10)
        with StateStore(self.database, clock=self.clock) as store:
            self.assertEqual(len(store.current()), 3)
            self.assertEqual(store.pending(), [])
            self.assert_ordinary(store.history("IT-001.json")[0], "IT-001.json")
        self.assertLessEqual(provider.peak, 3)
        self.assertEqual(Counter(provider.calls), Counter({"IT-001": 1, "IT-002": 1, "IT-003": 1}))

    def test_T37_pending_exposes_rate_limit_and_unavailability_during_backoff(self):
        workers = self.workers()
        settings = http_provider.ProviderSettings(**json.loads((ROOT / "tests/inputs/provider-settings.json").read_text()))
        transport = FixtureTransport(http_provider, "transient-recovery")
        entered = [threading.Event(), threading.Event()]
        release = [threading.Event(), threading.Event()]
        waits = []

        def sleep(delay):
            index = len(waits)
            waits.append(delay)
            entered[index].set()
            if not release[index].wait(timeout=15):
                raise AssertionError("backoff was not released")

        secret = json.loads((MOCKS / "http/mock-secret.json").read_text())["value"]
        provider = http_provider.HttpProvider(settings=settings, transport=transport, getenv=mock.Mock(return_value=secret), sleep=sleep)
        config = self.provider_config(provider)
        with StateStore(self.database, clock=self.clock) as store:
            prior = store.intake([self.paths[1]], provider=FixtureProvider(), config=self.provider_config(), operator="prior")[0]
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                workers.run_intake, self.database, [self.paths[0]], provider_factory=lambda: provider,
                config=config, operator="rate-limited", max_workers=1, clock=self.clock,
            )
            try:
                for index, state in enumerate(["rate-limit", "unavailable"]):
                    self.assertTrue(entered[index].wait(timeout=10))
                    with StateStore(self.database, clock=self.clock) as store:
                        pending = store.pending()
                        self.assertEqual(len(pending), 1)
                        self.assertEqual(pending[0]["document_id"], "IT-001.json")
                        self.assertEqual(pending[0]["service_state"], state)
                        self.assertEqual(pending[0]["retry_attempt"], index + 1)
                        self.assertEqual(store.current(), [prior])
                    release[index].set()
            finally:
                for event in release:
                    event.set()
            future.result(timeout=10)
        self.assertEqual(waits, [4.0, 2.0])
        self.assertEqual(len(transport.calls), 3)
        with StateStore(self.database, clock=self.clock) as store:
            self.assertEqual(store.pending(), [])
            self.assertEqual(store.history("IT-002.json"), [prior])
            self.assertEqual(len(store.current()), 2)

    def test_T37_resume_can_retry_latest_errors_without_reprocessing_successes(self):
        workers = self.workers()
        provider = ScriptedProvider(mock_dir=MOCKS, script=[{"error": "timeout.json"}, {"reply": "complete-002.json"}])
        config = self.provider_config(provider)
        with StateStore(self.database, clock=self.clock) as store:
            initial = store.intake(self.paths[:2], provider=provider, config=config, operator="initial")
            self.assertEqual(initial[0]["status"], "error")
        for path in self.paths[:2]:
            path.unlink()
        recovery = FixtureProvider()
        workers.resume(
            self.database, provider_factory=lambda: recovery, operator="recovery",
            max_workers=2, retry_errors=True, clock=self.clock,
        )
        with StateStore(self.database, clock=self.clock) as store:
            self.assertEqual(store.history("IT-002.json"), [initial[1]])
            self.assertEqual(len(store.history("IT-001.json")), 2)
            self.assert_ordinary(store.history("IT-001.json")[-1], "IT-001.json")
            self.assertEqual(len(store.current()), 2)
        self.assertEqual(recovery.calls, ["IT-001"])

    def operators(self, mode):
        start = self.context.Barrier(2)
        entered = {name: self.context.Event() for name in ("IT-001", "IT-002")}
        release = self.context.Event()
        reports = self.reports()
        processes = [self.child(
            operator_process, str(self.database), [str(path) for path in self.paths[:2]],
            operator, start, entered, {name: release for name in entered}, reports, mode,
        ) for operator in ("operator-a", "operator-b")]
        try:
            for event in entered.values():
                self.assertTrue(event.wait(timeout=10), "both source claims did not reach the barrier")
            with StateStore(self.database, clock=self.clock) as store:
                self.assertEqual(len(store.pending()), 2)
                self.assertEqual(store.current(), [])
        finally:
            release.set()
        for process in processes:
            process.join(timeout=10)
            self.assertEqual(process.exitcode, 0)
        messages = [self.read_report(reports) for _ in range(4)]
        self.assertEqual(sum(message["event"] == "operator-done" for message in messages), 2)
        calls = [message["document_id"] for message in messages if message["event"] == "provider-call"]
        self.assertEqual(Counter(calls), Counter({"IT-001.json": 1, "IT-002.json": 1}))
        with StateStore(self.database, clock=self.clock) as store:
            self.assertEqual(store.pending(), [])
            self.assertEqual(len(store.current()), 2)
            for row in store.current():
                self.assert_ordinary(row, row["document_id"])
                self.assertEqual(store.history(row["document_id"]), [row])
                self.assertIn(row["operator"], {"operator-a", "operator-b"})
                self.assertEqual(store.source(row["attempt_id"]).source_sha256, row["source_sha256"])
        with sqlite3.connect(self.database) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE attempts SET result_json='{}' WHERE state='completed'")

    def test_T38_existing_atomic_claim_guard_coordinates_two_actual_processes(self):
        self.operators("claims")

    def test_T38_bounded_worker_pools_coordinate_two_actual_operator_processes(self):
        self.workers()
        self.operators("workers")

    def test_T39_killed_worker_resumes_saved_pending_source_and_preserves_completion(self):
        workers = self.workers()
        ready, release = self.context.Event(), self.context.Event()
        reports = self.reports()
        process = self.child(
            interrupt_process, str(self.database), [str(path) for path in self.paths[:2]],
            self.value, ready, release, reports,
        )
        self.assertTrue(ready.wait(timeout=10))
        with StateStore(self.database, clock=self.clock) as store:
            before = store.current()
            self.assertEqual(len(before), 1)
            self.assert_ordinary(before[0], "IT-001.json")
            pending = store.pending()
            self.assertEqual([row["document_id"] for row in pending], ["IT-002.json"])
            abandoned_id = pending[0]["attempt_id"]
        process.terminate()
        process.join(timeout=10)
        self.assertIsNotNone(process.exitcode)
        self.assertNotEqual(process.exitcode, 0)
        self.value.value = 1100.0
        for path in self.paths[:2]:
            path.unlink()
        recovery = FixtureProvider()
        workers.resume(self.database, provider_factory=lambda: recovery, operator="recovered", max_workers=2, lease_seconds=30, clock=self.clock)
        with StateStore(self.database, clock=self.clock) as store:
            self.assertEqual(store.history("IT-001.json"), before)
            self.assertEqual(store.pending(), [])
            self.assert_ordinary(store.history("IT-002.json")[-1], "IT-002.json")
            events = self.require_method(store, "events")("IT-002.json")
            self.assertTrue(any(event["attempt_id"] == abandoned_id and event["event"] == "abandoned" for event in events))
            self.assertTrue(any(event["event"] == "reclaimed" for event in events))
        self.assertEqual(recovery.calls, ["IT-002"])

    def test_T39_reclaimed_lease_rejects_old_worker_after_its_response_arrives(self):
        workers = self.workers()
        with StateStore(self.database, clock=self.clock) as store:
            old = store.claim(self.paths[1], config=self.provider_config(), operator="old", lease_seconds=30)
        ready, release = self.context.Event(), self.context.Event()
        reports = self.reports()
        process = self.child(delayed_claim_process, str(self.database), old, self.value, ready, release, reports)
        try:
            self.assertTrue(ready.wait(timeout=10))
            self.value.value = 1100.0
            recovery = FixtureProvider()
            workers.resume(self.database, provider_factory=lambda: recovery, operator="new", max_workers=1, lease_seconds=30, clock=self.clock)
        finally:
            release.set()
        process.join(timeout=10)
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(self.read_report(reports), {"event": "stale-rejected", "attempt_id": old["attempt_id"]})
        with StateStore(self.database, clock=self.clock) as store:
            history = store.history("IT-002.json")
            self.assertEqual(len(history), 1)
            self.assert_ordinary(history[0], "IT-002.json")
            self.assertGreater(history[0]["fencing_token"], old["fencing_token"])
            self.assertEqual(store.current(), history)

    def test_T39_commit_crashes_leave_whole_completion_or_recoverable_pending_work(self):
        workers = self.workers()
        for stage in ("before", "after"):
            with self.subTest(stage=stage):
                database = self.directory / f"commit-{stage}.sqlite3"
                with StateStore(database, clock=self.clock):
                    pass
                ready, release = self.context.Event(), self.context.Event()
                process = self.child(commit_boundary_process, str(database), str(self.paths[0]), stage, self.value, ready, release)
                self.assertTrue(ready.wait(timeout=10))
                process.terminate()
                process.join(timeout=10)
                with StateStore(database, clock=self.clock) as store:
                    current = store.current()
                    pending = store.pending()
                    self.assertEqual(len(current), int(stage == "after"))
                    self.assertEqual(len(pending), int(stage == "before"))
                    self.assertEqual(len(store.history("IT-001.json")), len(current))
                    if current:
                        self.assert_ordinary(current[0], "IT-001.json")
                self.value.value += 100
                recovery = FixtureProvider()
                workers.resume(database, provider_factory=lambda: recovery, operator="after-crash", max_workers=1, clock=self.clock)
                with StateStore(database, clock=self.clock) as store:
                    self.assertEqual(len(store.current()), 1)
                    self.assertEqual(store.pending(), [])
                    self.assert_ordinary(store.current()[0], "IT-001.json")
                self.assertEqual(recovery.calls, ["IT-001"] if stage == "before" else [])

    def test_T39_lease_renewal_is_fenced_and_cannot_revive_an_expired_claim(self):
        provider = FixtureProvider()
        with StateStore(self.database, clock=self.clock) as store:
            renew = self.require_method(store, "renew_claim")
            claim = store.claim(self.paths[0], config=self.provider_config(provider), operator="owner", lease_seconds=10)
            self.value.value = 1005.0
            renewed = renew(claim, lease_seconds=10)
            self.assertEqual(renewed["lease_expires_at"], 1015.0)
            self.assertEqual(renewed["fencing_token"], claim["fencing_token"])
            self.assertEqual(store.pending(), [renewed])
            forged = dict(renewed, fencing_token=renewed["fencing_token"] + 1)
            with self.assertRaises(StaleClaimError):
                renew(forged, lease_seconds=10)
            self.value.value = 1016.0
            with self.assertRaises(StaleClaimError):
                renew(renewed, lease_seconds=10)
            with self.assertRaises(StaleClaimError):
                store.run_claim(renewed, provider=provider)
            self.assertEqual(provider.calls, [])

    def test_T40_existing_revision_guard_keeps_new_source_current_after_late_old_completion(self):
        provider = FixtureProvider()
        config = self.provider_config(provider)
        with StateStore(self.database, clock=self.clock) as store:
            old = store.claim(self.paths[0], config=config, operator="old-source", lease_seconds=60)
        ready, release = self.context.Event(), self.context.Event()
        reports = self.reports()
        process = self.child(delayed_claim_process, str(self.database), old, self.value, ready, release, reports)
        try:
            self.assertTrue(ready.wait(timeout=10))
            shutil.copyfile(ROOT / "tests/inputs/revisions/IT-001.json", self.paths[0])
            with StateStore(self.database, clock=self.clock) as store:
                new = store.intake([self.paths[0]], provider=provider, config=config, operator="new-source")[0]
                self.assertEqual(new["extraction"]["end_date"], "2026-11-07")
                self.assertEqual(store.current(), [new])
        finally:
            release.set()
        process.join(timeout=10)
        self.assertEqual(process.exitcode, 0)
        report = self.read_report(reports)
        self.assertEqual(report["event"], "late-completed")
        self.assertEqual(report["record"]["extraction"]["end_date"], "2026-11-06")
        with StateStore(self.database, clock=self.clock) as store:
            self.assertEqual(store.current(), [new])
            self.assertEqual([row["attempt_id"] for row in store.history("IT-001.json")], [old["attempt_id"], new["attempt_id"]])
            self.assertNotEqual(old["source_sha256"], new["source_sha256"])
            self.assertEqual(store.pending(), [])

    def test_T40_200_400_2000_mock_documents_have_bounded_progress_and_resume(self):
        workers = self.workers()
        template = (ROOT / "tests/inputs/workload-template.json").read_text()
        for count in EXPECTED["workload_sizes"]:
            with self.subTest(count=count):
                database = self.directory / f"load-{count}.sqlite3"
                folder = self.directory / f"load-{count}"
                folder.mkdir()
                paths, hashes = [], {}
                for index in range(1, count + 1):
                    identifier = f"LOAD-{index:04}"
                    path = folder / f"{identifier}.json"
                    raw = template.replace("{{request_id}}", identifier).encode("utf-8")
                    path.write_bytes(raw)
                    paths.append(path)
                    hashes[path.name] = hashlib.sha256(raw).hexdigest()
                provider = FixtureProvider(initial_barrier=threading.Barrier(4))
                config = self.provider_config(provider)
                stop = threading.Event()
                progress = []
                lock = threading.Lock()

                def on_result(row):
                    self.assertEqual(row["state"], "completed")
                    with lock:
                        progress.append(row["document_id"])
                        if len(progress) >= 23:
                            stop.set()

                workers.run_intake(
                    database, paths, provider_factory=lambda: provider, config=config,
                    operator="load-initial", max_workers=4, lease_seconds=30,
                    clock=self.clock, stop_event=stop, on_result=on_result,
                )
                self.assertEqual(provider.peak, 4)
                self.assertGreaterEqual(len(progress), 23)
                with StateStore(database, clock=self.clock) as store:
                    prior = {row["document_id"]: row for row in store.current()}
                    pending = store.pending()
                    self.assertGreater(len(prior), 0)
                    self.assertLess(len(prior), count)
                    self.assertEqual(len(prior) + len(pending), count)
                    self.assertEqual(set(prior) | {row["document_id"] for row in pending}, set(hashes))
                # Resume accepted work entirely from durable snapshots.
                for path in paths:
                    path.unlink()
                self.value.value += 100
                recovery = FixtureProvider()
                workers.resume(
                    database, provider_factory=lambda: recovery, operator="load-recovery",
                    max_workers=4, lease_seconds=30, clock=self.clock,
                )
                self.assertLessEqual(recovery.peak, 4)
                self.assertEqual(Counter(provider.calls + recovery.calls), Counter({f"LOAD-{index:04}": 1 for index in range(1, count + 1)}))
                with StateStore(database, clock=self.clock) as store:
                    current = store.current()
                    self.assertEqual(len(current), count)
                    self.assertEqual(len({row["document_id"] for row in current}), count)
                    self.assertEqual(store.pending(), [])
                    for row in current:
                        identifier = row["document_id"][:-5]
                        expected = dict(EXPECTED["workload"]["scalars"], request_id=identifier)
                        self.assertEqual({key: row["extraction"][key] for key in expected}, expected)
                        self.assertEqual([row["status"], row["owner"], row["next_action"]], EXPECTED["workload"]["route"])
                        self.assertEqual(row["source_sha256"], hashes[row["document_id"]])
                        self.assertEqual(row["response_source"], "mock")
                        self.assertIsNone(row["error"])
                        for field, value in expected.items():
                            if value is None:
                                self.assertTrue(any(field in issue for issue in row["extraction"]["issues"]))
                        self.assertEqual(len(store.history(row["document_id"])), 1)
                        if row["document_id"] in prior:
                            self.assertEqual(row, prior[row["document_id"]])


if __name__ == "__main__":
    unittest.main()
