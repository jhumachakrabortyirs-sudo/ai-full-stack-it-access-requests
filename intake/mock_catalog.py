"""Source-indexed, offline provider for repeatable CLI examples.

The catalog selects fixtures by SHA-256 of decoded message text. It never
invents an extraction or falls back to a live provider. Native CLI snapshots
archive catalog/fixture text so queued work survives missing fixture files.
"""
import hashlib
import json
from pathlib import Path
from typing import Any

from . import prompts
from .providers import ProviderError, ProviderResponse


class CatalogProvider:
    provider_kind = "mock"
    response_source = "mock"

    def __init__(
        self, mock_dir: Path, *, snapshot: dict[str, Any] | None = None,
        prompt_text: str | None = None, schema_text: str | None = None,
        prompt_version: str = prompts.PROMPT_VERSION,
    ):
        self.mock_dir = Path(mock_dir).resolve()
        if snapshot is not None:
            self._catalog_text = snapshot["catalog_text"]
            self._fixtures = dict(snapshot["fixture_texts"])
            self._configuration = dict(snapshot["model_configuration"])
            self._prompt_text = snapshot["prompt_text"]
            self._schema_text = snapshot["schema_text"]
        else:
            self._catalog_text = (self.mock_dir / "catalog.json").read_bytes().decode("utf-8")
            catalog = json.loads(self._catalog_text)
            if catalog.get("version") != 1 or not isinstance(catalog.get("messages"), dict):
                raise ValueError("Mock catalog must have version 1 and a messages mapping")
            self._fixtures = {}
            for digest, entry in catalog["messages"].items():
                if not isinstance(digest, str) or len(digest) != 64 or not isinstance(entry, dict):
                    raise ValueError("Mock catalog contains an invalid message entry")
                if set(entry) not in ({"reply"}, {"error"}):
                    raise ValueError("Mock entries must select exactly one reply or error fixture")
                name = next(iter(entry.values()))
                if not isinstance(name, str):
                    raise ValueError("Mock fixture name must be text")
                path = (self.mock_dir / name).resolve()
                if not path.is_relative_to(self.mock_dir):
                    raise ValueError("Mock fixture must be inside the selected directory")
                self._fixtures[name] = path.read_bytes().decode("utf-8")
            self._configuration = {
                "provider": "mock", "model": "scripted-fixtures", "generation": {},
                "prompt_version": prompt_version, "mock_dir": str(self.mock_dir),
                "catalog_sha256": hashlib.sha256(self._catalog_text.encode("utf-8")).hexdigest(),
                "fixture_sha256": {name: hashlib.sha256(text.encode("utf-8")).hexdigest() for name, text in self._fixtures.items()},
            }
            self._prompt_text = prompts.PROMPT_PATH.read_bytes().decode("utf-8") if prompt_text is None else prompt_text
            self._schema_text = prompts.SCHEMA_PATH.read_bytes().decode("utf-8") if schema_text is None else schema_text
        self._catalog = json.loads(self._catalog_text)["messages"]

    def extraction_assets(self) -> dict[str, Any]:
        return {
            "model_configuration": json.loads(json.dumps(self._configuration)),
            "prompt_text": self._prompt_text, "schema_text": self._schema_text,
            "catalog_text": self._catalog_text, "fixture_texts": dict(self._fixtures),
        }

    def with_snapshot(self, snapshot: dict[str, Any]) -> "CatalogProvider":
        if "catalog_text" not in snapshot:
            # Older library-created mock attempts used an injected script,
            # rather than a catalog. The operator explicitly selects a runtime
            # catalog to supply those saved source messages on recovery.
            return self
        return CatalogProvider(Path(snapshot["model_configuration"]["mock_dir"]), snapshot=snapshot)

    def complete(self, *, message: str) -> ProviderResponse:
        digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
        entry = self._catalog.get(digest)
        if entry is None:
            raise ProviderError("mock-exhausted", "No explicit mock fixture matches this message")
        try:
            payload = json.loads(self._fixtures[next(iter(entry.values()))])
        except (ValueError, KeyError):
            raise ProviderError("malformed-json", "Selected mock fixture is not valid JSON") from None
        if "error" in entry:
            raise ProviderError(payload["category"], payload["message"])
        return ProviderResponse(payload, "mock")
