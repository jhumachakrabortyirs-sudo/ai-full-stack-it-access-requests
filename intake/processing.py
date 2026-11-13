"""Independent document processing with actionable, credential-safe failures."""
from dataclasses import asdict, dataclass
import hashlib
import logging
from pathlib import Path
import shlex
from typing import Any, Iterable

from .extraction import ExtractionError, SourceExport, decode_export, extract_source
from .http_provider import RESPONSE_HANDLING
from .providers import Provider
from . import review


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProcessingRecord:
    document_id: str
    source_sha256: str | None
    extraction: dict[str, Any] | None
    response_source: str
    status: str
    owner: str
    next_action: str
    error: str | None = None
    error_category: str | None = None
    retry_command: str | None = None


def _failure(document_id: str, source_hash: str | None, category: str, provider: Provider) -> ProcessingRecord:
    if category not in RESPONSE_HANDLING:
        category = "provider-error"
    command = shlex.join([
        "python3", "-m", "intake", "retry", "--document", document_id,
        "--provider", provider.provider_kind,
    ])
    # Never interpolate upstream bodies, credential values or raw exceptions.
    error = f"{document_id}: {category}. {RESPONSE_HANDLING[category]} Retry: {command}"
    LOGGER.warning("Document failed document_id=%s category=%s", document_id, category)
    return ProcessingRecord(
        document_id=document_id, source_sha256=source_hash, extraction=None,
        response_source=provider.response_source, status="error", owner="IT Service Desk",
        next_action="retry-extraction", error=error, error_category=category, retry_command=command,
    )


def process_source(source: SourceExport, *, provider: Provider) -> ProcessingRecord:
    """Process a single original-byte snapshot without re-reading its file."""
    try:
        record = extract_source(source, provider=provider)
        result = review.route_extraction(record, source=source)
    except ExtractionError as exc:
        return _failure(source.document_id, source.source_sha256, exc.category, provider)
    return ProcessingRecord(**asdict(result))


def process_file(path: Path, *, provider: Provider) -> ProcessingRecord:
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError:
        return _failure(path.name, None, "invalid-export", provider)
    source_hash = hashlib.sha256(raw).hexdigest()
    try:
        source = decode_export(raw, document_id=path.name)
    except ExtractionError as exc:
        return _failure(path.name, source_hash, exc.category, provider)
    return process_source(source, provider=provider)


def process_files(paths: Iterable[Path], *, provider: Provider) -> list[ProcessingRecord]:
    """Keep failures independent; bounded parallel workers are step 6 work."""
    return [process_file(path, provider=provider) for path in paths]
