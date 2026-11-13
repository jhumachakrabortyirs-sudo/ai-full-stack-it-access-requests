"""Decode source exports and validate completed provider extractions.

This service returns validated facts and source identity. Review routing is
provided by intake.review. A schema-valid extraction is not proof
of factual accuracy; quote checks establish literal source membership only.
"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .providers import Provider, ProviderError, ProviderResponse


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "snapshot.schema.json"


class ExtractionError(Exception):
    """An explicit failure that must not be treated as extracted facts."""

    def __init__(self, category: str, message: str):
        self.category = category
        super().__init__(message)


@dataclass(frozen=True)
class SourceExport:
    document_id: str
    source_sha256: str
    original_bytes: bytes
    message: str


@dataclass(frozen=True)
class ExtractionRecord:
    document_id: str
    source_sha256: str
    extraction: dict[str, Any]
    response_source: str


def _reject_non_json_number(value: str) -> None:
    raise ValueError("Non-JSON numeric constant")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON property")
        result[key] = value
    return result


def _parse_json(text: str) -> Any:
    return json.loads(
        text, parse_constant=_reject_non_json_number, object_pairs_hook=_unique_object
    )


def read_export(path: Path) -> SourceExport:
    """Read once and hash original bytes before UTF-8/JSON decoding.

    Only the decoded message is passed to the provider. Export metadata cannot
    supply request facts, and a later file rewrite cannot change this snapshot.
    """
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ExtractionError("invalid-export", "Cannot read source export file") from exc
    return decode_export(raw, document_id=path.name)


def decode_export(raw: bytes, *, document_id: str) -> SourceExport:
    """Decode already-read bytes, allowing failures to retain their hash."""
    source_hash = hashlib.sha256(raw).hexdigest()
    try:
        export = _parse_json(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ExtractionError(
            "invalid-export", "Source export must contain valid UTF-8 JSON"
        ) from exc
    if not isinstance(export, dict) or not isinstance(export.get("message"), str):
        raise ExtractionError(
            "invalid-export", "Source export must be an object with a message string"
        )
    return SourceExport(document_id, source_hash, raw, export["message"])


def validate_response(
    response: ProviderResponse, *, message: str, schema_text: str | None = None,
) -> dict[str, Any]:
    """Require completion, valid extraction JSON/schema and literal quotes.

    Validation errors describe structure rather than echoing response content.
    Positive schema shapes can include nulls and unresolved fields; review
    policy will determine whether those facts need clarification.
    """
    envelope = response.envelope
    if not isinstance(envelope, dict):
        raise ExtractionError("invalid-envelope", "Provider envelope must be an object")
    if "error" in envelope:
        raise ExtractionError("provider-error", "Provider returned an error envelope")
    choices = envelope.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ExtractionError("invalid-envelope", "Provider envelope must contain one completion choice")
    choice = choices[0]
    if choice.get("finish_reason") != "stop":
        raise ExtractionError(
            "incomplete-response", "Provider response lacks a successful stop completion marker"
        )
    completion = choice.get("message")
    if not isinstance(completion, dict):
        raise ExtractionError("invalid-envelope", "Completion message must be an object")
    if completion.get("refusal") is not None:
        raise ExtractionError("refusal", "Provider refused extraction")
    content = completion.get("content")
    if not isinstance(content, str):
        raise ExtractionError("invalid-envelope", "Completion content must be a JSON string")
    try:
        extraction = _parse_json(content)
    except ValueError as exc:
        raise ExtractionError("malformed-json", "Completion content is not valid JSON") from exc

    # Load the supplied schema rather than duplicating its rules in Python.
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8") if schema_text is None else schema_text)
    validator = Draft202012Validator(schema)
    error = next(validator.iter_errors(extraction), None)
    if error is not None:
        location = ".".join(str(part) for part in error.absolute_path) or "root"
        reason = f"Extraction schema violation at {location}: {error.validator}"
        if error.validator == "required":
            missing = [key for key in error.validator_value if key not in error.instance]
            reason += "; missing required properties: " + ", ".join(missing)
        raise ExtractionError("schema", reason)
    for index, quote in enumerate(extraction["supporting_quotes"]):
        if quote not in message:
            raise ExtractionError(
                "invalid-quotes",
                f"supporting_quotes[{index}] is not a literal substring of the source message",
            )
    return extraction


def extract_file(path: Path, *, provider: Provider) -> ExtractionRecord:
    """Extract one source snapshot, returning facts only after validation."""
    return extract_source(read_export(path), provider=provider)


def extract_source(
    source: SourceExport, *, provider: Provider, schema_text: str | None = None,
) -> ExtractionRecord:
    """Extract an already-read snapshot so routing uses those same bytes."""
    try:
        response = provider.complete(message=source.message)
    except ProviderError as exc:
        raise ExtractionError(exc.category, "Provider failed to produce an extraction") from exc
    extraction = validate_response(response, message=source.message, schema_text=schema_text)
    return ExtractionRecord(
        document_id=source.document_id,
        source_sha256=source.source_sha256,
        extraction=extraction,
        response_source=response.response_source,
    )
