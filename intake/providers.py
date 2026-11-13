"""Provider contract and deterministic, offline scripted responses.

Script entries select a JSON reply with ``reply`` or an error fixture with
``error`` (containing string ``category`` and ``message`` properties). Optional
``entered`` and ``release`` threading events provide deterministic barriers.
Calls claim script entries under a lock, then wait outside the lock so one
delayed response does not prevent another call from progressing.
"""
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Mapping, Protocol, Sequence
import json


@dataclass(frozen=True)
class ProviderResponse:
    envelope: Any
    response_source: str


class Provider(Protocol):
    def complete(self, *, message: str) -> ProviderResponse:
        """Return a completion envelope; extraction validation happens later."""
        ...


class ProviderError(Exception):
    """A classified provider failure for the intake service to handle."""

    def __init__(self, category: str, message: str):
        self.category = category
        super().__init__(message)


class ScriptedProvider:
    """Consume fixture-backed script entries once, in invocation order.

    Exhaustion is explicit: the mock never falls back to a credential or a
    network service. Each reply is read afresh, giving callers independent
    envelope objects even when several entries name the same fixture.
    """

    def __init__(self, *, mock_dir: Path, script: Sequence[Mapping[str, Any]]):
        self._mock_dir = Path(mock_dir).resolve()
        self._script = [dict(entry) for entry in script]
        self._calls: list[dict[str, str]] = []
        self._lock = Lock()
        for entry in self._script:
            if ("reply" in entry) == ("error" in entry):
                raise ValueError("Each script entry must select exactly one reply or error fixture")

    @property
    def calls(self) -> list[dict[str, str]]:
        with self._lock:
            return [dict(call) for call in self._calls]

    @property
    def call_count(self) -> int:
        with self._lock:
            return len(self._calls)

    def complete(self, *, message: str) -> ProviderResponse:
        if not isinstance(message, str):
            raise TypeError("Provider message must be decoded text")
        with self._lock:
            index = len(self._calls)
            self._calls.append({"message": message})
            if index >= len(self._script):
                raise ProviderError("mock-exhausted", "No scripted response remains for this call")
            entry = self._script[index]

        if "entered" in entry:
            entry["entered"].set()
        if "release" in entry:
            entry["release"].wait()

        name = entry.get("reply", entry.get("error"))
        path = (self._mock_dir / name).resolve()
        if not path.is_relative_to(self._mock_dir):
            raise ValueError("Mock fixture must be inside mock_dir")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if "error" in entry:
            raise ProviderError(payload["category"], payload["message"])
        return ProviderResponse(envelope=payload, response_source="mock")
