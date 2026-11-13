"""Bounded per-operator workers with durable queues and fenced leases.

Every accepted input is committed before dispatch. A graceful stop drains
in-flight calls and leaves queued sources available for resume. Each worker
and heartbeat owns its SQLite connection; provider calls hold no transaction.
"""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import logging
import math
from pathlib import Path
import threading
import time
from typing import Any, Callable, Iterable

from .providers import Provider
from .state import ClaimBusy, ProcessingConfig, StateStore, StaleClaimError


LOGGER = logging.getLogger(__name__)


def _settings(max_workers: int, lease_seconds: float, heartbeat_interval: float | None) -> float:
    if type(max_workers) is not int or not 1 <= max_workers <= 32:
        raise ValueError("max_workers must be an integer from 1 to 32")
    if type(lease_seconds) not in {int, float} or not math.isfinite(lease_seconds) or lease_seconds <= 0:
        raise ValueError("lease_seconds must be finite and positive")
    interval = min(5.0, lease_seconds / 3) if heartbeat_interval is None else heartbeat_interval
    if type(interval) not in {int, float} or not math.isfinite(interval) or not 0 < interval < lease_seconds:
        raise ValueError("heartbeat_interval must be positive and shorter than the lease")
    return interval


def _execute(
    database: Path, claim: dict[str, Any], *, provider_factory: Callable[[], Provider],
    operator: str, max_workers: int, lease_seconds: float, heartbeat_interval: float,
    clock: Callable[[], float],
) -> dict[str, Any]:
    with StateStore(database, clock=clock, initialize=False) as store:
        try:
            active = store.activate_claim(
                claim, operator=operator, lease_seconds=lease_seconds,
                execution={"max_workers": max_workers, "lease_seconds": lease_seconds, "heartbeat_interval": heartbeat_interval},
            )
        except ClaimBusy as exc:
            return exc.claim
        except StaleClaimError:
            return {**claim, "state": "superseded"}
        if active["state"] == "completed":
            return active
        done = threading.Event()
        lost = threading.Event()

        def heartbeat():
            if done.wait(heartbeat_interval):
                return
            try:
                with StateStore(database, clock=clock, initialize=False) as leases:
                    while not done.is_set():
                        leases.renew_claim(active, lease_seconds=lease_seconds)
                        if done.wait(heartbeat_interval):
                            return
            except StaleClaimError:
                lost.set()
            except Exception:
                # Do not expose raw exception messages or config in logs. A
                # failed heartbeat stops renewal; fenced completion still
                # checks the durable expiry/owner before publishing.
                LOGGER.error("Lease heartbeat failed attempt_id=%s", active["attempt_id"])
                lost.set()

        thread = threading.Thread(target=heartbeat, name="intake-lease", daemon=True)
        thread.start()
        try:
            provider = provider_factory()
            callback_factory = getattr(provider, "with_retry_callback", None)
            if callback_factory is not None:
                provider = callback_factory(lambda category, attempt, delay: store.note_retry(active, category, attempt, delay))
            if lost.is_set():
                raise StaleClaimError("Lease renewal failed before provider call")
            return store.run_claim(active, provider=provider)
        except StaleClaimError:
            LOGGER.info("Worker lost claim attempt_id=%s", active["attempt_id"])
            return {**active, "state": "lost"}
        finally:
            done.set()
            thread.join()


def _dispatch(
    database: Path, accepted: list[dict[str, Any]], *, provider_factory: Callable[[], Provider],
    operator: str, max_workers: int, lease_seconds: float, heartbeat_interval: float,
    clock: Callable[[], float], stop_event: threading.Event | None,
    on_result: Callable[[dict[str, Any]], None] | None,
) -> list[dict[str, Any]]:
    stop = threading.Event() if stop_event is None else stop_event
    results = {claim["attempt_id"]: claim for claim in accepted}
    jobs = iter([claim for claim in results.values() if claim["state"] == "pending"])
    for claim in results.values():
        if claim["state"] == "completed" and on_result is not None:
            on_result(claim)
    exhausted = False
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="intake") as pool:
        active = {}
        while active or (not exhausted and not stop.is_set()):
            while len(active) < max_workers and not exhausted and not stop.is_set():
                try:
                    claim = next(jobs)
                except StopIteration:
                    exhausted = True
                    break
                future = pool.submit(
                    _execute, database, claim, provider_factory=provider_factory, operator=operator,
                    max_workers=max_workers, lease_seconds=lease_seconds, heartbeat_interval=heartbeat_interval,
                    clock=clock,
                )
                active[future] = claim["attempt_id"]
            if not active:
                break
            completed, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in completed:
                attempt_id = active.pop(future)
                result = future.result()
                results[attempt_id] = result
                if result["state"] == "completed" and on_result is not None:
                    on_result(result)
    return list(results.values())


