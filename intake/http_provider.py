"""Configurable, non-streaming Chat Completions HTTP adapter.

The transport and sleep function are injectable. Only complete() reads a
credential, via API_KEY; evidence uses the same builder with a placeholder.
Raw requests, upstream bodies and transport exception details are never logged.
"""
from copy import deepcopy
from dataclasses import dataclass, field
import json
import logging
import math
import os
from types import MappingProxyType
from typing import Any, Callable, Mapping
import time
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .extraction import _parse_json
from .prompts import PROMPT_VERSION, SCHEMA_PATH, build_messages
from . import prompts
from .providers import ProviderError, ProviderResponse


LOGGER = logging.getLogger(__name__)
RESPONSE_HANDLING = {
    "success": "Validate completion marker, extraction JSON, supplied schema and literal quotes before review routing.",
    "missing-credential": "Set API_KEY in the operator environment, then retry; no HTTP request was sent.",
    "timeout": "Request timed out after bounded retries; check provider availability and retry this document.",
    "rate-limit": "Provider rate limit exhausted the retry budget; wait for capacity and retry this document.",
    "unavailable": "Provider connection or server failed after bounded retries; restore service and retry this document.",
    "http-error": "Provider rejected the HTTP request; check endpoint, credentials, model access and configuration before retrying.",
    "refusal": "Provider refused extraction; inspect the source and provider policy before retrying.",
    "incomplete-response": "Provider response was interrupted or did not finish; inspect output limits and retry.",
    "malformed-json": "Provider returned malformed JSON; inspect the provider configuration and retry.",
    "schema": "Extraction failed the supplied schema; inspect the model configuration and retry.",
    "invalid-envelope": "Provider completion envelope was invalid; check endpoint compatibility and retry.",
    "invalid-quotes": "Extraction contained a quote absent from the message; inspect the source and retry.",
    "provider-error": "Provider reported an error without usable facts; check service configuration and retry.",
    "invalid-export": "Source must be a readable UTF-8 JSON object with a message string; repair the export and retry.",
    "mock-exhausted": "No mock response remained; extend the explicit mock script and retry.",
}


@dataclass(frozen=True)
class ProviderSettings:
    base_url: str
    provider: str
    model: str
    timeout: float = 30.0
    max_attempts: int = 3
    retry_base_delay: float = 1.0
    retry_max_delay: float = 30.0
    generation: Mapping[str, Any] = field(default_factory=lambda: {
        "temperature": 0, "top_p": 1, "max_tokens": 2048,
    })

    def __post_init__(self):
        for name in ("base_url", "provider", "model"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 for char in value):
                raise ValueError(f"{name} must be nonempty text without control characters")
        url = urlsplit(self.base_url)
        if (
            url.scheme not in {"https", "http"} or not url.hostname
            or url.username is not None or url.password is not None or url.query or url.fragment
        ):
            raise ValueError("base_url must be an HTTP(S) API base without credentials, query or fragment")
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 10:
            raise ValueError("max_attempts must be an integer from 1 to 10")
        for name in ("timeout", "retry_base_delay", "retry_max_delay"):
            value = getattr(self, name)
            if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if self.timeout == 0:
            raise ValueError("timeout must be positive")
        if not isinstance(self.generation, Mapping):
            raise ValueError("generation must be a mapping of generation parameters")
        generation = deepcopy(dict(self.generation))
        allowed = {"temperature", "top_p", "max_tokens", "max_completion_tokens", "seed", "frequency_penalty", "presence_penalty"}
        if set(generation) - allowed:
            raise ValueError("generation contains unsupported parameters or request control overrides")
        if "max_tokens" in generation and "max_completion_tokens" in generation:
            raise ValueError("Use only one output token limit parameter")
        for name, value in generation.items():
            if name in {"max_tokens", "max_completion_tokens", "seed"}:
                if type(value) is not int or (name != "seed" and value < 1):
                    raise ValueError(f"{name} must be an integer with a positive token limit")
            else:
                lower, upper = {"temperature": (0, 2), "top_p": (0, 1), "frequency_penalty": (-2, 2), "presence_penalty": (-2, 2)}[name]
                if type(value) not in {int, float} or not math.isfinite(value) or not lower <= value <= upper:
                    raise ValueError(f"{name} is outside its supported numeric range")
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))
        object.__setattr__(self, "generation", MappingProxyType(generation))

    def configuration(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url, "provider": self.provider, "model": self.model,
            "timeout": self.timeout, "max_attempts": self.max_attempts,
            "retry_base_delay": self.retry_base_delay, "retry_max_delay": self.retry_max_delay,
            "generation": dict(self.generation), "prompt_version": PROMPT_VERSION,
        }


@dataclass(frozen=True)
class HttpRequest:
    url: str
    method: str
    headers: dict[str, str] = field(repr=False)
    body: bytes = field(repr=False)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes = field(repr=False)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Preserve the configured destination for credential-bearing requests.
        return None


def http_transport(request: HttpRequest, *, timeout: float) -> HttpResponse:
    """Send a single HTTP request; the adapter owns all retries."""
    wire_request = Request(request.url, data=request.body, headers=request.headers, method=request.method)
    opener = build_opener(_NoRedirect())
    try:
        with opener.open(wire_request, timeout=timeout) as response:
            return HttpResponse(response.status, dict(response.headers.items()), response.read())
    except HTTPError as response:
        # Error bodies are not useful for extraction and can contain secrets.
        with response:
            return HttpResponse(response.code, dict(response.headers.items()), b"")


