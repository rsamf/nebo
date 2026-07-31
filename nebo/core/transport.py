"""Transport protocol — abstracts the SDK's event sink.

Two implementations live in the codebase:
  * NetworkTransport (nebo/core/client.py) — HTTP POST /events.
  * FileTransport (this module) — append-only .nebo file.

Both share the same in-memory event-dict shape, so SessionState
doesn't care which one is wired up.
"""

from __future__ import annotations

import atexit
import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from nebo.core.fileformat import NeboFileWriter

logger = logging.getLogger(__name__)

# Shutdown drain gives up only after this long with NO progress (no event
# resolved or written) — same env knob NetworkTransport uses for its
# shutdown budget. Progress-based rather than a fixed deadline: deferred
# media encoding can legally hold many seconds of backlog at exit, and a
# fixed join timeout truncates the run tail.
_DEFAULT_SHUTDOWN_STALL_S = 10.0


def _shutdown_stall_timeout() -> float:
    env = os.environ.get("NEBO_SHUTDOWN_TIMEOUT")
    if env is not None:
        try:
            return float(env)
        except ValueError:
            pass
    return _DEFAULT_SHUTDOWN_STALL_S


@runtime_checkable
class Transport(Protocol):
    def send_event(self, event: dict) -> None: ...
    def flush(self, timeout: float = 5.0) -> bool: ...
    def close(self) -> None: ...


