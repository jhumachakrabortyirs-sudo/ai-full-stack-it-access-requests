"""Fixture providers and spawn-safe helpers for T37–T40; no application code."""
from contextlib import contextmanager, ExitStack
import importlib
import json
import os
from pathlib import Path
import re
import socket
import threading
from unittest import mock

from intake.providers import ProviderResponse, ScriptedProvider
from intake.state import ClaimBusy, ProcessingConfig, StateStore, StaleClaimError


ROOT = Path(__file__).resolve().parents[1]
MOCKS = ROOT / "tests/mock_responses"
POLICY = ROOT / "materials/office-policy.md"


@contextmanager
def offline():
    original_getitem = type(os.environ).__getitem__

    def forbid_key(environment, key):
        if key == "API_KEY":
            raise AssertionError("operation test read a real credential")
        return original_getitem(environment, key)

    with ExitStack() as patches:
        patches.enter_context(mock.patch.object(type(os.environ), "__getitem__", forbid_key))
        patches.enter_context(mock.patch.object(socket, "socket", side_effect=AssertionError("network forbidden")))
        patches.enter_context(mock.patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")))
        yield


class SharedClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value.value


class FixtureProvider:
    """Choose saved replies by source identity, independent of thread order.

    Load fixtures substitute only the explicitly authored request_id token.
    Expected facts are read from separate source oracles in the test assertions.
    """
    provider_kind = "mock"
    response_source = "mock"

    def __init__(self, *, enter=None, release=None, report=None, initial_barrier=None):
        self.enter = enter or {}
        self.release = release or {}
        self.report = report
        self.initial_barrier = initial_barrier
        self.calls = []
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()
        self.responses = {}
        for number in (1, 2, 3):
            source = json.loads((ROOT / f"materials/requests/IT-{number:03}.json").read_text())["message"]
            self.responses[source] = f"complete-{number:03}.json"
        revised = json.loads((ROOT / "tests/inputs/revisions/IT-001.json").read_text())["message"]
        self.responses[revised] = "changed-001.json"

    def complete(self, *, message):
        matched = re.search(r"\b(?:IT-\d{3}|LOAD-\d{4})\b", message)
        if matched is None:
            raise AssertionError("unexpected source message")
        identifier = matched.group()
        with self.lock:
            self.calls.append(identifier)
            index = len(self.calls)
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if self.report is not None:
                self.report.put({"event": "provider-call", "document_id": identifier + ".json", "pid": os.getpid()})
            if identifier in self.enter:
                self.enter[identifier].set()
            if identifier in self.release and not self.release[identifier].wait(timeout=15):
                raise AssertionError("test did not release provider barrier")
            if self.initial_barrier is not None and index <= self.initial_barrier.parties:
                self.initial_barrier.wait(timeout=15)
            if identifier.startswith("LOAD-"):
                template = (MOCKS / "load-withdrawal.json").read_text(encoding="utf-8")
                if message != f"Please withdraw {identifier}. Request date 2026-10-22.":
                    raise AssertionError("load substitution requires the exact authored input")
                return ProviderResponse(json.loads(template.replace("{{request_id}}", identifier)), "mock")
            fixture = self.responses.get(message)
            if fixture is None:
                raise AssertionError("source has no explicitly selected fixture")
            return ScriptedProvider(mock_dir=MOCKS, script=[{"reply": fixture}]).complete(message=message)
        finally:
            with self.lock:
                self.active -= 1


def operator_process(database, paths, operator, start, enter, release, reports, mode="workers"):
    """Two real processes start the same intake at a shared barrier."""
    with offline():
        try:
            provider = FixtureProvider(enter=enter, release=release, report=reports)
            config = ProcessingConfig.capture(provider=provider, policy_path=POLICY)
            with StateStore(Path(database), clock=lambda: 1000.0) as store:
                start.wait(timeout=15)
                if mode == "workers":
                    workers = importlib.import_module("intake.workers")
                    workers.run_intake(
                        Path(database), [Path(path) for path in paths],
                        provider_factory=lambda: provider, config=config, operator=operator,
                        max_workers=2, lease_seconds=60, clock=lambda: 1000.0,
                    )
                else:
                    for path in paths:
                        try:
                            claim = store.claim(Path(path), config=config, operator=operator, lease_seconds=60)
                        except ClaimBusy:
                            continue
                        if claim["state"] == "pending":
                            store.run_claim(claim, provider=provider)
            reports.put({"event": "operator-done", "operator": operator, "pid": os.getpid()})
        except BaseException as exc:
            reports.put({"event": "child-error", "type": type(exc).__name__, "message": str(exc)})
            raise


def interrupt_process(database, paths, clock_value, ready, release, reports):
    """Commit IT-001, then hold the provider after the IT-002 claim commit."""
    with offline():
        provider = FixtureProvider(enter={"IT-002": ready}, release={"IT-002": release}, report=reports)
        config = ProcessingConfig.capture(provider=provider, policy_path=POLICY)
        with StateStore(Path(database), clock=SharedClock(clock_value)) as store:
            store.intake([Path(paths[0])], provider=provider, config=config, operator="interrupted", lease_seconds=30)
            claim = store.claim(Path(paths[1]), config=config, operator="interrupted", lease_seconds=30)
            store.run_claim(claim, provider=provider)


def delayed_claim_process(database, claim, clock_value, ready, release, reports):
    with offline():
        provider = FixtureProvider(enter={claim["document_id"][:-5]: ready}, release={claim["document_id"][:-5]: release})
        with StateStore(Path(database), clock=SharedClock(clock_value)) as store:
            try:
                record = store.run_claim(claim, provider=provider)
            except StaleClaimError:
                reports.put({"event": "stale-rejected", "attempt_id": claim["attempt_id"]})
            else:
                reports.put({"event": "late-completed", "record": record})


def commit_boundary_process(database, path, stage, clock_value, ready, release):
    """Hold immediately before COMMIT, or immediately after it, for termination.

    Instrument the existing transaction context only in this child; no new
    production fault-hook API is needed. The parent kills the process at the
    barrier, so rollback/durability is checked by another process afterward.
    """
    with offline():
        provider = FixtureProvider()
        config = ProcessingConfig.capture(provider=provider, policy_path=POLICY)
        with StateStore(Path(database), clock=SharedClock(clock_value)) as store:
            claim = store.claim(Path(path), config=config, operator="crash-boundary", lease_seconds=30)
            original = store._transaction

            @contextmanager
            def boundary():
                with original() as db:
                    yield db
                    if stage == "before":
                        ready.set()
                        if not release.wait(timeout=15):
                            raise AssertionError("commit barrier was not released")
                if stage == "after":
                    ready.set()
                    if not release.wait(timeout=15):
                        raise AssertionError("commit barrier was not released")

            store._transaction = boundary
            store.run_claim(claim, provider=provider)