def run_intake(
    database: Path, paths: Iterable[Path], *, provider_factory: Callable[[], Provider],
    config: ProcessingConfig, operator: str, max_workers: int = 4,
    lease_seconds: float = 300, heartbeat_interval: float | None = None,
    clock: Callable[[], float] = time.time, stop_event: threading.Event | None = None,
    on_result: Callable[[dict[str, Any]], None] | None = None, force: bool = False,
) -> list[dict[str, Any]]:
    interval = _settings(max_workers, lease_seconds, heartbeat_interval)
    database = Path(database)
    accepted = []
    with StateStore(database, clock=clock) as store:
        for path in paths:
            try:
                accepted.append(store.claim(
                    path, config=config, operator=operator, lease_seconds=lease_seconds, force=force, queued=True,
                ))
            except ClaimBusy as exc:
                accepted.append(exc.claim)
    return _dispatch(
        database, accepted, provider_factory=provider_factory, operator=operator, max_workers=max_workers,
        lease_seconds=lease_seconds, heartbeat_interval=interval, clock=clock,
        stop_event=stop_event, on_result=on_result,
    )


def resume(
    database: Path, *, provider_factory: Callable[[], Provider], operator: str,
    max_workers: int = 4, lease_seconds: float = 300, heartbeat_interval: float | None = None,
    clock: Callable[[], float] = time.time, stop_event: threading.Event | None = None,
    on_result: Callable[[dict[str, Any]], None] | None = None, retry_errors: bool = False,
    provider_kind: str | None = None,
) -> list[dict[str, Any]]:
    """Resume queued/expired work; opt in to retry latest completed errors.

    Live leases owned by another worker are skipped. No input/configuration
    files are needed: recovery restores the original committed snapshots.
    """
    interval = _settings(max_workers, lease_seconds, heartbeat_interval)
    database = Path(database)
    accepted = []
    with StateStore(database, clock=clock) as store:
        for candidate in store.recovery_candidates(retry_errors=retry_errors):
            if provider_kind is not None and store.configuration(candidate["attempt_id"])["provider_kind"] != provider_kind:
                continue
            try:
                accepted.append(store.recover_claim(candidate, operator=operator, lease_seconds=lease_seconds))
            except (ClaimBusy, StaleClaimError):
                continue
    return _dispatch(
        database, accepted, provider_factory=provider_factory, operator=operator, max_workers=max_workers,
        lease_seconds=lease_seconds, heartbeat_interval=interval, clock=clock,
        stop_event=stop_event, on_result=on_result,
    )


def retry(
    database: Path, document_id: str, *, provider_factory: Callable[[], Provider], operator: str,
    config: ProcessingConfig | None = None, lease_seconds: float = 300,
    heartbeat_interval: float | None = None, clock: Callable[[], float] = time.time,
    stop_event: threading.Event | None = None,
) -> list[dict[str, Any]]:
    interval = _settings(1, lease_seconds, heartbeat_interval)
    database = Path(database)
    with StateStore(database, clock=clock) as store:
        claim = store.retry_document(document_id, config=config, operator=operator, lease_seconds=lease_seconds)
    return _dispatch(
        database, [claim], provider_factory=provider_factory, operator=operator, max_workers=1,
        lease_seconds=lease_seconds, heartbeat_interval=interval, clock=clock,
        stop_event=stop_event, on_result=None,
    )
