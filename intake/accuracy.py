"""Compare measured records with a source-authored oracle.

Scalar exact matches, schema validity, evidence coverage, issue coverage and
routing are independent checks. Missing documents remain in every denominator.
This module never calls a provider or derives expectations from measured data.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable

from jsonschema import Draft202012Validator

from .extraction import ExtractionError, decode_export


ROOT = Path(__file__).resolve().parents[1]
_PROVENANCE = (
    "source_sha256", "response_source", "model_configuration", "processing_fingerprint",
    "extraction_fingerprint", "policy_sha256", "prompt_sha256", "schema_sha256",
    "attempt_id", "extraction_attempt_id", "routing_attempt_id",
)


def oracle_path(document: Path) -> Path:
    """Locate the independent oracle without reading any measured output."""
    document = Path(document)
    if document.suffix.lower() == ".json":
        return document.resolve()
    links = re.findall(r"<!--\s*accuracy-oracle:\s*([^>]+?)\s*-->", document.read_text(encoding="utf-8"))
    if len(links) != 1:
        raise ValueError("Expected document needs exactly one accuracy-oracle link")
    path = Path(links[0].strip())
    if path.is_absolute():
        raise ValueError("The accuracy-oracle link must be relative to its document")
    return (document.parent / path).resolve()


def load_oracle(document: Path) -> dict[str, Any]:
    """Read JSON directly, or follow the explicit relative link in Markdown."""
    oracle = json.loads(oracle_path(document).read_text(encoding="utf-8"))
    if not isinstance(oracle, dict):
        raise ValueError("Accuracy oracle root must be an object")
    return oracle


def validate_oracle(oracle: dict[str, Any], *, root: Path = ROOT) -> dict[str, str]:
    """Bind expectations to exact original bytes and literal source evidence."""
    root = Path(root).resolve()
    schema = json.loads((root / "snapshot.schema.json").read_text(encoding="utf-8"))
    fields = oracle.get("scalar_fields")
    expected_fields = set(schema["required"]) - {"supporting_quotes", "issues"}
    if oracle.get("version") != 1 or not isinstance(fields, list) or len(fields) != len(expected_fields) or set(fields) != expected_fields:
        raise ValueError("Oracle must declare version 1 and each scalar schema field exactly once")
    documents = oracle.get("documents")
    if not isinstance(documents, dict) or not documents:
        raise ValueError("Oracle must contain source-derived document expectations")
    validator = Draft202012Validator(schema)
    messages = {}
    for name, expected in documents.items():
        if not isinstance(name, str) or not isinstance(expected, dict):
            raise ValueError("Oracle document entries must be named objects")
        path = (root / expected["source_file"]).resolve()
        if not path.is_relative_to(root) or path.name != name:
            raise ValueError(f"Oracle source identity does not match {name}")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected["source_sha256"]:
            raise ValueError(f"Oracle source hash mismatch for {name}")
        try:
            source = decode_export(raw, document_id=name)
        except ExtractionError:
            raise ValueError(f"Oracle source is not a valid export for {name}") from None
        scalars = expected["scalars"]
        if not isinstance(scalars, dict) or set(scalars) != expected_fields:
            raise ValueError(f"Oracle scalar fields are incomplete for {name}")
        if not validator.is_valid({**scalars, "supporting_quotes": [], "issues": []}):
            raise ValueError(f"Oracle scalar expectations violate the schema for {name}")
        for key in ("source_quotes", "required_quotes"):
            quotes = expected[key]
            if not isinstance(quotes, list) or not quotes or any(not isinstance(quote, str) or not quote or quote not in source.message for quote in quotes):
                raise ValueError(f"Oracle source quote is absent or invalid for {name}")
        route = expected["route"]
        if not isinstance(route, list) or len(route) != 3 or any(not isinstance(value, str) for value in route):
            raise ValueError(f"Oracle route must specify status, owner and next_action for {name}")
        requirements = expected["issue_requirements"]
        if not isinstance(requirements, list):
            raise ValueError(f"Oracle issue requirements must be an array for {name}")
        for requirement in requirements:
            if not isinstance(requirement, dict) or not isinstance(requirement.get("field"), str) or not isinstance(requirement.get("patterns"), list) or not requirement["patterns"]:
                raise ValueError(f"Invalid oracle issue requirement for {name}")
            for pattern in requirement["patterns"]:
                re.compile(pattern, flags=re.IGNORECASE)
        messages[name] = source.message
    return messages


def compare(
    records: Iterable[dict[str, Any]], oracle: dict[str, Any], *, root: Path = ROOT,
) -> dict[str, Any]:
    messages = validate_oracle(oracle, root=root)
    documents = oracle["documents"]
    fields = oracle["scalar_fields"]
    measured = {}
    for row in records:
        if not isinstance(row, dict) or not isinstance(row.get("document_id"), str):
            raise ValueError("Measured records must be objects with document_id")
        name = row["document_id"]
        if name in measured:
            raise ValueError(f"Duplicate measured document: {name}")
        if name not in documents:
            raise ValueError(f"Measured document is outside the oracle: {name}")
        if row.get("source_sha256") != documents[name]["source_sha256"]:
            raise ValueError(f"Measured source hash mismatch for {name}")
        measured[name] = row
    count = len(documents)
    report = {
        "scalar": {"correct": 0, "denominator": count * len(fields)},
        "by_field": {field: {"correct": 0, "denominator": count} for field in fields},
        "by_document": {name: {"correct": 0, "denominator": len(fields)} for name in documents},
        "factual_errors": [],
        **{check: {"passed": 0, "denominator": count, "failures": []} for check in ("schema", "quotes", "issues")},
        "routing": {"correct": 0, "denominator": count, "failures": []},
        "provenance": [],
        "oracle_provenance": oracle.get("provenance"),
        "oracle_fingerprint": hashlib.sha256(json.dumps(oracle, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest(),
    }
    schema = json.loads((Path(root) / "snapshot.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)

    def error(name, field, expected, actual, reason, *, present=True):
        report["factual_errors"].append({
            "document_id": name, "field": field, "expected": deepcopy(expected),
            "actual": deepcopy(actual), "actual_present": present, "reason": reason,
        })

    for name, expected in documents.items():
        record = measured.get(name)
        extraction = None if record is None else record.get("extraction")
        usable = record is not None and isinstance(extraction, dict) and record.get("status") != "error" and record.get("error") is None
        for field in fields:
            present = usable and field in extraction
            value = extraction[field] if present else None
            wanted = expected["scalars"][field]
            if present and type(value) is type(wanted) and value == wanted:
                report["scalar"]["correct"] += 1
                report["by_field"][field]["correct"] += 1
                report["by_document"][name]["correct"] += 1
            else:
                reason = "value-mismatch" if present else "missing-field" if usable else "unavailable-extraction"
                error(name, field, wanted, value, reason, present=bool(present))

        violations = list(validator.iter_errors(extraction)) if usable else []
        if usable and not violations:
            report["schema"]["passed"] += 1
        else:
            reasons = [{"path": list(item.absolute_path), "validator": item.validator} for item in violations]
            report["schema"]["failures"].append({"document_id": name, "reason": "schema" if usable else "unavailable-extraction", "violations": reasons})

        quote_failures = []
        quotes = extraction.get("supporting_quotes") if usable else None
        if not isinstance(quotes, list) or any(not isinstance(quote, str) for quote in quotes):
            quote_failures.append({"document_id": name, "reason": "invalid-shape" if usable else "unavailable-extraction"})
            error(name, "supporting_quotes", expected["required_quotes"], quotes, "unavailable-evidence", present=usable and "supporting_quotes" in extraction)
        else:
            for quote in quotes:
                if quote not in messages[name]:
                    quote_failures.append({"document_id": name, "reason": "not-substring", "quote": quote})
                    error(name, "supporting_quotes", "Literal source substring", quote, "not-substring")
            for required in expected["required_quotes"]:
                if not any(required in observed for observed in quotes):
                    quote_failures.append({"document_id": name, "reason": "missing-evidence", "expected_quote": required})
                    error(name, "supporting_quotes", required, quotes, "missing-evidence")
        if quote_failures:
            report["quotes"]["failures"].extend(quote_failures)
        else:
            report["quotes"]["passed"] += 1

        issue_failures = []
        issues = extraction.get("issues") if usable else None
        if not isinstance(issues, list) or any(not isinstance(issue, str) for issue in issues):
            issue_failures.append({"document_id": name, "reason": "invalid-shape" if usable else "unavailable-extraction"})
        else:
            text = "\n".join(issues)
            for requirement in expected["issue_requirements"]:
                absent = [pattern for pattern in requirement["patterns"] if not re.search(pattern, text, flags=re.IGNORECASE)]
                if absent:
                    issue_failures.append({"document_id": name, "reason": "missing-issue", "field": requirement["field"], "patterns": absent})
                    error(name, "issues", requirement, issues, "missing-issue")
        if issue_failures:
            report["issues"]["failures"].extend(issue_failures)
        else:
            report["issues"]["passed"] += 1

        route_fields = ("status", "owner", "next_action")
        actual_route = [None if record is None else record.get(field) for field in route_fields]
        if actual_route == expected["route"]:
            report["routing"]["correct"] += 1
        else:
            report["routing"]["failures"].append({"document_id": name, "expected": expected["route"], "actual": actual_route})
            for field, wanted, value in zip(route_fields, expected["route"], actual_route):
                if wanted != value:
                    error(name, field, wanted, value, "route-mismatch", present=record is not None and field in record)
        report["provenance"].append({
            "document_id": name, "record_present": record is not None,
            **{field: None if record is None else deepcopy(record.get(field)) for field in _PROVENANCE},
        })

    sources = {row["response_source"] for row in report["provenance"] if row["record_present"]}
    if sources == {"mock"}:
        report["observation_kind"] = "mock-fixture-agreement"
        report["interpretation"] = "Mock fixture agreement checks comparison and application behavior; it is not a measurement of live model accuracy."
    elif sources and "mock" not in sources and None not in sources:
        report["observation_kind"] = "provider-observation"
        report["interpretation"] = "Provider-labelled records were compared with source expectations. Recorded labels alone do not independently establish that a live provider call occurred."
    else:
        report["observation_kind"] = "mixed-observation" if sources else "no-observations"
        report["interpretation"] = "Response-source labels are reported per document. Mixed or missing records are not a single live model accuracy measurement."
    return report
