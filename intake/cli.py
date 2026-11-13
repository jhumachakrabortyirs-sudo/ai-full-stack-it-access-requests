"""JSON operator interface for shared intake state and consistent exports."""
import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import signal
import sqlite3
import sys
import threading
import time
from typing import Callable

from . import exports, workers
from .http_provider import HttpProvider, ProviderSettings
from .mock_catalog import CatalogProvider
from .providers import Provider
from .prompts import PROMPT_VERSION
from .state import ClaimBusy, ProcessingConfig, StateStore, StaleClaimError


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path("state/intake.sqlite3")
DEFAULT_MOCKS = ROOT / "tests/mock_responses"


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    # Suppressed defaults preserve --db supplied before the subcommand while
    # also accepting replayable retry options after it.
    common.add_argument("--db", type=Path, default=argparse.SUPPRESS, help="Shared local SQLite database (default: state/intake.sqlite3)")
    parser = argparse.ArgumentParser(prog="python3 -m intake", parents=[common], description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    mutation = {}
    for name, help_text in (
        ("run", "Accept JSON exports and prepare human review records"),
        ("resume", "Resume queued/expired work from saved inputs"),
        ("retry", "Force a targeted retry using a saved source"),
    ):
        command = commands.add_parser(name, parents=[common], help=help_text)
        command.add_argument("--provider", choices=("mock", "http"), default=None)
        command.add_argument("--mock-dir", type=Path, default=None, help="Directory containing explicit catalog.json and fixtures")
        command.add_argument("--config", type=Path, default=ROOT / "config/provider.json", help="HTTP settings for new intake; retries/resume restore saved settings")
        command.add_argument("--operator", default="operator")
        command.add_argument("--workers", type=int, default=4)
        command.add_argument("--lease-seconds", type=float, default=300)
        command.add_argument("--heartbeat-interval", type=float, default=None)
        mutation[name] = command
    mutation["run"].add_argument("inputs", nargs="+", type=Path)
    mutation["run"].add_argument("--policy", type=Path, default=ROOT / "materials/office-policy.md")
    mutation["run"].add_argument("--force", action="store_true", help="Bypass unchanged-success reuse")
    mutation["resume"].add_argument("--retry-errors", action="store_true")
    mutation["retry"].add_argument("--document", required=True)
    for name, help_text in (("queue", "Inspect current completed records"), ("pending", "Inspect unfinished sources and service states")):
        commands.add_parser(name, parents=[common], help=help_text)
    history = commands.add_parser("history", parents=[common], help="Inspect completed attempt history or lifecycle events")
    history.add_argument("--document", required=True)
    history.add_argument("--events", action="store_true")
    export = commands.add_parser("export", parents=[common], help="Publish an immutable export generation or read the captured pair")
    export.add_argument("--output", type=Path, default=Path("deliverables"))
    export.add_argument("--read", action="store_true", help="Read one captured generation without republishing")
    return parser


@contextmanager
def _interrupts(stop: threading.Event):
    received = []
    previous = {}

    def handler(number, frame):
        received.append(number)
        stop.set()

    if threading.current_thread() is threading.main_thread():
        for number in (signal.SIGINT, signal.SIGTERM):
            previous[number] = signal.signal(number, handler)
    try:
        yield received
    finally:
        for number, original in previous.items():
            signal.signal(number, original)


def _emit(value) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False))


def _paths(inputs: list[Path]) -> list[Path]:
    paths = []
    for path in inputs:
        if path.is_dir():
            paths.extend(sorted(path.glob("*.json")))
        elif path.is_file():
            paths.append(path)
        else:
            raise ValueError(f"Input does not exist: {path}")
    paths = list(dict.fromkeys(path.resolve() for path in paths))
    if not paths:
        raise ValueError("No JSON exports found in the supplied inputs")
    return paths


