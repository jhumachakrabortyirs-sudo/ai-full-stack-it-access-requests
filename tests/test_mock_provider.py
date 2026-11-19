"""T01: provider contract, before step 1 implementation.

The scalar oracle below is transcribed from IT-001 and the plan's fact ledger;
it is deliberately independent of the provider reply fixture.
"""
import importlib
import json
import os
import socket
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MOCKS = ROOT / "tests" / "mock_responses"
EXPECTED = {
    "request_id": "IT-001",
    "request_date": "2026-10-22",
    "kind": "new",
    "requester": "Maya Chen",
    "team": "Analytics",
    "system": "Atlas",
    "environment": "staging",
    "permission": "read",
    "business_purpose": "I'm checking the weekly conversion dashboard with the new event definition changes (before next reporting cycle).",
    "start_date": "2026-10-23",
    "end_date": "2026-11-06",
    "manager": "Priya Shah",
}


class MockProviderContract(unittest.TestCase):
    def test_T01_scripted_calls_and_independent_release_without_key_or_network(self):
        message = json.loads(
            (ROOT / "materials/requests/IT-001.json").read_text(encoding="utf-8")
        )["message"]
        envelope = json.loads((MOCKS / "complete-001.json").read_text(encoding="utf-8"))
        choice = envelope["choices"][0]
        self.assertEqual(choice["finish_reason"], "stop")
        payload = json.loads(choice["message"]["content"])
        self.assertEqual({key: payload[key] for key in EXPECTED}, EXPECTED)
        self.assertEqual(payload["issues"], [])
        self.assertTrue(payload["supporting_quotes"])
        for quote in payload["supporting_quotes"]:
            self.assertIn(quote, message)

        entered = threading.Event()
        release = threading.Event()
        original_getitem = type(os.environ).__getitem__

        def forbid_key_read(environment, name):
            if name == "API_KEY":
                raise AssertionError("mock provider read API_KEY")
            return original_getitem(environment, name)

        with mock.patch.object(type(os.environ), "__getitem__", forbid_key_read), \
                mock.patch.object(socket, "socket", side_effect=AssertionError("network forbidden")), \
                mock.patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")):
            try:
                module = importlib.import_module("intake.providers")
            except ModuleNotFoundError as exc:
                self.fail(f"Step 1 provider contract is absent: {exc}")

            # Script slots are claimed in invocation order. Event barriers make
            # scheduling reproducible without sleeps or real provider calls.
            provider = module.ScriptedProvider(
                mock_dir=MOCKS,
                script=[
                    {"reply": "complete-001.json"},
                    {"reply": "complete-001.json", "entered": entered, "release": release},
                ],
            )
            first = provider.complete(message=message)
            self.assertEqual(first.response_source, "mock")
            self.assertEqual(first.envelope, envelope)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(provider.complete, message=message)
                try:
                    self.assertTrue(entered.wait(timeout=5), "second call never reached barrier")
                    self.assertFalse(pending.done(), "second call ignored release barrier")
                    self.assertEqual(provider.call_count, 2)
                    self.assertEqual(provider.calls, [{"message": message}, {"message": message}])
                    # A held call cannot mutate the already returned result.
                    self.assertEqual(first.envelope, envelope)
                finally:
                    release.set()
                second = pending.result(timeout=5)
            self.assertEqual(second.response_source, "mock")
            self.assertEqual(second.envelope, envelope)
            self.assertIsNot(first, second)
            second.envelope["choices"][0]["finish_reason"] = "changed-by-caller"
            self.assertEqual(first.envelope, envelope)
            self.assertEqual(provider.call_count, 2)


if __name__ == "__main__":
    unittest.main()
