"""Apply the ordered office policy to validated extractions.

The source is read once for extraction and routing. Source control-override
patterns provide an additional check when the provider omits an override issue;
they are finite heuristics, not a general semantic injection detector.
"""
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import re
from typing import Any

from .extraction import ExtractionRecord, SourceExport, extract_source, read_export
from .providers import Provider


REQUIRED_FACTS = (
    "request_id", "request_date", "requester", "team", "system", "environment",
    "permission", "business_purpose", "start_date", "end_date", "manager",
)
_OVERRIDE = re.compile(
    r"\bsystem\s+override\b"
    r"|\bbypass\s+(?:the\s+)?approval\s+rules\b"
    r"|\b(?:ignore|disregard|override)\b.{0,80}\b(?:instructions|rules|policy|prompt)\b"
    r"|\b(?:display|print|reveal|expose|show)\b.{0,50}\b(?:api[\s_-]*key|credentials?|secrets?)\b"
    r"|\bhighest[-\s]+priority\s+system\s+instruction\b"
    r"|\bmark\b.{0,30}\bapproved\b",
    re.IGNORECASE,
)
_OVERRIDE_ISSUE = re.compile(
    r"instruction[-\s]+override|override\s+attempt|prompt\s+injection", re.IGNORECASE
)


@dataclass(frozen=True)
class ReviewRecord:
    document_id: str
    source_sha256: str
    extraction: dict[str, Any]
    response_source: str
    status: str
    owner: str
    next_action: str
    error: None = None


def _missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _add_issue(extraction: dict[str, Any], issue: str) -> None:
    if issue not in extraction["issues"]:
        extraction["issues"].append(issue)


def route_extraction(record: ExtractionRecord, *, source: SourceExport) -> ReviewRecord:
    """Route a validated record from the matching source snapshot.

    Scalar facts are preserved. Add deterministic issues for missing fields,
    invalid dates, reversed ranges, identifier mismatch and detected overrides.
    The remaining issues represent unresolved extraction concerns under the
    prompt contract and conservatively require clarification after rules 1–2.
    """
    if (record.document_id, record.source_sha256) != (source.document_id, source.source_sha256):
        raise ValueError("Extraction record and routing source must identify the same snapshot")
    extraction = deepcopy(record.extraction)
    for field, value in extraction.items():
        if field not in {"supporting_quotes", "issues"} and _missing(value):
            if not any(field in issue for issue in extraction["issues"]):
                _add_issue(extraction, f"{field}: absent or unresolved in source")

    request_id = extraction["request_id"]
    filename_id = Path(source.document_id).stem
    mismatch = not _missing(request_id) and request_id != filename_id
    if mismatch:
        _add_issue(extraction, f"request_id {request_id} differs from filename identifier {filename_id}")

    dates: dict[str, date] = {}
    for field in ("request_date", "start_date", "end_date"):
        value = extraction[field]
        if _missing(value):
            continue
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError("Date format")
            dates[field] = date.fromisoformat(value)
        except ValueError:
            _add_issue(extraction, f"{field}: invalid YYYY-MM-DD date; clarify the source fact")
    if "start_date" in dates and "end_date" in dates and dates["start_date"] > dates["end_date"]:
        _add_issue(extraction, "start_date is after end_date; clarify the access window")

    override = bool(_OVERRIDE.search(source.message)) or any(
        _OVERRIDE_ISSUE.search(issue) for issue in extraction["issues"]
    )
    if override:
        _add_issue(extraction, "Instruction-override attempt in source; human inspection required")
        # Preserve the entire literal source when existing quotes do not cover
        # it, retaining the attempted instruction along with its context.
        if source.message not in extraction["supporting_quotes"]:
            extraction["supporting_quotes"].append(source.message)

    # Policy order is significant: override > withdrawal > clarification > risk
    # > complete staging/read. A manager never changes this into approval.
    if override:
        destination = ("review", "IT Service Desk", "inspect-message")
    elif extraction["kind"] == "withdrawal" and not _missing(request_id) and "request_date" in dates:
        destination = ("review", "IT Service Desk", "confirm-withdrawal")
    elif (
        extraction["kind"] not in {"new", "change"}
        or mismatch
        or any(_missing(extraction[field]) for field in REQUIRED_FACTS)
        or extraction["issues"]
    ):
        destination = ("needs-information", "IT Service Desk", "clarify")
    elif extraction["environment"] == "production" or extraction["permission"] in {"write", "admin"}:
        destination = ("review", "Security Review", "review-risk-then-owner")
    else:
        destination = ("review", "System Owner", "review-request")

    return ReviewRecord(
        document_id=record.document_id,
        source_sha256=record.source_sha256,
        extraction=extraction,
        response_source=record.response_source,
        status=destination[0],
        owner=destination[1],
        next_action=destination[2],
    )


def review_file(path: Path, *, provider: Provider) -> ReviewRecord:
    """Validate one source snapshot before applying the review rules.

    Extraction failures propagate as ExtractionError; failure queue records and
    operator retry commands are introduced in step 4.
    """
    source = read_export(path)
    record = extract_source(source, provider=provider)
    return route_extraction(record, source=source)
