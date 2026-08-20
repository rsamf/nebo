"""SDK client that connects to the nebo daemon server.

In server mode, all events (logs, metrics, node registrations, etc.) are
forwarded to the daemon via HTTP. If the daemon disconnects, the client
falls back to local mode and buffers events for replay on reconnect.

Uses only stdlib (urllib) — no httpx dependency required.
"""

from __future__ import annotations

import atexit
import http.client
import json
import logging
import os
import queue
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Any, Optional

import msgpack


logger = logging.getLogger(__name__)

# Event types that are never dropped by buffer backpressure: they define
# run/graph structure, are tiny, and losing one corrupts the run's shape
# rather than just thinning its data.
STRUCTURAL_TYPES = frozenset({
    "run_start", "run_completed", "loggable_register", "edge",
    "node_executed", "config", "ui_config", "run_config", "description",
    "alert", "error",
    # A pose frame without its model is unrenderable, so the model event
    # is never dropped. body_transform stays droppable — losing frames
    # thins a scene's timeline, exactly like thinning a metric series.
    "body_model",
})

DEFAULT_BUFFER_BUDGET_MB = 128


class DaemonLocalOnlyError(RuntimeError):
    """Connecting to a daemon that refuses network runs.

    A local-only daemon (`nebo serve` with no `--remote*` flag) only ingests
    `.nebo` files from its watched logdir. To accept runs over the network,
    restart it with `--remote [dir]` (persisted) or `--remote-ephemeral`
    (RAM/cache only). Raised at `nb.init()` / first emit so the misconfig
    surfaces immediately, not hours into a run.
    """


def _local_only_message(base_url: str) -> str:
    return (
        f"The nebo daemon at {base_url} is running in local-only mode and does "
        "not accept network runs. Restart it with `nebo serve --remote [dir]` "
        "to persist incoming runs, or `--remote-ephemeral` to accept them "
        "without persistence. (Or log to a file instead: nb.init('.nebo/').)"
    )


@dataclass
class DrainResult:
    """Outcome of a drain attempt.

    `last_error` is None on full success (dropped == 0); on partial or
    full failure it carries `repr(exc)` of the most recent
    `_post_packed` failure.
    """
    sent: int
    dropped: int
    dropped_bytes: int
    last_error: Optional[str]


