"""One-host SQLite state: retained sources/configurations, claims and results.

Completed attempts are immutable. Current pointers are separate from history;
pending claims never impersonate a completed result. Provider I/O occurs outside
transactions. Worker orchestration, lease renewal and recovery are step 6 work.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, Iterable
from uuid import uuid4

from jsonschema import Draft202012Validator

from . import prompts, review
from .extraction import ExtractionError, ExtractionRecord, SourceExport, decode_export, extract_source
from .processing import _failure
from .providers import Provider


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class ProcessingConfig:
    """Immutable JSON snapshot; accessors return fresh, caller-owned values."""
    _snapshot_json: str

    @classmethod
    def capture(cls, *, provider: Provider, policy_path: Path) -> "ProcessingConfig":
        if provider.provider_kind == "http":
            assets = provider.extraction_assets()
        elif provider.provider_kind == "mock":
            assets = {
                "model_configuration": {
                    "provider": "mock", "model": "scripted-fixtures",
                    "generation": {}, "prompt_version": prompts.PROMPT_VERSION,
                },
                "prompt_text": prompts.PROMPT_PATH.read_bytes().decode("utf-8"),
                "schema_text": prompts.SCHEMA_PATH.read_bytes().decode("utf-8"),
            }
        else:
            raise ValueError("Unsupported provider kind for configuration capture")
        data = dict(assets, provider_kind=provider.provider_kind)
        data["policy_text"] = Path(policy_path).read_bytes().decode("utf-8")
        review.ReviewPolicy.from_text(data["policy_text"])
        Draft202012Validator.check_schema(json.loads(data["schema_text"]))
        for name in ("prompt", "schema", "policy"):
            data[name + "_sha256"] = _hash(data[name + "_text"].encode("utf-8"))
        extraction_identity = {key: data[key] for key in (
            "provider_kind", "model_configuration", "prompt_sha256", "schema_sha256",
        )}
        data["extraction_fingerprint"] = _hash(_json(extraction_identity).encode("utf-8"))
        data["processing_fingerprint"] = _hash(_json({
            "extraction_fingerprint": data["extraction_fingerprint"],
            "policy_sha256": data["policy_sha256"],
        }).encode("utf-8"))
        return cls(_json(data))

    def snapshot(self) -> dict[str, Any]:
        return json.loads(self._snapshot_json)

    @property
    def model_configuration(self) -> dict[str, Any]:
        return self.snapshot()["model_configuration"]

    @property
    def extraction_fingerprint(self) -> str:
        return self.snapshot()["extraction_fingerprint"]

    @property
    def processing_fingerprint(self) -> str:
        return self.snapshot()["processing_fingerprint"]

    @property
    def prompt_sha256(self) -> str:
        return self.snapshot()["prompt_sha256"]

    @property
    def schema_sha256(self) -> str:
        return self.snapshot()["schema_sha256"]

    @property
    def policy_sha256(self) -> str:
        return self.snapshot()["policy_sha256"]


class ClaimBusy(Exception):
    def __init__(self, claim: dict[str, Any]):
        super().__init__("This source/configuration already has an active claim")
        self.claim = claim


class StaleClaimError(Exception):
    """A lost/expired claim cannot publish a result."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    ingestion_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    source_sha256 TEXT NOT NULL,
    original_bytes BLOB NOT NULL,
    UNIQUE(document_id, revision)
);
CREATE TABLE IF NOT EXISTS configurations (
    processing_fingerprint TEXT PRIMARY KEY,
    snapshot_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL UNIQUE,
    document_id TEXT NOT NULL,
    source_sequence INTEGER NOT NULL REFERENCES sources(ingestion_sequence),
    config_fingerprint TEXT NOT NULL REFERENCES configurations(processing_fingerprint),
    state TEXT NOT NULL CHECK(state IN ('pending', 'completed', 'abandoned')),
    lease_owner TEXT NOT NULL,
    lease_expires_at REAL NOT NULL,
    fencing_token INTEGER NOT NULL,
    claim_json TEXT NOT NULL,
    result_json TEXT,
    raw_extraction_json TEXT
);
CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    source_sequence INTEGER NOT NULL REFERENCES sources(ingestion_sequence),
    latest_attempt_id TEXT REFERENCES attempts(attempt_id),
    current_attempt_id TEXT REFERENCES attempts(attempt_id)
);
CREATE TABLE IF NOT EXISTS attempt_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
    event TEXT NOT NULL,
    recorded_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS attempts_document ON attempts(document_id, sequence);
