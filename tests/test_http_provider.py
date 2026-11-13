"""Step 4, T26–T30: actual adapter with fixture-backed HTTP transport.

All HTTP results and exception messages come from saved scripts. Credentials
come only from a stub returning a fixed fixture sentinel. No real API_KEY is
read or set. Retry commands are contracts for the step 7 CLI; executing them
against persisted attempts is covered in later steps.
"""
from copy import deepcopy
import hashlib
from http.client import IncompleteRead
import importlib
import io
import json
import logging
import os
from pathlib import Path
import shlex
import socket
import unittest
from unittest import mock

from intake.prompts import build_messages, PROMPT_VERSION
from intake.providers import ProviderError, ScriptedProvider


ROOT = Path(__file__).resolve().parents[1]
MOCKS = ROOT / "tests/mock_responses"
SETTINGS = json.loads((ROOT / "tests/inputs/provider-settings.json").read_text(encoding="utf-8"))
ORACLE = json.loads((ROOT / "tests/expected_facts.json").read_text(encoding="utf-8"))["documents"]
SCHEMA = json.loads((ROOT / "snapshot.schema.json").read_text(encoding="utf-8"))


class FixtureTransport:
    """Capture actual request objects and play a saved HTTP scenario once."""

    def __init__(self, api, scenario):
        self.api = api
        self.steps = json.loads((MOCKS / "http" / f"{scenario}.json").read_text(encoding="utf-8"))["steps"]
        self.calls = []

    def __call__(self, request, *, timeout):
        index = len(self.calls)
        self.calls.append({"request": deepcopy(request), "timeout": timeout})
        if index >= len(self.steps):
            raise AssertionError("adapter exceeded the saved HTTP script")
        step = self.steps[index]
        if step.get("exception") == "timeout":
            raise TimeoutError(step["message"])
        if step.get("exception") == "connection":
            raise OSError(step["message"])
        if step.get("exception") == "incomplete-read":
            raise IncompleteRead(step["partial"].encode("utf-8"))
        if "body_fixture" in step:
            body = (MOCKS / step["body_fixture"]).read_bytes()
        elif "body_json" in step:
            body = json.dumps(step["body_json"]).encode("utf-8")
        else:
            body = step["body_text"].encode("utf-8")
        return self.api.HttpResponse(status=step["status"], headers=step["headers"], body=body)