class FileTransport:
    """Append-only `.nebo` writer running off a background thread.

    Mirrors the queue-and-flush shape of NetworkTransport so SessionState
    can treat both interchangeably.
    """

    def __init__(
        self,
        logdir: os.PathLike | str,
        run_id: str,
        script_path: str,
        flush_interval: float = 0.1,
        run_name: Optional[str] = None,
        group: str = "",
    ) -> None:
        self._logdir = Path(logdir)
        self._logdir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d_%H%M%S")
        self._filepath = self._logdir / f"{ts}_{run_id}.nebo"
        self._stream = self._filepath.open("wb")
        self._writer = NeboFileWriter(
            self._stream, run_id=run_id, script_path=script_path,
            run_name=run_name, group=group,
        )
        self._writer.write_header()
        # Seed implicit loggables exactly like the daemon does on run_start.
        for lid, kind in (("__global__", "global"), ("__agent__", "agent")):
            self._writer.write_entry(
                "loggable_register",
                {
                    "type": "loggable_register",
                    "loggable_id": lid,
                    "data": {"loggable_id": lid, "kind": kind},
                },
            )

        self._queue: queue.Queue[Optional[dict]] = queue.Queue()
        self._flush_interval = flush_interval
        self._running = True
        self._lock = threading.Lock()
        # Monotonic count of events resolved+written; close() watches it to
        # tell "slow but draining" from "wedged".
        self._progress = 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._run_completed_sent = False
        atexit.register(self._emit_run_completed_atexit)

    @property
    def filepath(self) -> Path:
        return self._filepath

    def _run(self) -> None:
        """Background thread: drain the queue per tick, coalesce, write.

        Each tick drains everything available, folds accumulating metric
        events into `metric_batch` frames (see nebo/core/coalesce.py) and
        writes with a single stream flush — instead of one write+flush
        syscall pair per event.
        """
        from nebo.core.coalesce import coalesce
        from nebo.logging.serializers import resolve_media

        stop = False
        while not stop:
            try:
                first = self._queue.get(timeout=self._flush_interval)
            except queue.Empty:
                if not self._running:
                    break
                continue
            batch: list = [first]
            while True:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break

            events: list[dict] = []
            barriers: list[threading.Event] = []
            for item in batch:
                if item is None:
                    stop = True
                elif isinstance(item, tuple) and item[0] == "__barrier__":
                    barriers.append(item[1])
                else:
                    events.append(item)

            try:
                self._write_batch(events, coalesce, resolve_media)
            except Exception:
                # A poison batch must not kill the writer thread — later
                # events still deserve a drain attempt (and close() relies
                # on this thread staying alive to finish the shutdown drain).
                logger.exception("nebo: file transport failed to write a batch")
            finally:
                # Barriers release even on failure; a flush() that waits
                # forever on a failed batch is worse than one that returns.
                for barrier in barriers:
                    barrier.set()

    def _write_batch(self, events: list, coalesce, resolve_media) -> None:
        """Resolve deferred media, coalesce, and write one batch.

        Bumps `_progress` per event as it goes so close() can distinguish a
        slow drain (media encoding) from a wedged one. A None from
        resolve_media means encoding failed (already logged there).
        """
        resolved = []
        for e in events:
            r = resolve_media(e)
            self._progress += 1
            if r is not None:
                resolved.append(r)
        with self._lock:
            for event in coalesce(resolved):
                self._writer.write_entry(event.get("type", "text"), event)
            self._stream.flush()

    def send_event(self, event: dict) -> None:
        if not self._running:
            return
        self._queue.put(event)

    def flush(self, timeout: float = 5.0) -> bool:
        """Barrier: block until everything queued before this call is on disk."""
        if not self._running:
            return True
        barrier = threading.Event()
        self._queue.put(("__barrier__", barrier))
        return barrier.wait(timeout)

    def close(self) -> None:
        """Drain the queue completely, then close the file.

        The backlog is bounded by what the caller logged, but deferred
        media encoding means it can hold many seconds of CPU work at
        shutdown — so the wait is progress-based, not a fixed deadline:
        we only give up after NEBO_SHUTDOWN_TIMEOUT (default 10 s) with
        zero events resolved or written. Closing the stream under a
        still-draining worker (the pre-fix behavior) silently truncated
        the run tail.
        """
        if not self._running:
            return
        self._run_completed_sent = True
        self._running = False
        self._queue.put(None)  # poison pill; the worker drains, then stops
        stall_budget = _shutdown_stall_timeout()
        last_progress = self._progress
        last_change = time.monotonic()
        while self._thread.is_alive():
            self._thread.join(timeout=0.2)
            if self._progress != last_progress:
                last_progress = self._progress
                last_change = time.monotonic()
            elif time.monotonic() - last_change >= stall_budget:
                # Wedged (stuck write / pathological encode): warn and bail
                # WITHOUT closing the stream under the worker — bytes
                # already flushed per tick stay safe, the fd is reclaimed
                # at process exit.
                print(
                    f"nebo: WARNING — file transport made no progress for "
                    f"{stall_budget:.0f}s at shutdown; ~{self._queue.qsize()} "
                    "event(s) may be missing from the run file. "
                    "(NEBO_SHUTDOWN_TIMEOUT tunes this.)",
                    file=sys.stderr, flush=True,
                )
                return
        # Worker exited. Normally the queue is empty; if the thread died
        # early (crash), drain the residue synchronously on this thread.
        residue = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not None and not (
                isinstance(item, tuple) and item[0] == "__barrier__"
            ):
                residue.append(item)
        if residue:
            from nebo.core.coalesce import coalesce
            from nebo.logging.serializers import resolve_media
            try:
                self._write_batch(residue, coalesce, resolve_media)
            except Exception:
                print(
                    f"nebo: WARNING — dropped {len(residue)} event(s) at "
                    "shutdown; the run file writer failed.",
                    file=sys.stderr, flush=True,
                )
        with self._lock:
            self._writer.close()
            self._stream.close()

    def _emit_run_completed_atexit(self) -> None:
        """Drain and close at process exit.

        Always closes (close() is the shutdown barrier — its poison pill
        makes the worker drain everything first). The run_completed
        emission alone is guarded by _run_completed_sent so explicit
        start_run() context-manager exits don't get a duplicate event.
        """
        if not self._running:
            return
        if not self._run_completed_sent:
            self._run_completed_sent = True
            self.send_event({
                "type": "run_completed",
                "data": {"timestamp": time.time()},
            })
        self.close()