class NetworkTransport:
    """Client that streams events from the SDK to the daemon server.

    Thread-safe. Uses a background thread for batched HTTP flushing.
    Supports graceful fallback and reconnection.
    """

    # 2 MB cap on a single POST body. Large enough that normal
    # text-event traffic never chunks; small enough that common proxy /
    # WAF body limits don't bite even with base64 image payloads.
    _MAX_CHUNK_BYTES = 2 * 1024 * 1024

    def __init__(
        self,
        host: str = "localhost",
        port: int = 7861,
        run_id: Optional[str] = None,
        flush_interval: float = 0.1,
        base_url: Optional[str] = None,
        api_token: Optional[str] = None,
        shutdown_timeout: float = 10.0,
    ) -> None:
        """Connect to a nebo daemon.

        For a local daemon: pass `host` + `port` (defaults work).
        For a remote daemon (e.g. one running on a Hugging Face Space):
        pass `base_url` (e.g. `https://username-space.hf.space`) and
        optionally `api_token`. When `base_url` is set it takes
        precedence over `host`/`port`. When `api_token` is set, an
        `X-Nebo-Token` header is added to every HTTP request — required
        when the target daemon enforces auth via `NEBO_API_TOKEN`.

        `shutdown_timeout` (default 10 s; env override
        NEBO_SHUTDOWN_TIMEOUT) is the budget the atexit drain has to
        push remaining events to the daemon before warning to stderr.
        """
        self._host = host
        self._port = port
        self._run_id = run_id
        self._flush_interval = flush_interval
        self._api_token = api_token
        if base_url:
            self._base_url = base_url.rstrip("/")
        else:
            self._base_url = f"http://{host}:{port}"

        self._queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._buffer: list[dict[str, Any]] = []
        self._connected = False
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._fallback_buffer: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._run_completed: bool = False
        # Persistent keep-alive connections, ONE PER THREAD: the background
        # flush loop and an explicit flush()/atexit drain can post
        # concurrently, and http.client connections are not thread-safe —
        # sharing one interleaves request/getresponse and every collision
        # costs a dropped connection plus a retry sleep.
        self._conn_local = threading.local()
        # Remote daemons cross the public internet; loopback daemons are
        # fast. Use the longer timeout whenever a token is in play, since
        # that's the remote-target signal.
        self._conn_timeout = 30.0 if api_token else 5.0
        parts = urllib.parse.urlsplit(self._base_url)
        self._conn_scheme = parts.scheme or "http"
        self._conn_host = parts.hostname or "localhost"
        self._conn_port = parts.port
        self._conn_prefix = parts.path.rstrip("/")
        # Backpressure: approximate bytes across queue + buffer + fallback.
        # Over budget, incoming non-structural events are dropped (progress
        # first, at 90%) instead of growing RSS without bound while the
        # daemon is slow or down. Structural events always get through.
        budget_mb = float(
            os.environ.get("NEBO_BUFFER_BUDGET_MB") or DEFAULT_BUFFER_BUDGET_MB
        )
        self._buffer_budget = int(budget_mb * 1024 * 1024)
        self._buffered_bytes = 0
        self._budget_lock = threading.Lock()
        self._dropped_events = 0
        # Policy rejection (daemon in local-only mode). `_fatal` stops the
        # flush loop from retrying forever the way a transient outage would;
        # `_policy_error` carries the message init() raises as
        # DaemonLocalOnlyError. Set at connect (fail-fast) or on a mid-run 409.
        self._fatal = False
        self._policy_error: Optional[str] = None
        # Reconnect pacing for the flush loop (test-overridable).
        self._reconnect_backoff_initial = 1.0
        self._reconnect_backoff_max = 30.0

        env_timeout = os.environ.get("NEBO_SHUTDOWN_TIMEOUT")
        if env_timeout is not None:
            try:
                shutdown_timeout = float(env_timeout)
            except ValueError:
                pass
        self._shutdown_timeout = shutdown_timeout

    def _auth_headers(self) -> dict[str, str]:
        if self._api_token:
            # Use X-Nebo-Token instead of Authorization: Bearer.
            # Google Cloud Frontend's WAF blocks the Authorization
            # bearer pattern (matches Stripe-like leaked-credential
            # detection) AND the X-Nebo-Api-Token header name
            # specifically — but X-Nebo-Token sails through.
            return {"X-Nebo-Token": self._api_token}
        return {}

    def warmup(self, timeout: float = 120.0) -> bool:
        """Block until a fronting router has a daemon ready (best-effort).

        POSTs to `/api/daemon/warmup` to nudge a multi-tenant router
        into bringing up a per-user daemon. Returns True on 200, False
        otherwise. For directly-reachable daemons (local or a Space)
        this endpoint typically 404s and the caller should ignore the
        return value.

        No-op (returns True immediately) when no api_token is set.
        """
        if not self._api_token:
            return True
        url = (
            f"{self._base_url}/api/daemon/warmup"
            f"?t={urllib.request.quote(self._api_token)}"
        )
        req = urllib.request.Request(url, data=b"", method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status == 200
        except Exception:
            return False

    def connect(self) -> bool:
        """Attempt to connect to the daemon server.

        Returns:
            True if connection successful.
        """
        try:
            req = urllib.request.Request(f"{self._base_url}/health", method="GET")
            for k, v in self._auth_headers().items():
                req.add_header(k, v)
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                if resp.status == 200:
                    try:
                        body = json.loads(resp.read().decode("utf-8"))
                    except Exception:
                        body = {}
                    # A local-only daemon won't accept our runs — fail fast
                    # instead of buffering events that will never be accepted.
                    if body.get("mode") == "local":
                        self._policy_error = _local_only_message(self._base_url)
                        self._fatal = True
                        self._connected = False
                        return False
                    self._connected = True
                    self._start_flush_thread()
                    return True
        except Exception:
            pass
        self._connected = False
        return False

    @staticmethod
    def _event_nbytes(event: dict[str, Any]) -> int:
        """Cheap size estimate for buffer accounting (media dominates)."""
        overhead = 200
        data = event.get("data")
        if isinstance(data, (bytes, bytearray)):
            return len(data) + overhead
        nbytes = getattr(data, "nbytes", None)  # PendingMedia / ndarray
        if isinstance(nbytes, int):
            return nbytes + overhead
        message = event.get("message")
        if isinstance(message, str):
            return len(message) + overhead
        return overhead

    def _admit(self, event: dict[str, Any]) -> bool:
        """Charge an event against the buffer budget, or drop it."""
        etype = event.get("type", "")
        size = self._event_nbytes(event)
        with self._budget_lock:
            if etype not in STRUCTURAL_TYPES:
                # Progress is the most expendable stream — shed it before
                # the budget is fully exhausted so real data fits longer.
                limit = (
                    int(self._buffer_budget * 0.9)
                    if etype == "progress" else self._buffer_budget
                )
                if self._buffered_bytes + size > limit:
                    if self._dropped_events == 0:
                        logger.warning(
                            "nebo: transport buffer over budget (%d MB); "
                            "dropping non-structural events until it "
                            "drains. Is the daemon reachable?",
                            self._buffer_budget // (1024 * 1024),
                        )
                    self._dropped_events += 1
                    return False
            self._buffered_bytes += size
        return True

    def _release_bytes(self, events: list[dict[str, Any]]) -> None:
        size = sum(self._event_nbytes(e) for e in events)
        with self._budget_lock:
            self._buffered_bytes = max(0, self._buffered_bytes - size)

    def _charge_bytes(self, events: list[dict[str, Any]]) -> None:
        """Re-charge already-admitted events (failed-chunk re-buffering)."""
        size = sum(self._event_nbytes(e) for e in events)
        with self._budget_lock:
            self._buffered_bytes += size

    def send_event(self, event: dict[str, Any]) -> None:
        """Queue an event to be sent to the daemon.

        If disconnected, events are buffered for replay on reconnect.
        Non-structural events are dropped once the buffer budget
        (NEBO_BUFFER_BUDGET_MB, default 128) is exhausted.

        Args:
            event: The event dictionary to send.
        """
        if not self._admit(event):
            return
        if self._connected:
            self._queue.put(event)
        else:
            with self._lock:
                self._fallback_buffer.append(event)

    def send_events(self, events: list[dict[str, Any]]) -> None:
        """Queue multiple events."""
        for event in events:
            self.send_event(event)

    def is_connected(self) -> bool:
        """Check if the client is connected to the daemon."""
        return self._connected

    def disconnect(self) -> None:
        """Disconnect from the daemon and flush remaining events."""
        # Send run_completed event before flushing (guard against double send)
        if self._connected and not self._run_completed:
            self._queue.put({
                "type": "run_completed",
                "data": {"timestamp": time.time()},
            })
        self._running = False
        if self._thread and self._thread.is_alive():
            # Long enough to cover one in-flight POST (the flush loop's
            # worst-case block); a shorter join races the final drain
            # against a still-live flush loop on self._buffer.
            self._thread.join(timeout=self._conn_timeout + 1.0)
        self._flush_remaining()
        self._connected = False
        self._drop_connection()

    def _start_flush_thread(self) -> None:
        """Start the background flush thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._thread.start()
        atexit.register(self.disconnect)

    def _flush_loop(self) -> None:
        """Background loop: batch + send while connected; keep retrying
        the connection (1 s → 30 s backoff) while the daemon is away —
        forever, not for a fixed attempt count. Fallback-buffered events
        replay on reconnect."""
        last_flush = time.monotonic()
        reconnect_at = 0.0
        backoff = self._reconnect_backoff_initial

        while self._running:
            if self._fatal:
                self._on_fatal()
                return
            if not self._connected:
                now = time.monotonic()
                if now >= reconnect_at:
                    if self.try_reconnect():
                        backoff = self._reconnect_backoff_initial
                    else:
                        reconnect_at = now + backoff
                        backoff = min(backoff * 2, self._reconnect_backoff_max)
                time.sleep(min(self._flush_interval, 0.1))
                continue

            try:
                event = self._queue.get(timeout=self._flush_interval)
                self._buffer.append(event)
            except queue.Empty:
                pass

            now = time.monotonic()

            if (now - last_flush) >= self._flush_interval and self._buffer:
                success = self._do_flush()
                last_flush = now
                if not success:
                    self._handle_disconnect()

    def _prepare_packed(
        self, batch: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[bytes], list[dict[str, Any]]]:
        """Coalesce, resolve deferred media, and pack each event ONCE.

        Returns (events, packed, bad) with events/packed parallel. Events
        msgpack can't encode (most commonly a `set` value) are quarantined
        into `bad` so one poison event can't wedge every later flush —
        callers must drop them, never re-buffer. Media bytes ride natively
        in the msgpack body: no base64 anywhere on this path.
        """
        from nebo.core.coalesce import coalesce
        from nebo.logging.serializers import resolve_media

        events: list[dict[str, Any]] = []
        packed: list[bytes] = []
        bad: list[dict[str, Any]] = []
        for event in coalesce(batch):
            resolved = resolve_media(event)
            if resolved is None:
                continue  # encoding failed; already logged
            try:
                data = msgpack.packb(resolved, use_bin_type=True)
            except (TypeError, ValueError, OverflowError):
                bad.append(resolved)
                continue
            events.append(resolved)
            packed.append(data)
        return events, packed, bad

    @staticmethod
    def _chunk_packed(
        events: list[dict[str, Any]],
        packed: list[bytes],
        max_bytes: int,
    ) -> list[tuple[list[dict[str, Any]], list[bytes]]]:
        """Split parallel (events, packed) into chunks <= max_bytes each.

        Sizes come from the already-encoded bytes — no re-serialization. A
        single oversized event becomes its own chunk (never dropped; the
        network may still accept it).
        """
        chunks: list[tuple[list[dict[str, Any]], list[bytes]]] = []
        cur_events: list[dict[str, Any]] = []
        cur_packed: list[bytes] = []
        size = 0
        for event, data in zip(events, packed):
            if cur_events and size + len(data) > max_bytes:
                chunks.append((cur_events, cur_packed))
                cur_events, cur_packed, size = [], [], 0
            cur_events.append(event)
            cur_packed.append(data)
            size += len(data)
        if cur_events:
            chunks.append((cur_events, cur_packed))
        return chunks

    def _warn_unserializable(self, bad: list[dict[str, Any]]) -> None:
        """Log a warning summarising dropped un-serializable events."""
        if not bad:
            return
        sample = bad[0]
        if isinstance(sample, dict):
            event_type = sample.get("type", "<unknown>")
            keys = list(sample.keys())
        else:
            event_type = type(sample).__name__
            keys = []
        logger.warning(
            "nebo: dropping %d un-serializable event(s) — most often a "
            "`set` value (e.g. `@nb.fn(ui={\"a\", \"b\"})` instead of a "
            "dict). First offender type=%r keys=%s.",
            len(bad), event_type, keys,
        )

    def _drain_with_retry(self, deadline: float) -> DrainResult:
        """Drain queue + buffer to the daemon, retrying until deadline.

        On entry, anything in self._buffer or self._queue is fair game.
        On exit, any unsent events remain in self._buffer for the
        caller to inspect (or for another call to retry).
        """
        sent = 0
        last_error: Optional[str] = None

        while True:
            self._drain_queue_into_buffer()
            if not self._buffer:
                return DrainResult(
                    sent=sent, dropped=0, dropped_bytes=0, last_error=None
                )

            pending = self._buffer[:]
            self._buffer.clear()
            self._release_bytes(pending)
            events, packed, bad = self._prepare_packed(pending)
            if bad:
                self._warn_unserializable(bad)
            if not events:
                continue

            chunks = self._chunk_packed(events, packed, self._MAX_CHUNK_BYTES)

            failed_at: Optional[int] = None
            for i, (chunk_events, chunk_packed) in enumerate(chunks):
                ok, exc = self._post_packed(chunk_events, chunk_packed)
                if ok:
                    sent += len(chunk_events)
                else:
                    last_error = repr(exc)
                    failed_at = i
                    break

            if failed_at is not None:
                # Restore the failed chunk + everything after it. Restored
                # events are post-coalesce shapes — legal wire events.
                rest = chunks[failed_at:]
                for chunk_events, _ in rest:
                    self._buffer.extend(chunk_events)
                    self._charge_bytes(chunk_events)

                now = time.monotonic()
                if now >= deadline:
                    dropped_bytes = sum(
                        len(p) for _, chunk_packed in rest for p in chunk_packed
                    )
                    return DrainResult(
                        sent=sent,
                        dropped=len(self._buffer),
                        dropped_bytes=dropped_bytes,
                        last_error=last_error,
                    )
                time.sleep(min(0.2, deadline - now))
            # If failed_at is None, all chunks succeeded; loop again to
            # catch anything queued during the POSTs.

    def _connection(self) -> http.client.HTTPConnection:
        """This thread's persistent keep-alive connection."""
        conn = getattr(self._conn_local, "conn", None)
        if conn is None:
            conn_cls = (
                http.client.HTTPSConnection
                if self._conn_scheme == "https"
                else http.client.HTTPConnection
            )
            conn = conn_cls(
                self._conn_host, self._conn_port, timeout=self._conn_timeout
            )
            self._conn_local.conn = conn
        return conn

    def _drop_connection(self) -> None:
        conn = getattr(self._conn_local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._conn_local.conn = None

    def _post_packed(
        self, events: list[dict[str, Any]], packed: list[bytes],
    ) -> tuple[bool, Optional[Exception]]:
        """POST pre-packed events as one concatenated-msgpack body.

        The body is the concatenation of individually-packed event maps
        (the daemon splits them with msgpack.Unpacker), so no bytes are
        re-encoded here. Returns (True, None) on HTTP 200; (False, exc)
        otherwise — returned, not raised, so callers keep linear flow.
        """
        if not events:
            return True, None
        path = f"{self._conn_prefix}/events"
        if self._run_id:
            path += f"?run_id={urllib.parse.quote(self._run_id)}"
        headers = {"Content-Type": "application/msgpack"}
        headers.update(self._auth_headers())
        try:
            conn = self._connection()
            conn.request("POST", path, body=b"".join(packed), headers=headers)
            resp = conn.getresponse()
            payload = resp.read()  # drain so the connection can be reused
            if resp.status == 200:
                return True, None
            # The daemon restarted into local-only mode mid-run: fatal, not a
            # transient outage. Stop retrying rather than buffer forever.
            if resp.status == 409 and b"daemon_local_only" in payload:
                self._policy_error = _local_only_message(self._base_url)
                self._fatal = True
            return False, RuntimeError(f"HTTP {resp.status}")
        except Exception as exc:
            self._drop_connection()
            return False, exc

    def _do_flush(self) -> bool:
        """Send buffered events to the daemon. Returns True on success."""
        if not self._buffer:
            return True

        batch = self._buffer[:]
        self._buffer.clear()
        self._release_bytes(batch)
        events, packed, bad = self._prepare_packed(batch)

        # Drop un-serializable events before posting. Re-buffering them
        # would re-poison every subsequent flush — see issue.md.
        if bad:
            self._warn_unserializable(bad)
        if not events:
            return True

        chunks = self._chunk_packed(events, packed, self._MAX_CHUNK_BYTES)
        for i, (chunk_events, chunk_packed) in enumerate(chunks):
            ok, _ = self._post_packed(chunk_events, chunk_packed)
            if not ok:
                # Put events back for retry by the next periodic tick.
                restored = [e for evs, _ in chunks[i:] for e in evs]
                self._buffer = restored + self._buffer
                self._charge_bytes(restored)
                return False
        return True

    def _drain_queue_into_buffer(self) -> None:
        """Move every event currently in self._queue into self._buffer."""
        while not self._queue.empty():
            try:
                event = self._queue.get_nowait()
                self._buffer.append(event)
            except queue.Empty:
                break

    def _flush_remaining(self) -> None:
        """Drain queue + buffer at shutdown with a bounded retry budget.

        On any unsent residue, prints a WARNING to stderr (not via the
        `nebo` logger or `warnings.warn` — atexit runs after most
        logging handlers have shut down and warnings filters may have
        been reconfigured by user code by this point; bare stderr is
        the path most likely to actually reach the terminal).
        """
        # Events parked in the fallback buffer (queued while disconnected)
        # get a real send attempt too — the daemon may be reachable again
        # by exit time, and silently discarding them was the old behavior.
        # They were charged at admit time, so accounting stays consistent
        # when _drain_with_retry releases them on pull.
        with self._lock:
            fallback = self._fallback_buffer[:]
            self._fallback_buffer.clear()
        if fallback:
            self._buffer = fallback + self._buffer

        deadline = time.monotonic() + self._shutdown_timeout
        result = self._drain_with_retry(deadline)
        if self._dropped_events > 0:
            print(
                f"nebo: WARNING — {self._dropped_events} event(s) were "
                "dropped during the run because the transport buffer "
                "exceeded its budget (NEBO_BUFFER_BUDGET_MB).",
                file=sys.stderr, flush=True,
            )
        if result.dropped > 0:
            kb = result.dropped_bytes / 1024
            msg = (
                f"nebo: WARNING — dropped {result.dropped} event(s) "
                f"(~{kb:.0f} KB) at shutdown after "
                f"{self._shutdown_timeout}s. "
                f"Last error: {result.last_error}. "
                "Some logs may not appear in the UI."
            )
            print(msg, file=sys.stderr, flush=True)

    def _on_fatal(self) -> None:
        """Tear down after a fatal policy rejection: log once, drop buffers,
        stop. Unlike a transient outage, retrying can never succeed."""
        self._running = False
        self._connected = False
        self._drop_connection()
        with self._lock:
            self._fallback_buffer.clear()
        self._buffer.clear()
        logger.error("nebo: %s", self._policy_error or "transport disabled")

    def _handle_disconnect(self) -> None:
        """Mark the daemon as away; the flush loop owns reconnection.

        Events queued while disconnected land in the fallback buffer
        (still budget-bounded) and replay once `try_reconnect` succeeds.
        """
        self._connected = False
        self._drop_connection()
        with self._lock:
            self._fallback_buffer.extend(self._buffer)
            self._buffer.clear()

    def flush(self, timeout: float = 5.0) -> bool:
        """Force-flush queued events to the daemon, blocking until done
        or `timeout` seconds elapse.

        Useful for fencing a logging-heavy section before something
        irreversible (saving artifacts, sending an email, etc.) so the
        UI shows everything that was logged before that point.

        Returns True if everything was sent; False if any events
        remain un-flushed when the deadline expired (those events
        stay in self._buffer).
        """
        deadline = time.monotonic() + max(0.0, timeout)
        result = self._drain_with_retry(deadline)
        return result.dropped == 0

    def get(self, path: str) -> Optional[dict[str, Any]]:
        """Send a GET request to the daemon and return the JSON response.

        Args:
            path: URL path (e.g. "/runs/run_1/loggables/foo").

        Returns:
            Parsed JSON dict, or None on error.
        """
        try:
            url = f"{self._base_url}{path}"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                if resp.status == 200:
                    return json.loads(resp.read().decode("utf-8"))
        except Exception:
            pass
        return None

    def try_reconnect(self) -> bool:
        """Manually attempt reconnection."""
        if self._fatal:
            return False
        if self._connected:
            return True
        connected = self.connect()
        if connected:
            with self._lock:
                replay = self._fallback_buffer[:]
                self._fallback_buffer.clear()
            for event in replay:
                self._queue.put(event)
        return connected
