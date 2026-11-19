"""Immutable export generations with a single atomic current reference.

Capture the reference once before reading either file. Publication is serialized
per output directory, while readers of existing generations need no lock.
"""
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .state import StateStore


_FILES = ("queue.json", "extractions.jsonl")
_QUEUE_FIELDS = (
    "document_id", "source_sha256", "attempt_id", "source_revision", "ingestion_sequence",
    "status", "owner", "next_action", "error", "response_source", "processing_fingerprint",
    "extraction_fingerprint", "policy_sha256", "prompt_sha256", "schema_sha256",
    "extraction_attempt_id", "routing_attempt_id", "operator", "attempt_kind",
    "error_category", "retry_command",
)


@dataclass(frozen=True)
class ExportSnapshot:
    generation: str
    directory: Path

    def read(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        manifest = json.loads((self.directory / "manifest.json").read_text(encoding="utf-8"))
        if manifest["generation"] != self.generation:
            raise ValueError("Export generation identity does not match its manifest")
        content = {}
        for name in _FILES:
            raw = (self.directory / name).read_bytes()
            if hashlib.sha256(raw).hexdigest() != manifest["sha256"][name]:
                raise ValueError("Export generation failed its integrity check")
            content[name] = raw.decode("utf-8")
        queue = json.loads(content["queue.json"])
        extractions = [json.loads(line) for line in content["extractions.jsonl"].splitlines()]
        if len(queue) != manifest["records"] or len(extractions) != manifest["records"]:
            raise ValueError("Export generation record counts disagree")
        return queue, extractions


def capture(output: Path) -> ExportSnapshot:
    """Resolve current exactly once; retain this object for both file reads."""
    output = Path(output).resolve()
    target = os.readlink(output / "current")
    directory = (output / target).resolve(strict=True)
    if directory.parent != (output / "generations").resolve() or not directory.is_dir():
        raise ValueError("Current export reference is outside the generation directory")
    if not all((directory / name).is_file() for name in (*_FILES, "manifest.json")):
        raise ValueError("Current export generation is incomplete")
    return ExportSnapshot(directory.name, directory)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write(path: Path, raw: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _replace_link(path: Path, target: str, generation: str) -> None:
    temporary = path.with_name(f".{path.name}.{generation}.tmp")
    temporary.symlink_to(target)
    os.replace(temporary, path)


def publish(
    database: Path, output: Path, *, checkpoint: Callable[[str, str], None] | None = None,
) -> ExportSnapshot:
    """Write a complete generation, then switch current in one rename.

    Files already published are never rewritten or removed. A failed/crashed
    publisher may leave an unreferenced generation, which readers ignore.
    Checkpoints support reproducible publication tests and never run by default.
    """
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    generations = output / "generations"
    generations.mkdir(exist_ok=True)
    with (output / ".publish.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        generation = uuid4().hex
        directory = generations / generation

        def mark(stage: str):
            if checkpoint is not None:
                checkpoint(stage, generation)

        with StateStore(database) as state:
            records = state.export_snapshot()
        mark("snapshot-captured")
        directory.mkdir()
        mark("generation-created")
        queue = [{field: row.get(field) for field in _QUEUE_FIELDS} for row in records]
        raw_queue = (json.dumps(queue, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        _write(directory / "queue.json", raw_queue)
        mark("queue-written")
        raw_extractions = "".join(json.dumps(row, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n" for row in records).encode("utf-8")
        _write(directory / "extractions.jsonl", raw_extractions)
        mark("extractions-written")
        manifest = {
            "generation": generation, "records": len(records),
            "sha256": {"queue.json": hashlib.sha256(raw_queue).hexdigest(), "extractions.jsonl": hashlib.sha256(raw_extractions).hexdigest()},
        }
        _write(directory / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
        _sync_directory(directory)
        _sync_directory(generations)
        mark("generation-ready")
        for name in _FILES:
            _replace_link(output / name, f"current/{name}", generation)
        _sync_directory(output)
        mark("before-publish")
        _replace_link(output / "current", f"generations/{generation}", generation)
        _sync_directory(output)
        mark("published")
        return ExportSnapshot(generation, directory)
