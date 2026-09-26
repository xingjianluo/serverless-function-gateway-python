"""Invocation engine: concurrency limits, timeouts, idempotent dedupe.

Design contract
---------------
* **One request id, one conclusion.** A request id admitted for execution
  has exactly one *owner*; every later submission with the same id and the
  same parameters attaches to the owner's record and converges to the same
  terminal state. The same id with different parameters is rejected with
  ``REQUEST_CONFLICT``.
* **No silent queueing.** If a version is already executing at its
  ``max_concurrency``, the slot acquisition fails immediately and the
  request terminates as ``THROTTLED`` (no wait queue).
* **No runaway execution.** The handler runs in a worker thread with a
  hard deadline. Missing it terminates the record as ``TIMED_OUT``; slot
  and counters are reclaimed at that moment, never on the late return.
* **Version pinning.** The concrete :class:`FunctionVersion` is resolved
  once, at admission, and stored on the entry. Alias switches or
  disable/enable afterwards cannot redirect an in-flight call.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from typing import Any

from .errors import InvalidRequestError, RequestConflictError
from .types import (
    FunctionVersion,
    InvocationRecord,
    InvocationRequest,
    InvocationState,
)

logger = logging.getLogger("gateway.engine")

# Upper bound on simultaneously alive handler worker threads. Per-version
# limits reject excess work up front; this pool is only a global ceiling.
_DEFAULT_POOL_SIZE = 256

# Defensive ceiling when a duplicate sync submission waits for its owner.
_DUPLICATE_WAIT_SECONDS = 300.0


@dataclass(frozen=True)
class _Fingerprint:
    """What "same request" means: every caller-provided parameter."""

    function_name: str
    target_kind: str
    target_name: str
    payload: Any
    payload_signature: str | None = None

    @staticmethod
    def of(request: InvocationRequest) -> "_Fingerprint":
        payload = request.payload
        try:
            hash(payload)
            signature = None
        except TypeError:
            signature = repr(payload)
        return _Fingerprint(
            function_name=request.function_name,
            target_kind=request.target.kind.value,
            target_name=request.target.name,
            payload=payload if signature is None else None,
            payload_signature=signature,
        )


@dataclass
class _Slot:
    """Per-version concurrency accounting."""

    semaphore: threading.Semaphore
    inflight: int = 0


@dataclass
class _Entry:
    """Bookkeeping for one admitted request id."""

    fingerprint: _Fingerprint
    record: InvocationRecord
    version: FunctionVersion
    done: threading.Event = field(default_factory=threading.Event)
    owner_active: bool = False


class InvocationEngine:
    """Executes registered function versions with limits and dedupe."""

    def __init__(
        self,
        registry: Any,
        *,
        pool_size: int = _DEFAULT_POOL_SIZE,
    ) -> None:
        self._registry = registry
        self._lock = threading.RLock()
        self._entries: dict[str, _Entry] = {}
        self._slots: dict[tuple[str, str], _Slot] = {}
        self._pool = ThreadPoolExecutor(
            max_workers=pool_size,
            thread_name_prefix="fn",
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def invoke_sync(self, request: InvocationRequest) -> InvocationRecord:
        """Run to a terminal state and return the (shared) record."""
        entry, is_owner = self._admit(
            request, initial_state=InvocationState.RUNNING
        )
        if is_owner:
            return self._run(request, entry)
        entry.done.wait(timeout=_DUPLICATE_WAIT_SECONDS)
        logger.info(
            "invoke.duplicate request_id=%s state=%s "
            "reason=shared_request_id_converged",
            request.request_id,
            entry.record.state.value,
        )
        return entry.record

    def invoke_async(self, request: InvocationRequest) -> InvocationRecord:
        """Schedule background execution; return the ACCEPTED record."""
        entry, is_owner = self._admit(
            request, initial_state=InvocationState.ACCEPTED
        )
        if is_owner:
            # Snapshot before the worker can advance the state, so the
            # caller deterministically observes ACCEPTED.
            snapshot = entry.record
            worker = threading.Thread(
                target=self._run_async_safely,
                args=(request, entry),
                name=f"async-{request.request_id[:8]}",
                daemon=True,
            )
            worker.start()
            return snapshot
        logger.info(
            "invoke.duplicate_async request_id=%s state=%s "
            "reason=shared_request_id_attached",
            request.request_id,
            entry.record.state.value,
        )
        return entry.record

    def get_result(self, request_id: str) -> InvocationRecord | None:
        """Return the current record snapshot for a request id."""
        with self._lock:
            entry = self._entries.get(request_id)
            return entry.record if entry is not None else None

    def inflight_count(self, function_name: str, version: str) -> int:
        """Current in-flight executions for a concrete version."""
        with self._lock:
            slot = self._slots.get((function_name, version))
            return slot.inflight if slot is not None else 0

    def shutdown(self, wait: bool = False) -> None:
        """Release the worker pool (used by tests and shutdown hooks)."""
        self._pool.shutdown(wait=wait, cancel_futures=True)

    # ------------------------------------------------------------------
    # Admission: idempotency fence + registry resolution
    # ------------------------------------------------------------------

    def _admit(
        self,
        request: InvocationRequest,
        *,
        initial_state: InvocationState,
    ) -> tuple[_Entry, bool]:
        rid = request.request_id
        if not isinstance(rid, str) or not rid.strip():
            raise InvalidRequestError("request_id must be a non-empty string")

        fp = _Fingerprint.of(request)

        with self._lock:
            entry = self._entries.get(rid)
            if entry is None:
                # Resolve *before* creating the entry: pre-admission
                # rejections are reported as errors, not stored records, so a
                # corrected retry with the same id remains possible.
                fv = self._registry.resolve(
                    request.function_name, request.target
                )
                entry = _Entry(
                    fingerprint=fp,
                    version=fv,
                    record=InvocationRecord(
                        request_id=rid,
                        function_name=request.function_name,
                        resolved_version=fv.version,
                        state=initial_state,
                    ),
                )
                self._entries[rid] = entry
                entry.owner_active = True
                logger.info(
                    "invoke.admit request_id=%s function=%s target=%s/%s "
                    "resolved_version=%s state=%s reason=first_submission",
                    rid,
                    request.function_name,
                    request.target.kind.value,
                    request.target.name,
                    fv.version,
                    initial_state.value,
                )
                return entry, True

        # Entry already exists: compare fingerprints without the global lock.
        if entry.fingerprint != fp:
            logger.info(
                "invoke.conflict request_id=%s "
                "reason=request_id_reused_with_different_parameters",
                rid,
            )
            raise RequestConflictError(
                f"request id {rid!r} was already submitted with different "
                "parameters",
                detail={"request_id": rid},
            )

        with self._lock:
            logger.info(
                "invoke.dedupe request_id=%s state=%s owner_active=%s "
                "reason=request_id_resubmitted",
                rid,
                entry.record.state.value,
                entry.owner_active,
            )
            return entry, False

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _run_async_safely(
        self, request: InvocationRequest, entry: _Entry
    ) -> None:
        """Async wrapper guaranteeing the record always reaches a terminal."""
        try:
            self._run(request, entry)
        except Exception as exc:  # pragma: no cover - defensive backstop
            logger.exception(
                "invoke.background_crash request_id=%s", request.request_id
            )
            if not entry.record.state.terminal:
                self._finish(
                    entry,
                    state=InvocationState.FAILED,
                    error=f"{type(exc).__name__}: {exc}",
                    error_code="HANDLER_ERROR",
                    finished_at=time.monotonic(),
                )
                entry.owner_active = False
                entry.done.set()

    def _run(self, request: InvocationRequest, entry: _Entry) -> InvocationRecord:
        rid = request.request_id
        fv = entry.version  # pinned once at admission; never re-resolved
        cfg = fv.config
        slot = self._get_slot(fv.function_name, fv.version, cfg.max_concurrency)

        # ---- concurrency gate: immediate refusal, never queued ----
        if not slot.semaphore.acquire(blocking=False):
            self._finish(
                entry,
                state=InvocationState.THROTTLED,
                error=(
                    f"concurrency limit {cfg.max_concurrency} reached for "
                    f"{fv.function_name}:{fv.version}"
                ),
                error_code="THROTTLED",
                finished_at=time.monotonic(),
            )
            self._close_owner(entry)
            logger.info(
                "invoke.throttled request_id=%s function=%s version=%s "
                "limit=%d inflight=%d reason=concurrency_capacity_exceeded",
                rid,
                fv.function_name,
                fv.version,
                cfg.max_concurrency,
                slot.inflight,
            )
            return entry.record

        acquired = False
        try:
            with self._lock:
                slot.inflight += 1
                inflight = slot.inflight
            acquired = True
            started = time.monotonic()
            self._set_record(
                entry, state=InvocationState.RUNNING, started_at=started
            )
            logger.info(
                "invoke.start request_id=%s function=%s version=%s "
                "inflight=%d/%d deadline=%.3fs reason=slot_acquired",
                rid,
                fv.function_name,
                fv.version,
                inflight,
                cfg.max_concurrency,
                cfg.timeout_seconds,
            )

            try:
                future: Future[Any] = self._pool.submit(
                    fv.handler, request.payload
                )
            except RuntimeError as exc:  # pool shut down / cannot accept work
                self._finish(
                    entry,
                    state=InvocationState.FAILED,
                    error=f"executor unavailable: {exc}",
                    error_code="EXECUTOR_SHUTDOWN",
                    finished_at=time.monotonic(),
                )
                return entry.record

            done, _ = wait(
                [future],
                timeout=cfg.timeout_seconds,
                return_when=FIRST_COMPLETED,
            )
            if done:
                try:
                    result = future.result()
                except Exception as exc:
                    self._finish(
                        entry,
                        state=InvocationState.FAILED,
                        error=f"{type(exc).__name__}: {exc}",
                        error_code="HANDLER_ERROR",
                        finished_at=time.monotonic(),
                    )
                    logger.info(
                        "invoke.failed request_id=%s version=%s "
                        "error=%s reason=handler_exception",
                        rid,
                        fv.version,
                        type(exc).__name__,
                    )
                else:
                    self._finish(
                        entry,
                        state=InvocationState.SUCCEEDED,
                        result=result,
                        finished_at=time.monotonic(),
                    )
                    logger.info(
                        "invoke.succeeded request_id=%s version=%s "
                        "reason=handler_returned",
                        rid,
                        fv.version,
                    )
            else:
                # Best-effort cancel (only works if the worker never started);
                # reclaim state now rather than waiting for the late return.
                future.cancel()
                self._finish(
                    entry,
                    state=InvocationState.TIMED_OUT,
                    error=(
                        f"execution exceeded {cfg.timeout_seconds}s for "
                        f"{fv.function_name}:{fv.version}"
                    ),
                    error_code="TIMED_OUT",
                    finished_at=time.monotonic(),
                )
                logger.info(
                    "invoke.timed_out request_id=%s version=%s "
                    "deadline=%.3fs reason=timeout_budget_exhausted "
                    "note=worker_may_finish_late_but_record_is_final",
                    rid,
                    fv.version,
                    cfg.timeout_seconds,
                )
            return entry.record
        finally:
            if acquired:
                with self._lock:
                    slot.inflight -= 1
                    remaining = slot.inflight
                slot.semaphore.release()
                logger.info(
                    "invoke.reclaim request_id=%s version=%s inflight=%d "
                    "reason=execution_scope_closed",
                    rid,
                    fv.version,
                    remaining,
                )
            self._close_owner(entry)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _close_owner(self, entry: _Entry) -> None:
        """Release idempotency ownership and wake converging duplicates."""
        with self._lock:
            entry.owner_active = False
        entry.done.set()

    def _get_slot(
        self, function_name: str, version: str, max_concurrency: int
    ) -> _Slot:
        with self._lock:
            key = (function_name, version)
            slot = self._slots.get(key)
            if slot is None:
                slot = _Slot(
                    semaphore=threading.Semaphore(max_concurrency),
                )
                self._slots[key] = slot
            return slot

    def _set_record(self, entry: _Entry, **changes: Any) -> None:
        with self._lock:
            entry.record = replace(entry.record, **changes)

    def _finish(
        self,
        entry: _Entry,
        *,
        state: InvocationState,
        finished_at: float,
        result: Any = None,
        error: str | None = None,
        error_code: str | None = None,
    ) -> None:
        with self._lock:
            entry.record = replace(
                entry.record,
                state=state,
                result=result,
                error=error,
                error_code=error_code,
                finished_at=finished_at,
            )