class HttpProviderContract(unittest.TestCase):
    def setUp(self):
        self.secret = json.loads((MOCKS / "http/mock-secret.json").read_text(encoding="utf-8"))["value"]
        original_getitem = type(os.environ).__getitem__

        def forbid_key_read(environment, name):
            if name == "API_KEY":
                raise AssertionError("test read the real credential environment")
            return original_getitem(environment, name)

        for patch in [
            mock.patch.object(type(os.environ), "__getitem__", forbid_key_read),
            mock.patch.object(socket, "socket", side_effect=AssertionError("network forbidden")),
            mock.patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")),
        ]:
            patch.start()
            self.addCleanup(patch.stop)
        self.logs = io.StringIO()
        logger = logging.getLogger("intake")
        handler = logging.StreamHandler(self.logs)
        previous_level = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        self.addCleanup(logger.setLevel, previous_level)
        self.addCleanup(logger.removeHandler, handler)

    def api(self, name="intake.http_provider"):
        try:
            return importlib.import_module(name)
        except ModuleNotFoundError as exc:
            self.fail(f"Step 4 behavior is absent: {exc}")

    def make_provider(self, scenario, **overrides):
        api = self.api()
        settings = api.ProviderSettings(**dict(deepcopy(SETTINGS), **overrides))
        transport = FixtureTransport(api, scenario)
        sleeps = []
        clock = [0.0]

        def sleep(seconds):
            self.assertGreaterEqual(seconds, 0)
            sleeps.append(seconds)
            clock[0] += seconds

        credentials = mock.Mock(return_value=self.secret)
        provider = api.HttpProvider(
            settings=settings, transport=transport, getenv=credentials, sleep=sleep,
        )
        return provider, transport, sleeps, clock, credentials

    def process(self, provider, number=1):
        return self.api("intake.processing").process_file(
            ROOT / f"materials/requests/IT-{number:03}.json", provider=provider,
        )

    def assert_success(self, record, number=1, response_source="fixture-provider/fixture-model-v1"):
        name = f"IT-{number:03}.json"
        self.assertEqual(record.document_id, name)
        self.assertEqual(record.source_sha256, hashlib.sha256((ROOT / "materials/requests" / name).read_bytes()).hexdigest())
        self.assertEqual(record.response_source, response_source)
        self.assertIsNone(record.error)
        expected = ORACLE[name]["scalars"]
        self.assertEqual({key: record.extraction[key] for key in expected}, expected)
        self.assertEqual(record.status, "review")
        self.assertEqual(record.owner, "System Owner" if number == 1 else "Security Review")
        self.assertEqual(record.next_action, "review-request" if number == 1 else "review-risk-then-owner")

    def assert_failure(self, record, category, response_source="fixture-provider/fixture-model-v1"):
        self.assertEqual(record.document_id, "IT-001.json")
        self.assertEqual(record.source_sha256, hashlib.sha256((ROOT / "materials/requests/IT-001.json").read_bytes()).hexdigest())
        self.assertEqual(record.response_source, response_source)
        self.assertEqual((record.status, record.owner, record.next_action), ("error", "IT Service Desk", "retry-extraction"))
        self.assertIsNone(record.extraction)
        self.assertEqual(record.error_category, category)
        self.assertIsInstance(record.error, str)
        self.assertIn("IT-001.json", record.error)
        self.assertIn(category, record.error)
        command = shlex.split(record.retry_command)
        self.assertEqual(command[:4], ["python3", "-m", "intake", "retry"])
        self.assertEqual(command[command.index("--document") + 1], "IT-001.json")
        self.assertIn(record.retry_command, record.error)
        self.assertNotIn(self.secret, repr(record))
        self.assertNotIn(self.secret, self.logs.getvalue())

    def test_T26_mock_failure_does_not_stop_the_next_message(self):
        processing = self.api("intake.processing")
        provider = ScriptedProvider(mock_dir=MOCKS, script=[
            {"error": "timeout.json"}, {"reply": "complete-002.json"},
        ])
        results = processing.process_files(
            [ROOT / "materials/requests/IT-001.json", ROOT / "materials/requests/IT-002.json"],
            provider=provider,
        )
        self.assertEqual(len(results), 2)
        self.assert_failure(results[0], "timeout", response_source="mock")
        self.assert_success(results[1], 2, response_source="mock")
        self.assertEqual(provider.call_count, 2)

    def test_T26_http_transient_failures_have_separate_bounded_retry_budgets(self):
        processing = self.api("intake.processing")
        scenarios = {
            "timeout-then-independent-success": "timeout",
            "rate-limit-then-independent-success": "rate-limit",
            "unavailable-then-independent-success": "unavailable",
            "connection-then-independent-success": "unavailable",
        }
        for scenario, category in scenarios.items():
            with self.subTest(scenario=scenario):
                provider, transport, sleeps, clock, credentials = self.make_provider(scenario)
                results = processing.process_files(
                    [ROOT / "materials/requests/IT-001.json", ROOT / "materials/requests/IT-002.json"],
                    provider=provider,
                )
                self.assertEqual(len(results), 2)
                self.assert_failure(results[0], category)
                self.assert_success(results[1], 2)
                self.assertEqual(len(transport.calls), 4)
                self.assertEqual(sleeps, [1.0, 2.0])
                self.assertEqual(clock[0], 3.0)
                self.assertTrue(credentials.called)
                self.assertTrue(all(call.args == ("API_KEY",) for call in credentials.call_args_list))

    def test_T26_retry_after_is_capped_and_recovery_keeps_valid_facts(self):
        provider, transport, sleeps, clock, _ = self.make_provider("transient-recovery")
        self.assert_success(self.process(provider))
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(sleeps, [4.0, 2.0])
        self.assertEqual(clock[0], 6.0)

    def test_T27_refusal_is_explicit_and_is_not_retried_or_routed(self):
        provider, transport, sleeps, _, _ = self.make_provider("refusal")
        review = self.api("intake.review")
        with mock.patch.object(review, "route_extraction") as route:
            self.assert_failure(self.process(provider), "refusal")
            route.assert_not_called()
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(sleeps, [])

    def test_T28_interrupted_stream_and_completion_markers_never_publish_partial_facts(self):
        for scenario in ["interrupted-complete-looking", "interrupted-content", "interrupted-stream"]:
            with self.subTest(scenario=scenario):
                provider, transport, sleeps, _, _ = self.make_provider(scenario)
                self.assert_failure(self.process(provider), "incomplete-response")
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(sleeps, [])

    def test_T29_malformed_schema_and_envelope_failures_are_actionable(self):
        for scenario, category in {
            "malformed-content": "malformed-json",
            "malformed-http-json": "malformed-json",
            "schema-extra-property": "schema",
            "schema-nonobject": "schema",
            "invalid-envelope": "invalid-envelope",
            "fabricated-quote": "invalid-quotes",
            "provider-error-envelope": "provider-error",
        }.items():
            with self.subTest(scenario=scenario):
                provider, transport, sleeps, _, _ = self.make_provider(scenario)
                self.assert_failure(self.process(provider), category)
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(sleeps, [])

    def test_T30_actual_request_and_redacted_evidence_share_the_request_builder(self):
        provider, transport, sleeps, _, credentials = self.make_provider("success")
        message = json.loads((ROOT / "materials/requests/IT-001.json").read_text(encoding="utf-8"))["message"]
        with mock.patch.object(provider, "build_request", wraps=provider.build_request) as builder:
            self.assert_success(self.process(provider))
            self.assertEqual(builder.call_count, 1)
            evidence = provider.request_evidence(message=message)
            self.assertEqual(builder.call_count, 2)
        credentials.assert_called_once_with("API_KEY")
        self.assertEqual(len(transport.calls), 1, "evidence generation must not send a request")
        self.assertEqual(sleeps, [])
        call = transport.calls[0]
        request = call["request"]
        self.assertEqual(request.url, "https://provider.example.invalid/v1/chat/completions")
        self.assertEqual(request.method, "POST")
        self.assertEqual(call["timeout"], 7.5)
        headers = {key.lower(): value for key, value in request.headers.items()}
        self.assertEqual(headers["authorization"], f"Bearer {self.secret}")
        self.assertEqual(headers["content-type"], "application/json")
        body = json.loads(request.body.decode("utf-8"))
        self.assertEqual(body["model"], "fixture-model-v1")
        self.assertEqual(body["messages"], build_messages(message=message))
        for key, value in SETTINGS["generation"].items():
            self.assertEqual(body[key], value)
        self.assertEqual(body["response_format"]["type"], "json_schema")
        response_schema = body["response_format"]["json_schema"]
        self.assertTrue(response_schema["strict"])
        self.assertEqual(response_schema["schema"], SCHEMA)
        expected_headers = dict(request.headers)
        for key in expected_headers:
            if key.lower() == "authorization":
                expected_headers[key] = "[REDACTED]"
        self.assertEqual(evidence["request"], {
            "url": request.url, "method": request.method,
            "headers": expected_headers, "body": body,
        })
        for key, value in SETTINGS.items():
            self.assertEqual(evidence["configuration"][key], value)
        self.assertEqual(evidence["configuration"]["prompt_version"], PROMPT_VERSION)
        for category in [
            "success", "missing-credential", "timeout", "rate-limit", "unavailable",
            "http-error", "refusal", "incomplete-response", "malformed-json", "schema",
            "invalid-envelope", "invalid-quotes", "provider-error",
        ]:
            self.assertTrue(evidence["response_handling"][category])
        self.assertNotIn(self.secret, json.dumps(evidence))
        self.assertNotIn(self.secret, self.logs.getvalue())

    def test_T30_source_override_cannot_change_request_controls(self):
        provider, transport, _, _, _ = self.make_provider("success-override")
        record = self.process(provider, 6)
        self.assertEqual((record.status, record.owner, record.next_action), ("review", "IT Service Desk", "inspect-message"))
        self.assertEqual(record.extraction["permission"], "read")
        body = json.loads(transport.calls[0]["request"].body.decode("utf-8"))
        self.assertEqual(body["model"], SETTINGS["model"])
        self.assertEqual([message["role"] for message in body["messages"]], ["system", "user"])
        self.assertNotIn("SYSTEM OVERRIDE", body["messages"][0]["content"])
        self.assertIn("SYSTEM OVERRIDE", body["messages"][1]["content"])
        for key, value in SETTINGS["generation"].items():
            self.assertEqual(body[key], value)

    def test_T30_upstream_error_details_are_redacted_and_client_errors_are_not_retried(self):
        for scenario in ["http-client-error", "http-auth-error"]:
            with self.subTest(scenario=scenario):
                provider, transport, sleeps, _, _ = self.make_provider(scenario)
                self.assert_failure(self.process(provider), "http-error")
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(sleeps, [])
                self.assertIn("IT-001.json", self.logs.getvalue())
                self.assertIn("http-error", self.logs.getvalue())
                direct, _, _, _, _ = self.make_provider(scenario)
                with self.assertRaises(ProviderError) as failure:
                    direct.complete(message="fixture message")
                self.assertNotIn(self.secret, str(failure.exception))
                self.assertNotIn(self.secret, self.logs.getvalue())

    def test_T30_missing_credential_is_actionable_before_transport(self):
        for missing in [None, "", "   "]:
            with self.subTest(value=missing):
                provider, transport, sleeps, _, credentials = self.make_provider("success")
                credentials.return_value = missing
                self.assert_failure(self.process(provider), "missing-credential")
                credentials.assert_called_once_with("API_KEY")
                self.assertEqual(transport.calls, [])
                self.assertEqual(sleeps, [])

    def test_T30_settings_reject_unbounded_attempts_and_request_control_overrides(self):
        api = self.api()
        for override in [
            {"max_attempts": 0}, {"max_attempts": -1}, {"timeout": 0},
            {"retry_base_delay": -1}, {"retry_max_delay": -1},
            {"generation": {"model": "source-selected-model"}},
            {"generation": {"messages": []}},
            {"generation": {"response_format": {"type": "text"}}},
        ]:
            with self.subTest(override=override):
                with self.assertRaises(ValueError):
                    api.ProviderSettings(**dict(deepcopy(SETTINGS), **override))
        # Exercise a saved completion as well as constructor rejection.
        provider, _, _, _, _ = self.make_provider("success")
        self.assert_success(self.process(provider))


if __name__ == "__main__":
    unittest.main()