CREATE INDEX IF NOT EXISTS attempts_pending ON attempts(state, sequence);
CREATE TRIGGER IF NOT EXISTS completed_attempt_immutable BEFORE UPDATE ON attempts
WHEN OLD.state = 'completed' BEGIN SELECT RAISE(ABORT, 'Completed attempts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS attempts_retained BEFORE DELETE ON attempts
BEGIN SELECT RAISE(ABORT, 'Attempts must be retained'); END;
"""


class StateStore:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._connection = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.executescript(_SCHEMA)
        for table in ("sources", "configurations", "attempt_events"):
            for action in ("UPDATE", "DELETE"):
                self._connection.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'Snapshots and events are immutable'); END"
                )

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    @contextmanager
    def _transaction(self):
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield self._connection
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise

    def _attempt(self, attempt_id: str) -> sqlite3.Row:
        row = self._connection.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row is None:
            raise KeyError("Unknown attempt ID")
        return row

    def configuration(self, attempt_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT c.snapshot_json FROM attempts a JOIN configurations c "
            "ON c.processing_fingerprint=a.config_fingerprint WHERE a.attempt_id=?", (attempt_id,),
        ).fetchone()
        if row is None:
            raise KeyError("Unknown attempt ID")
        return json.loads(row["snapshot_json"])

    def _source_row(self, attempt_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT s.* FROM sources s JOIN attempts a ON a.source_sequence=s.ingestion_sequence "
            "WHERE a.attempt_id=?", (attempt_id,),
        ).fetchone()
        if row is None:
            raise KeyError("Unknown attempt ID")
        return row

    def source(self, attempt_id: str) -> SourceExport:
        row = self._source_row(attempt_id)
        return decode_export(row["original_bytes"], document_id=row["document_id"])

    def current(self) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT a.result_json FROM documents d JOIN attempts a ON a.attempt_id=d.current_attempt_id "
            "WHERE a.state='completed' ORDER BY d.document_id"
        ).fetchall()
        return [json.loads(row["result_json"]) for row in rows]

    def history(self, document_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT result_json FROM attempts WHERE document_id=? AND state='completed' ORDER BY sequence",
            (document_id,),
        ).fetchall()
        return [json.loads(row["result_json"]) for row in rows]

    def pending(self) -> list[dict[str, Any]]:
        rows = self._connection.execute("SELECT claim_json FROM attempts WHERE state='pending' ORDER BY sequence").fetchall()
        return [json.loads(row["claim_json"]) for row in rows]

    def claim(
        self, path: Path, *, config: ProcessingConfig, operator: str,
        lease_seconds: float = 300, force: bool = False,
    ) -> dict[str, Any]:
        """Atomically accept a source/configuration and reserve one attempt.

        Reusable successes are returned unchanged. Active duplicate claims raise
        ClaimBusy. Files must be readable to accept a source revision; malformed
        readable bytes can still produce a durable invalid-export result.
        """
        if not isinstance(operator, str) or not operator.strip():
            raise ValueError("operator must identify the claimant")
        if not isinstance(lease_seconds, (int, float)) or not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be finite and positive")
        path = Path(path)
        raw = path.read_bytes()
        source_hash = _hash(raw)
        snapshot = config.snapshot()
        now = self._clock()
        with self._transaction() as db:
            document = db.execute("SELECT * FROM documents WHERE document_id=?", (path.name,)).fetchone()
            previous_source = None if document is None else db.execute(
                "SELECT * FROM sources WHERE ingestion_sequence=?", (document["source_sequence"],),
            ).fetchone()
            if previous_source is not None and previous_source["source_sha256"] == source_hash:
                source_sequence = previous_source["ingestion_sequence"]
                revision = previous_source["revision"]
            else:
                revision = 1 if previous_source is None else previous_source["revision"] + 1
                source_sequence = db.execute(
                    "INSERT INTO sources(document_id,revision,source_sha256,original_bytes) VALUES(?,?,?,?)",
                    (path.name, revision, source_hash, raw),
                ).lastrowid
            db.execute(
                "INSERT INTO configurations(processing_fingerprint,snapshot_json) VALUES(?,?) ON CONFLICT DO NOTHING",
                (config.processing_fingerprint, config._snapshot_json),
            )
            latest = None if document is None or document["latest_attempt_id"] is None else self._attempt(document["latest_attempt_id"])
            same_source = latest is not None and latest["source_sequence"] == source_sequence
            latest_result = None if latest is None or latest["state"] != "completed" else json.loads(latest["result_json"])
            if not force and same_source and latest["config_fingerprint"] == config.processing_fingerprint:
                if latest_result is not None and latest_result["error"] is None:
                    return latest_result
                if latest["state"] == "pending" and latest["lease_expires_at"] > now:
                    raise ClaimBusy(json.loads(latest["claim_json"]))
            if latest is not None and latest["state"] == "pending" and latest["lease_expires_at"] <= now:
                db.execute("UPDATE attempts SET state='abandoned' WHERE attempt_id=?", (latest["attempt_id"],))
                db.execute("INSERT INTO attempt_events(attempt_id,event,recorded_at) VALUES(?,?,?)", (latest["attempt_id"], "abandoned", now))
            routing_only = bool(
                not force and same_source and latest_result is not None
                and latest_result["error"] is None
                and latest_result["extraction_fingerprint"] == config.extraction_fingerprint
            )
            attempt_id = uuid4().hex
            token = db.execute(
                "SELECT COALESCE(MAX(fencing_token),0)+1 FROM attempts WHERE document_id=?", (path.name,),
            ).fetchone()[0]
            claim = {
                "attempt_id": attempt_id, "document_id": path.name,
                "source_sha256": source_hash, "source_revision": revision,
                "ingestion_sequence": source_sequence, "state": "pending",
                "operator": operator, "lease_owner": operator,
                "lease_expires_at": now + lease_seconds, "fencing_token": token,
                "created_at": now, "attempt_kind": "routing" if routing_only else "extraction",
                "extraction_attempt_id": latest_result["extraction_attempt_id"] if routing_only else attempt_id,
                "routing_attempt_id": attempt_id,
                **{key: snapshot[key] for key in (
                    "model_configuration", "extraction_fingerprint", "processing_fingerprint",
                    "prompt_sha256", "schema_sha256", "policy_sha256",
                )},
            }
            db.execute(
                "INSERT INTO attempts(attempt_id,document_id,source_sequence,config_fingerprint,state,lease_owner,lease_expires_at,fencing_token,claim_json) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (attempt_id, path.name, source_sequence, config.processing_fingerprint, "pending", operator, now + lease_seconds, token, _json(claim)),
            )
            db.execute(
                "INSERT INTO documents(document_id,source_sequence,latest_attempt_id) VALUES(?,?,?) "
                "ON CONFLICT(document_id) DO UPDATE SET source_sequence=excluded.source_sequence,latest_attempt_id=excluded.latest_attempt_id",
                (path.name, source_sequence, attempt_id),
            )
            db.execute("INSERT INTO attempt_events(attempt_id,event,recorded_at) VALUES(?,?,?)", (attempt_id, "claimed", now))
        return claim

    def _check_claim(self, claim: dict[str, Any]) -> sqlite3.Row:
        row = self._attempt(claim["attempt_id"])
        if row["fencing_token"] != claim["fencing_token"] or row["lease_owner"] != claim["lease_owner"]:
            raise StaleClaimError("Claim owner or fencing token no longer matches")
        if row["state"] == "completed":
            return row
        if row["state"] != "pending" or row["lease_expires_at"] <= self._clock():
            raise StaleClaimError("Claim has expired or was abandoned")
        return row

    def run_claim(self, claim: dict[str, Any], *, provider: Provider) -> dict[str, Any]:
        row = self._check_claim(claim)
        if row["state"] == "completed":
            return json.loads(row["result_json"])
        # Trust durable metadata, never a caller-mutated claim dictionary.
        metadata = json.loads(row["claim_json"])
        snapshot = self.configuration(row["attempt_id"])
        if provider.provider_kind != snapshot["provider_kind"]:
            raise ValueError("Runtime provider kind does not match the saved configuration")
        runtime = provider.with_snapshot(snapshot) if provider.provider_kind == "http" else provider
        policy = review.ReviewPolicy.from_text(snapshot["policy_text"])
        raw_extraction = None
        try:
            source = self.source(row["attempt_id"])
            if metadata["attempt_kind"] == "routing":
                original = self._attempt(metadata["extraction_attempt_id"])
                raw_extraction = json.loads(original["raw_extraction_json"])
                original_result = json.loads(original["result_json"])
                extracted = ExtractionRecord(source.document_id, source.source_sha256, raw_extraction, original_result["response_source"])
            else:
                extracted = extract_source(source, provider=runtime, schema_text=snapshot["schema_text"])
                raw_extraction = extracted.extraction
            result = asdict(review.route_extraction(extracted, source=source, policy=policy))
        except ExtractionError as exc:
            result = asdict(_failure(metadata["document_id"], metadata["source_sha256"], exc.category, runtime))
        completed = {**metadata, **result, "state": "completed", "completed_at": self._clock()}
        with self._transaction() as db:
            latest = self._check_claim(claim)
            if latest["state"] == "completed":
                return json.loads(latest["result_json"])
            db.execute(
                "UPDATE attempts SET state='completed',result_json=?,raw_extraction_json=? WHERE attempt_id=?",
                (_json(completed), None if raw_extraction is None else _json(raw_extraction), row["attempt_id"]),
            )
            db.execute(
                "UPDATE documents SET current_attempt_id=? WHERE document_id=? AND latest_attempt_id=?",
                (row["attempt_id"], metadata["document_id"], row["attempt_id"]),
            )
            db.execute("INSERT INTO attempt_events(attempt_id,event,recorded_at) VALUES(?,?,?)", (row["attempt_id"], "completed", completed["completed_at"]))
        return completed

    def intake(
        self, paths: Iterable[Path], *, provider: Provider, config: ProcessingConfig,
        operator: str, force: bool = False, lease_seconds: float = 300,
    ) -> list[dict[str, Any]]:
        results = []
        for path in paths:
            try:
                claim = self.claim(path, config=config, operator=operator, force=force, lease_seconds=lease_seconds)
            except ClaimBusy as exc:
                results.append(exc.claim)
                continue
            results.append(claim if claim["state"] == "completed" else self.run_claim(claim, provider=provider))
        return results