def _provider(args, kind: str, snapshot=None) -> Provider:
    if kind == "mock":
        model = {} if snapshot is None else snapshot["model_configuration"]
        directory = args.mock_dir or model.get("mock_dir", DEFAULT_MOCKS)
        if snapshot is not None and "catalog_text" in snapshot and (args.command == "resume" or args.mock_dir is None):
            return CatalogProvider(Path(directory), snapshot=snapshot)
        return CatalogProvider(
            Path(directory), prompt_text=None if snapshot is None else snapshot["prompt_text"],
            schema_text=None if snapshot is None else snapshot["schema_text"],
            prompt_version=PROMPT_VERSION if snapshot is None else model["prompt_version"],
        )
    if snapshot is not None:
        values = dict(snapshot["model_configuration"])
        values.pop("prompt_version")
        return HttpProvider(settings=ProviderSettings(**values)).with_snapshot(snapshot)
    return HttpProvider(settings=ProviderSettings(**json.loads(args.config.read_text(encoding="utf-8"))))


def main(
    argv: list[str] | None = None, *, provider_factory: Callable[[], Provider] | None = None,
    clock: Callable[[], float] = time.time,
) -> int:
    args = _parser().parse_args(argv)
    database = getattr(args, "db", DEFAULT_DB).resolve()
    try:
        if args.command == "export":
            if args.read:
                snapshot = exports.capture(args.output)
                queue, extractions = snapshot.read()
                _emit({"generation": snapshot.generation, "queue": queue, "extractions": extractions})
            else:
                snapshot = exports.publish(database, args.output)
                queue, _ = snapshot.read()
                _emit({"generation": snapshot.generation, "directory": str(snapshot.directory), "records": len(queue)})
            return 0
        if args.command in {"queue", "pending", "history"}:
            with StateStore(database, clock=clock) as store:
                if args.command == "queue":
                    result = store.current()
                elif args.command == "pending":
                    result = store.pending()
                else:
                    result = store.events(args.document) if args.events else store.history(args.document)
            _emit(result)
            return 0

        snapshot = None
        kind = args.provider
        if args.command == "retry":
            with StateStore(database, clock=clock) as store:
                latest = store.latest(args.document)
                snapshot = store.configuration(latest["attempt_id"])
            kind = kind or snapshot["provider_kind"]
            if kind != snapshot["provider_kind"]:
                raise ValueError("Retry provider must match the saved attempt")
        elif args.command == "resume":
            with StateStore(database, clock=clock) as store:
                candidates = store.recovery_candidates(retry_errors=args.retry_errors)
                saved = [store.configuration(row["attempt_id"]) for row in candidates]
            kinds = {item["provider_kind"] for item in saved}
            if kind is None and len(kinds) > 1:
                raise ValueError("Mixed pending providers: select --provider mock or --provider http")
            kind = kind or next(iter(kinds), "mock")
            snapshot = next((item for item in saved if item["provider_kind"] == kind), None)
        else:
            kind = kind or "mock"

        factory = provider_factory or (lambda: _provider(args, kind, snapshot))
        config = None
        if args.command in {"run", "retry"}:
            provider = factory()
            if provider.provider_kind != kind:
                raise ValueError("Runtime provider does not match the selected provider kind")
            config = ProcessingConfig.capture(
                provider=provider,
                policy_path=args.policy if args.command == "run" else None,
                policy_text=None if snapshot is None else snapshot["policy_text"],
            )
            if provider_factory is None:
                # Immutable native providers are safe to share. Keep the
                # accepted catalog/settings available even if their files
                # disappear while queued work is being dispatched.
                factory = lambda: provider
        options = {
            "provider_factory": factory, "operator": args.operator,
            "lease_seconds": args.lease_seconds, "heartbeat_interval": args.heartbeat_interval,
            "clock": clock,
        }
        stop = threading.Event()
        with _interrupts(stop) as interrupted:
            if args.command == "run":
                result = workers.run_intake(
                    database, _paths(args.inputs), config=config, max_workers=args.workers,
                    force=args.force, stop_event=stop, **options,
                )
            elif args.command == "resume":
                result = workers.resume(
                    database, max_workers=args.workers, retry_errors=args.retry_errors,
                    provider_kind=kind, stop_event=stop, **options,
                )
            else:
                result = workers.retry(database, args.document, config=config, stop_event=stop, **options)
        _emit(result)
        if interrupted:
            return 128 + interrupted[0]
        return int(any(row.get("error") is not None for row in result))
    except (OSError, ValueError, KeyError, sqlite3.Error, ClaimBusy, StaleClaimError) as exc:
        print(f"intake: {exc}", file=sys.stderr)
        return 2