class HttpProvider:
    provider_kind = "http"

    def __init__(
        self, *, settings: ProviderSettings, transport: Callable = http_transport,
        getenv: Callable | None = None, sleep: Callable = time.sleep,
        prompt_text: str | None = None, schema_text: str | None = None,
        prompt_version: str = PROMPT_VERSION,
        on_retry: Callable[[str, int, float], None] | None = None,
    ):
        self.settings = settings
        self._transport = transport
        self._getenv = os.getenv if getenv is None else getenv
        self._sleep = sleep
        self._prompt_text = prompt_text
        self._schema_text = schema_text
        self._prompt_version = prompt_version
        self._on_retry = on_retry

    def extraction_assets(self) -> dict[str, Any]:
        configuration = self.settings.configuration()
        configuration["prompt_version"] = self._prompt_version
        return {
            "model_configuration": configuration,
            "prompt_text": prompts.PROMPT_PATH.read_bytes().decode("utf-8") if self._prompt_text is None else self._prompt_text,
            "schema_text": SCHEMA_PATH.read_bytes().decode("utf-8") if self._schema_text is None else self._schema_text,
        }

    def with_snapshot(self, snapshot: dict[str, Any]) -> "HttpProvider":
        """Use saved assets/settings while retaining the injected runtime I/O."""
        values = dict(snapshot["model_configuration"])
        version = values.pop("prompt_version")
        return HttpProvider(
            settings=ProviderSettings(**values), transport=self._transport,
            getenv=self._getenv, sleep=self._sleep,
            prompt_text=snapshot["prompt_text"], schema_text=snapshot["schema_text"],
            prompt_version=version,
            on_retry=self._on_retry,
        )

    def with_retry_callback(self, callback: Callable[[str, int, float], None]) -> "HttpProvider":
        """Return a runtime copy; report only safe retry categories and timing."""
        return HttpProvider(
            settings=self.settings, transport=self._transport, getenv=self._getenv,
            sleep=self._sleep, prompt_text=self._prompt_text, schema_text=self._schema_text,
            prompt_version=self._prompt_version, on_retry=callback,
        )

    @property
    def response_source(self) -> str:
        return f"{self.settings.provider}/{self.settings.model}"

    def build_request(self, *, message: str, api_key: str) -> HttpRequest:
        assets = self.extraction_assets()
        body = {
            "model": self.settings.model, "messages": build_messages(
                message=message, prompt_text=assets["prompt_text"], schema_text=assets["schema_text"],
            ),
            **self.settings.generation,
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "access_request", "strict": True,
                "schema": json.loads(assets["schema_text"]),
            }},
        }
        return HttpRequest(
            url=self.settings.base_url + "/chat/completions", method="POST",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            body=json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8"),
        )

    def request_evidence(self, *, message: str) -> dict[str, Any]:
        """Build a redacted example without reading a key or sending HTTP."""
        request = self.build_request(message=message, api_key="[REDACTED]")
        headers = dict(request.headers)
        headers["Authorization"] = "[REDACTED]"
        return {
            "observation": {"kind": "request-example-only", "provider_call_performed": False},
            "configuration": self.extraction_assets()["model_configuration"],
            "request": {"url": request.url, "method": request.method,
                        "headers": headers, "body": json.loads(request.body.decode("utf-8"))},
            "response_handling": dict(RESPONSE_HANDLING),
        }

    def _retry_delay(self, attempt: int, headers: Mapping[str, str]) -> float:
        delay = self.settings.retry_base_delay * 2 ** attempt
        value = next((value for key, value in headers.items() if key.lower() == "retry-after"), None)
        if value is not None:
            try:
                suggested = float(value)
                if math.isfinite(suggested) and suggested >= 0:
                    delay = max(delay, suggested)
            except (ValueError, TypeError):
                pass
        return min(delay, self.settings.retry_max_delay)

    def complete(self, *, message: str) -> ProviderResponse:
        api_key = self._getenv("API_KEY")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ProviderError("missing-credential", RESPONSE_HANDLING["missing-credential"])
        request = self.build_request(message=message, api_key=api_key.strip())
        for attempt in range(self.settings.max_attempts):
            headers: Mapping[str, str] = {}
            category = None
            try:
                response = self._transport(request, timeout=self.settings.timeout)
            except IncompleteRead:
                raise ProviderError("incomplete-response", RESPONSE_HANDLING["incomplete-response"]) from None
            except TimeoutError:
                category = "timeout"
            except URLError as exc:
                category = "timeout" if isinstance(exc.reason, TimeoutError) else "unavailable"
            except OSError:
                category = "unavailable"
            else:
                headers = response.headers
                if response.status == 429:
                    category = "rate-limit"
                elif response.status in {408, 504}:
                    category = "timeout"
                elif 500 <= response.status <= 599:
                    category = "unavailable"
                elif not 200 <= response.status <= 299:
                    raise ProviderError("http-error", RESPONSE_HANDLING["http-error"])
                else:
                    try:
                        envelope = _parse_json(response.body.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError):
                        raise ProviderError("malformed-json", RESPONSE_HANDLING["malformed-json"]) from None
                    return ProviderResponse(envelope, self.response_source)
            if attempt + 1 == self.settings.max_attempts:
                raise ProviderError(category, RESPONSE_HANDLING[category])
            delay = self._retry_delay(attempt, headers)
            if self._on_retry is not None:
                self._on_retry(category, attempt + 1, delay)
            LOGGER.info("Provider retry category=%s attempt=%d delay=%s", category, attempt + 1, delay)
            self._sleep(delay)
        raise AssertionError("Validated retry budget was not executed")
