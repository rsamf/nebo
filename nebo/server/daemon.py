"""Persistent daemon server for nebo.

The daemon outlives individual pipeline runs, retaining logs, errors, and DAG
state across crashes and restarts. AI agents connect via MCP to the same server.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import Request, Response, WebSocket, WebSocketDisconnect
from nebo.server.cache import (
    DEFAULT_MEDIA_LRU_MB,
    MediaLRU,
    RunCache,
    media_id_for,
)
from nebo.server.protocol import MessageType, decode_batch

logger = logging.getLogger(__name__)


@dataclass
class LogEntry:
    """A single log entry."""
    timestamp: float
    node: Optional[str]
    message: str
    name: str = "text"
    level: str = "info"
    type: str = "log"
    step: Optional[int] = None
    extra: dict = field(default_factory=dict)


@dataclass
class LoggableState:
    """State for a single loggable within a run.

    `kind` is one of:
      - "node": a DAG node produced by @nb.fn()
      - "global": the implicit __global__ loggable for user logs outside any node
      - "agent": the implicit __agent__ loggable for entries authored over MCP
    """
    loggable_id: str
    kind: str = "node"
    func_name: str = ""
    docstring: Optional[str] = None
    # True when this entry was created on-demand by a metric/image/audio event
    # that arrived before its loggable_register (e.g. after a daemon restart
    # mid-run, or out-of-order batches). A subsequent loggable_register
    # upgrades the placeholder in place rather than being dropped as a dupe.
    auto_seeded: bool = False
    exec_count: int = 0
    is_source: bool = True
    params: dict = field(default_factory=dict)
    logs: list[dict] = field(default_factory=list)
    metrics: dict[str, list] = field(default_factory=dict)
    images: list[dict] = field(default_factory=list)
    audio: list[dict] = field(default_factory=list)
    progress: Optional[dict] = None
    group: Optional[str] = None  # Class name if this node is a method of a decorated class
    ui_hints: Optional[dict] = None  # Per-node UI display hints from @nb.fn(ui=...)


@dataclass
class Run:
    """Represents a single pipeline run managed by the daemon."""
    id: str
    script_path: str
    args: list[str] = field(default_factory=list)
    started_at: Optional[datetime] = None
    loggables: dict[str, "LoggableState"] = field(default_factory=dict)
    edges: list[dict[str, str]] = field(default_factory=list)
    _edge_set: set[tuple[str, str]] = field(default_factory=set, repr=False)
    logs: list[LogEntry] = field(default_factory=list)
    metrics: dict[str, list] = field(default_factory=dict)
    source_hash: Optional[str] = None
    workflow_description: Optional[str] = None
    config: dict = field(default_factory=dict)
    ui_config: Optional[dict] = None
    significant_events: list[dict] = field(default_factory=list)
    alerts: list[dict] = field(default_factory=list)
    run_name: Optional[str] = None
    run_config: dict = field(default_factory=dict)
    # Cache-era bookkeeping. `ram_complete` is the read-routing flag: True
    # means every entry of this run is in RAM (serve reads from RAM); False
    # means only ingest-state is resident (serve reads from the SQL cache).
    last_event_at: float = 0.0
    resident_points: int = 0
    ram_complete: bool = True
    source: str = "network"
    latest_step: Optional[int] = None

    def get_graph(self) -> dict:
        """Return the DAG as a serializable dict.

        Only node-kind loggables appear under the "nodes" key; the implicit
        global loggable (kind="global") is excluded from the DAG view.
        Edges are filtered to those whose endpoints are both node-kind, so
        the graph is self-consistent even if a stray edge referenced the
        global.
        """
        non_node_ids = {
            lid for lid, lg in self.loggables.items() if lg.kind != "node"
        }
        return {
            "nodes": {
                lid: {
                    "name": lg.loggable_id,
                    "func_name": lg.func_name,
                    "docstring": lg.docstring,
                    "exec_count": lg.exec_count,
                    "is_source": lg.is_source,
                    "params": lg.params,
                    "progress": lg.progress,
                    "group": lg.group,
                    "ui_hints": lg.ui_hints,
                }
                for lid, lg in self.loggables.items()
                if lg.kind == "node"
            },
            "edges": [
                e for e in self.edges
                if e.get("source") not in non_node_ids
                and e.get("target") not in non_node_ids
            ],
            "workflow_description": self.workflow_description,
            "ui_config": self.ui_config,
            "run_config": self.run_config,
        }

    def get_summary(self) -> dict:
        """Return a concise run summary."""
        # Flat catalog of available metric names per loggable. Lets agents
        # discover what's loggable without iterating every loggable card.
        metrics_index: dict[str, list[str]] = {
            lid: sorted(loggable.metrics.keys())
            for lid, loggable in self.loggables.items()
            if loggable.metrics
        }
        # `latest_step` is maintained as a counter at ingest time (see
        # _process_event) so it stays correct even for runs whose entry
        # lists were demoted out of RAM.
        metric_series_count = sum(
            len(loggable.metrics) for loggable in self.loggables.values()
        )
        return {
            "id": self.id,
            "script_path": self.script_path,
            "args": self.args,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_event_at": self.last_event_at or None,
            "node_count": sum(1 for l in self.loggables.values() if l.kind == "node"),
            "edge_count": len(self.edges),
            "log_count": len(self.logs),
            "run_name": self.run_name,
            "run_config": self.run_config,
            "metrics_index": metrics_index,
            "metric_series_count": metric_series_count,
            "latest_step": self.latest_step,
        }


# Comparison operators an alert-rule condition may use.
ALERT_CONDITION_OPS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}

# Reserved pseudo-metric for heartbeat rules: `last_event > 60` fires when a
# run has been idle (now - last_event_at) more than 60 seconds. Nebo has no
# run status or ended_at, so an idle heartbeat is *the* completion signal.
# A real metric with this name never triggers such a rule (and vice versa).
HEARTBEAT_METRIC = "last_event"

# Cadence of the always-on heartbeat evaluator task (module-level so tests
# can monkeypatch it down).
HEARTBEAT_TICK_S = 1.0

_ALERT_LEVEL_NAMES = {10: "DEBUG", 20: "INFO", 30: "WARN", 40: "ERROR"}

# Returned (HTTP 409) when a network client tries to create a run on a
# local-only daemon. The copy names both flags so the fix is self-evident.
_LOCAL_ONLY_ERROR = {
    "error": "daemon_local_only",
    "detail": (
        "This daemon does not accept network runs. Restart it with "
        "`nebo serve --remote [dir]` to persist incoming runs, or "
        "`--remote-ephemeral` to accept them without persistence."
    ),
}


def _sniff_mime(data: bytes) -> str:
    """Content-type from magic bytes — nebo media is PNG or WAV, but agents
    can push arbitrary files via the MCP write tools."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def validate_alert_condition(condition: Any) -> Optional[str]:
    """Return an error message for a malformed rule condition, else None."""
    if not isinstance(condition, dict):
        return "condition must be an object"
    if not condition.get("metric"):
        return "condition.metric is required"
    if condition.get("op") not in ALERT_CONDITION_OPS:
        ops = ", ".join(ALERT_CONDITION_OPS)
        return f"condition.op must be one of: {ops}"
    if not isinstance(condition.get("value"), (int, float)) or isinstance(
        condition.get("value"), bool
    ):
        return "condition.value must be a number"
    if condition["metric"] == HEARTBEAT_METRIC:
        # Idle-time rules: `<`/`<=`/`==`/`!=` would fire instantly or never.
        if condition["op"] not in (">", ">="):
            return "last_event rules fire on idle seconds and only support the > and >= operators"
        if condition["value"] < 0:
            return "last_event threshold must be a non-negative number of seconds"
        if condition.get("loggable_id"):
            return "loggable_id does not apply to last_event rules (idleness is run-wide)"
    return None


def format_alert_condition(condition: dict) -> str:
    """Human/agent-readable display string for a rule condition."""
    prefix = f"{condition['loggable_id']}:" if condition.get("loggable_id") else ""
    value = condition["value"]
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{prefix}{condition['metric']} {condition['op']} {value}"


class _WsClient:
    """A /stream subscriber with its own bounded outbound queue.

    Ingest serializes each batch once and enqueues it per client
    (`put_nowait`, drop-oldest at capacity); a per-client sender task
    pumps the queue into the socket. A slow browser tab therefore skips
    old batches instead of backpressuring the SDK's POST — ingest never
    awaits a browser.
    """

    __slots__ = ("ws", "queue", "task", "dropped")

    def __init__(self, ws: Any, maxsize: int = 256) -> None:
        self.ws = ws
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=maxsize)
        self.task: Optional[asyncio.Task] = None
        self.dropped = 0

    def enqueue(self, message: str) -> None:
        while True:
            try:
                self.queue.put_nowait(message)
                return
            except asyncio.QueueFull:
                try:
                    self.queue.get_nowait()
                    self.dropped += 1
                except asyncio.QueueEmpty:
                    pass

    async def sender(self) -> None:
        while True:
            message = await self.queue.get()
            await self.ws.send_text(message)


class DaemonState:
    """Central state holder for the daemon server.

    Manages all runs, retaining state across pipeline lifecycles.
    """

    def __init__(self, cache: Optional[RunCache] = None) -> None:
        self.runs: dict[str, Run] = {}
        # Alert rules created over the API (CLI / MCP). In-memory, like
        # every other piece of daemon state. Keyed by rule id.
        self.alert_rules: dict[str, dict] = {}
        self.active_run_id: Optional[str] = None
        self._ws_clients: list[Any] = []
        self._lock = asyncio.Lock()
        self._event_notify: asyncio.Condition = asyncio.Condition()
        # Write-behind SQLite cache. None (direct construction, tests,
        # --no-cache) preserves the pure-RAM behavior: media falls back to
        # a plain dict and the eviction janitor is disabled.
        self.cache = cache
        self.media_lru = (
            cache.media_lru if cache is not None
            else MediaLRU(DEFAULT_MEDIA_LRU_MB * 1024 * 1024)
        )
        self._media_fallback: dict[str, bytes] = {}
        # Persistence mode. A bare DaemonState() (tests, embedders) defaults
        # to "remote-ephemeral" so direct network ingest works without flags;
        # the real server (`create_daemon_app(state=None)`) starts "local" and
        # env resolves the rest. "local" rejects network runs; "remote" writes
        # them to `_remote_dir`; "remote-ephemeral" accepts without persisting.
        self.mode: str = "remote-ephemeral"
        self._remote_dir: Optional[Path] = None
        self._logdir: Optional[Path] = None
        # Set by the lifespan when the directory watcher starts. Read-access
        # deep-ingest of shallow (header-only) runs delegates to it.
        self._watcher: Optional[Any] = None
        # Run ids whose watcher events were dropped as aliases of a
        # network-owned run — remembered only to warn once per run.
        self._alias_dropped: set[str] = set()
        # Run tree (groups + placements over meta/tree.json). None on a daemon
        # with no workspace root (a bare DaemonState in tests). `_tree_dirty`
        # is set when an ingest seeds a group, so ingest_events broadcasts a
        # tree_updated after the batch.
        self.tree: Optional[Any] = None
        self._tree_dirty: bool = False
        # RAM budget for resident point entries (metric points + log lines),
        # enforced by the janitor. Converted from MB at BYTES_PER_POINT.
        from nebo.server.cache import BYTES_PER_POINT, DEFAULT_RAM_BUDGET_MB
        self.ram_budget_points = (
            DEFAULT_RAM_BUDGET_MB * 1024 * 1024
        ) // BYTES_PER_POINT

    def _cache_put(self, op: tuple) -> None:
        if self.cache is not None:
            self.cache.enqueue(op)

    def media_bytes(self, run_id: str, media_id: str) -> Optional[bytes]:
        """Resolve media bytes: LRU -> no-cache fallback dict -> SQL cache."""
        data = self.media_lru.get(media_id)
        if data is not None:
            return data
        data = self._media_fallback.get(media_id)
        if data is not None:
            return data
        if self.cache is not None:
            return self.cache.get_media(media_id)
        return None

    # -- eviction janitor ------------------------------------------------
    #
    # RAM is a bounded working set; the SQL cache holds everything. There
    # are no run states (a run is never known to be "done"), so liveness is
    # approximated by recency of the last observed event. Rules, in order:
    #   1. idle > EVICT_IDLE_S           -> evict (a run that resumes just
    #                                       rehydrates cheaply from the cache)
    #   2. resident points over budget   -> evict runs idlest-first until
    #                                       under budget
    #   3. a lone recently-active run    -> demote (drop read-state, keep
    #      alone over budget                ingest-state; reads switch to
    #                                       SQL) instead of evicting it
    # A no-op without a cache — eviction would otherwise lose data.

    EVICT_IDLE_S = 1800.0

    def _demotable(self, run: "Run", now: float) -> bool:
        """A lone oversized run still receiving events: keep its ingest-state
        (type locks, counters, edge dedup) resident so it doesn't thrash
        rehydrate, dropping only the point history."""
        return (
            run.ram_complete
            and (now - (run.last_event_at or 0.0)) < self.EVICT_IDLE_S
            and run.resident_points > self.ram_budget_points
        )

    def janitor_pass(self, *, now: Optional[float] = None) -> dict:
        result: dict[str, list[str]] = {"evicted": [], "demoted": []}
        if self.cache is None:
            return result
        if now is None:
            now = time.time()

        # 1. Idle eviction.
        for rid, run in list(self.runs.items()):
            if (now - (run.last_event_at or 0.0)) > self.EVICT_IDLE_S:
                self._evict_run(rid)
                result["evicted"].append(rid)

        # 2. Budget eviction: idlest (oldest last_event_at) first. A lone
        #    recently-active run that alone exceeds budget is demoted below,
        #    not evicted, so skip it here.
        total = sum(r.resident_points for r in self.runs.values())
        if total > self.ram_budget_points:
            for run in sorted(self.runs.values(), key=lambda r: r.last_event_at):
                if total <= self.ram_budget_points:
                    break
                if self._demotable(run, now):
                    continue
                total -= run.resident_points
                self._evict_run(run.id)
                result["evicted"].append(run.id)

        # 3. Demote any lone oversized recently-active run.
        for rid, run in list(self.runs.items()):
            if self._demotable(run, now):
                self._demote_run(rid)
                result["demoted"].append(rid)

        # Keep SQL "last active" honest for demoted/rehydrated runs that read
        # from the cache while still ingesting (their RAM value runs ahead).
        for rid, run in self.runs.items():
            if not run.ram_complete:
                self._cache_put(
                    ("run_upsert", rid, {"last_event_at": run.last_event_at})
                )

        return result

    def _evict_run(self, run_id: str) -> None:
        """Drop a run from RAM entirely; its state lives in the cache."""
        run = self.runs.get(run_id)
        if run is not None:
            # Persist final recency before the barrier so the run's
            # cache-served summary shows the right "last active".
            self._cache_put(
                ("run_upsert", run_id, {"last_event_at": run.last_event_at})
            )
        self.cache.flush()  # barrier: SQL must be complete before the drop
        self.runs.pop(run_id, None)
        if self.active_run_id == run_id:
            self.active_run_id = None

    def _demote_run(self, run_id: str) -> None:
        """Drop a live run's read-state; keep ingest-state. One-way."""
        run = self.runs.get(run_id)
        if run is None:
            return
        self._cache_put(
            ("run_upsert", run_id, {"last_event_at": run.last_event_at})
        )
        self.cache.flush()
        for lg in run.loggables.values():
            for series in lg.metrics.values():
                series["entries"] = []
            lg.logs = []
            lg.images = []
            lg.audio = []
        run.logs = []
        run.resident_points = 0
        run.ram_complete = False

    # -- read accessors -------------------------------------------------
    #
    # Routing rule: a run serves reads from RAM only while it is resident
    # with ram_complete=True (never evicted or demoted). Everything else —
    # evicted runs, demoted live runs — reads from the SQL cache, where
    # the full history lives. Endpoint handlers call these and never touch
    # `state.runs` directly for reads.

    def _resident(self, run_id: str) -> Optional[Run]:
        run = self.runs.get(run_id)
        if run is not None and run.ram_complete:
            return run
        return None

    def run_summary(self, run_id: str) -> Optional[dict]:
        run = self._resident(run_id)
        if run is not None:
            return run.get_summary()
        if self.cache is not None:
            return self.cache.get_summary(run_id)
        run = self.runs.get(run_id)
        return run.get_summary() if run is not None else None

    def all_summaries(self) -> list[dict]:
        out: dict[str, dict] = {}
        if self.cache is not None:
            for s in self.cache.list_summaries():
                out[s["id"]] = s
        for rid, run in self.runs.items():
            if run.ram_complete or rid not in out:
                out[rid] = run.get_summary()
        return list(out.values())

    def run_graph(self, run_id: str) -> Optional[dict]:
        run = self._resident(run_id)
        if run is not None:
            return run.get_graph()
        if self.cache is not None:
            return self.cache.get_graph(run_id)
        run = self.runs.get(run_id)
        return run.get_graph() if run is not None else None

    def run_logs(
        self, run_id: str, loggable_id: Optional[str] = None, limit: int = 100,
    ) -> Optional[list[dict]]:
        run = self._resident(run_id)
        if run is not None:
            logs = run.logs
            if loggable_id:
                logs = [l for l in logs if l.node == loggable_id]
            return [
                {
                    "timestamp": l.timestamp,
                    "loggable_id": l.node,
                    "name": l.name,
                    "message": l.message,
                    "level": l.level,
                    "step": l.step,
                }
                for l in logs[-limit:]
            ]
        if self.cache is not None and self.cache.has_run(run_id):
            return self.cache.get_logs(run_id, loggable_id=loggable_id, limit=limit)
        return None

    def run_metrics(self, run_id: str) -> Optional[dict]:
        run = self._resident(run_id)
        if run is not None:
            return {
                lid: l.metrics for lid, l in run.loggables.items() if l.metrics
            }
        if self.cache is not None and self.cache.has_run(run_id):
            return self.cache.get_metrics(run_id)
        return None

    def run_loggable(self, run_id: str, loggable_id: str) -> Optional[dict]:
        run = self._resident(run_id)
        if run is not None:
            if loggable_id not in run.loggables:
                return None
            lg = run.loggables[loggable_id]
            return {
                "loggable_id": lg.loggable_id,
                "kind": lg.kind,
                "func_name": lg.func_name,
                "docstring": lg.docstring,
                "exec_count": lg.exec_count,
                "is_source": lg.is_source,
                "params": lg.params,
                # Normalized to the /logs entry shape (raw wire events also
                # carry "type"); keeps RAM and SQL reads byte-identical.
                "recent_logs": [
                    {
                        "timestamp": e.get("timestamp"),
                        "loggable_id": loggable_id,
                        "name": e.get("name") or "text",
                        "message": e.get("message", ""),
                        "level": e.get("level", "info"),
                        "step": e.get("step"),
                    }
                    for e in lg.logs[-20:]
                ],
                "metrics": lg.metrics,
                "progress": lg.progress,
            }
        if self.cache is not None and self.cache.has_run(run_id):
            return self.cache.get_loggable(run_id, loggable_id)
        return None

    def run_media_listing(self, run_id: str, kind: str) -> Optional[dict]:
        run = self._resident(run_id)
        if run is not None:
            out: dict[str, list] = {}
            for lid, l in run.loggables.items():
                items = l.images if kind == "image" else l.audio
                if not items:
                    continue
                if kind == "image":
                    out[lid] = [
                        {
                            "loggable_id": lid,
                            "media_id": m.get("media_id", ""),
                            "name": m.get("name", ""),
                            "step": m.get("step"),
                            "timestamp": m.get("timestamp", 0),
                            "labels": m.get("labels"),
                        }
                        for m in items
                    ]
                else:
                    out[lid] = [
                        {
                            "loggable_id": lid,
                            "media_id": m.get("media_id", ""),
                            "name": m.get("name", ""),
                            "sr": m.get("sr", 16000),
                            "step": m.get("step"),
                            "timestamp": m.get("timestamp", 0),
                        }
                        for m in items
                    ]
            return out
        if self.cache is not None and self.cache.has_run(run_id):
            return self.cache.list_media(run_id, kind)
        return None

    def run_alerts(self, run_id: str) -> Optional[list[dict]]:
        run = self._resident(run_id)
        if run is not None:
            return run.alerts
        if self.cache is not None and self.cache.has_run(run_id):
            return self.cache.get_alerts(run_id)
        return None

    def run_significant_events(self, run_id: str) -> Optional[list[dict]]:
        run = self._resident(run_id)
        if run is not None:
            return run.significant_events
        if self.cache is not None and self.cache.has_run(run_id):
            return self.cache.get_significant_events(run_id)
        return None

    def known_run_ids(self) -> list[str]:
        ids = list(self.runs)
        if self.cache is not None:
            seen = set(ids)
            ids.extend(r for r in self.cache.run_ids() if r not in seen)
        return ids

    def has_run_anywhere(self, run_id: str) -> bool:
        if run_id in self.runs:
            return True
        return self.cache is not None and self.cache.has_run(run_id)

    def _tree_payload(self) -> dict:
        """The GET /tree / tree_updated body, filtered to known runs."""
        if self.tree is None:
            return {"groups": {}, "runs": {}}
        return self.tree.to_payload(set(self.known_run_ids()))

    def _enqueue_tree_update(self) -> None:
        """Broadcast the full tree to WS subscribers (idempotent snapshot).
        Sync — enqueue never blocks — so both async endpoints and the ingest
        seed path can call it."""
        if not self._ws_clients:
            return
        msg = json.dumps({"type": "tree_updated", "data": self._tree_payload()})
        for client in self._ws_clients[:]:
            client.enqueue(msg)

    async def ensure_deep(self, run_id: str) -> None:
        """Deep-ingest a shallow (header-only) run before a detail read.

        No-op unless a directory watcher is running and the run is still
        shallow. The watcher owns the file read + per-run locking so two
        concurrent requests don't double-ingest. Cheap after the first call.
        """
        watcher = self._watcher
        if watcher is not None:
            await watcher.ensure_deep(run_id)

    def set_recency(self, run_id: str, ts: Optional[float]) -> None:
        """Override a run's last_event_at (e.g. a shallow run's file mtime,
        which is a better "last active" than the header-registration time)."""
        if not ts:
            return
        run = self.runs.get(run_id)
        if run is not None:
            run.last_event_at = ts
        self._cache_put(("run_upsert", run_id, {"last_event_at": ts}))

    def local_mode_rejects(
        self, events: list[dict], run_id: Optional[str],
    ) -> bool:
        """True when a network batch would CREATE a run on a local-only daemon.

        Local mode accepts annotations of runs it already knows (discovered by
        the directory watcher) but refuses to materialize *new* runs over the
        network — those belong to a `--remote`/`--remote-ephemeral` daemon. A
        `run_start`, or any event for an unknown run_id, would create one.
        """
        if self.mode != "local":
            return False
        for e in events:
            if e.get("type") == "run_start":
                return True
        rid = run_id or self.active_run_id
        return rid is None or not self.has_run_anywhere(rid)

    def create_run(
        self,
        script_path: str,
        args: list[str] | None = None,
        run_id: str | None = None,
        source: str = "network",
    ) -> Run:
        """Create a new run entry."""
        if run_id is None:
            run_id = f"run_{int(time.time())}_{len(self.runs)}"

        source_hash = None
        path = Path(script_path)
        if path.exists():
            source_hash = hashlib.sha256(path.read_bytes()).hexdigest()[:12]

        run = Run(
            id=run_id,
            script_path=script_path,
            args=args or [],
            started_at=datetime.now(),
            source_hash=source_hash,
            last_event_at=time.time(),
            source=source,
        )
        self._cache_put(("run_upsert", run_id, {
            "script_path": script_path,
            "args_json": json.dumps(args or []),
            "started_at": run.started_at.timestamp(),
            "last_event_at": run.last_event_at,
            "source": run.source,
        }))
        # Seed implicit loggables so events that arrive without a node
        # context have a home: __global__ for user code outside an @nb.fn,
        # __agent__ for entries authored by an MCP client (agent).
        run.loggables["__global__"] = LoggableState(
            loggable_id="__global__", kind="global"
        )
        run.loggables["__agent__"] = LoggableState(
            loggable_id="__agent__", kind="agent"
        )
        self.runs[run_id] = run
        self.active_run_id = run_id

        # Only network-originated runs get a remote-mode writer. Watcher and
        # loaded runs (source != "network") already exist on disk.
        if self._remote_dir is not None and source == "network":
            from nebo.core.fileformat import NeboFileWriter
            self._remote_dir.mkdir(parents=True, exist_ok=True)
            timestamp = time.strftime("%Y-%m-%d_%H%M%S")
            filepath = self._remote_dir / f"{timestamp}_{run_id}.nebo"
            run._file_stream = filepath.open("wb")
            run._file_path = str(filepath)
            run._file_writer = NeboFileWriter(
                run._file_stream, run_id=run_id, script_path=script_path,
                args=args or [],
            )
            run._file_writer.write_header()
        else:
            run._file_stream = None
            run._file_path = None
            run._file_writer = None

        return run

    def finalize_run(self, run_id: str) -> None:
        """Close the .nebo file for a run if storage was enabled."""
        run = self.runs.get(run_id)
        if run and getattr(run, "_file_stream", None):
            run._file_writer.close()
            run._file_stream.close()
            run._file_stream = None
            run._file_writer = None

    async def load_nebo_file(self, filepath: str) -> None:
        """Load a .nebo file and reconstruct a Run from it."""
        from nebo.core.fileformat import NeboFileReader

        with open(filepath, "rb") as f:
            reader = NeboFileReader(f)
            meta = reader.read_header()
            run_id = meta["run_id"]

            run = self.create_run(
                meta["script_path"],
                meta.get("args", []),
                run_id,
                source="watcher",  # file-originated: never open a writer
            )

            # The file body carries no run_start entry — run_name, group
            # and started_at live in the header. Synthesize one, the same
            # way the watcher's shallow registration does.
            event_dicts: list[dict] = [{
                "type": "run_start",
                "data": {
                    "script_path": meta.get("script_path", ""),
                    "started_at": meta.get("started_at"),
                    "run_name": meta.get("run_name"),
                    "group": meta.get("group"),
                    "args": meta.get("args", []),
                },
            }]
            event_dicts += [
                {"type": e["type"], **e["payload"]}
                for e in reader.read_entries()
            ]
            # File-originated, like the run itself: never write these events
            # back out through a remote-mode writer.
            await self.ingest_events(
                event_dicts, run_id=run_id, source="watcher",
            )

    def get_active_run(self) -> Optional[Run]:
        """Return the currently active run, if any.

        `active_run_id` is set when a run starts ingesting and cleared by
        its `run_completed` event, so presence alone means "live".
        """
        if self.active_run_id and self.active_run_id in self.runs:
            return self.runs[self.active_run_id]
        return None

    def get_latest_run(self) -> Optional[Run]:
        """Return the most recent run regardless of status."""
        if self.active_run_id and self.active_run_id in self.runs:
            return self.runs[self.active_run_id]
        if self.runs:
            return list(self.runs.values())[-1]
        return None

    async def ingest_events(
        self,
        events: list[dict],
        run_id: str | None = None,
        source: str = "network",
    ) -> None:
        """Ingest a batch of events into the appropriate run."""
        async with self._lock:
            rid = run_id or self.active_run_id
            if not rid or rid not in self.runs:
                # An evicted (or pre-restart) run that lives in the cache is
                # rehydrated instead of recreated — its ingest-state (series
                # type locks, loggable registry, counters) comes back, but
                # not its point history: reads stay on the SQL path.
                if rid and self.cache is not None and self.cache.has_run(rid):
                    run = self._rehydrate_run(rid)
                else:
                    # Create run if it doesn't exist yet (script_path updated
                    # by run_start event).
                    run = self.create_run("direct", run_id=rid, source=source)
                rid = run.id

            run = self.runs[rid]
            # A watched file claiming a run this daemon owns over the network
            # is an alias (e.g. a stray copy of the daemon's own remote-mode
            # file placed in the logdir): its contents were already ingested
            # once, so appending them again would double RAM state. The SQL
            # layer would dedup identical rows (INSERT OR IGNORE); RAM
            # appends would not.
            if source == "watcher" and run.source == "network":
                if rid not in self._alias_dropped:
                    self._alias_dropped.add(rid)
                    logger.warning(
                        "ignoring watcher events for run %s: it is owned by "
                        "a network client — a .nebo file in the watched "
                        "logdir aliases it", rid,
                    )
                return
            for event in events:
                self._process_event(run, event, source)

        # Broadcast to WebSocket clients: serialize ONCE, enqueue per
        # client, never await a socket here.
        if self._ws_clients:
            message = json.dumps(
                {"type": "batch", "run_id": rid, "events": events},
                default=str,
            )
            for client in self._ws_clients[:]:
                client.enqueue(message)

        # An ingested run_start may have seeded a new group — broadcast the
        # updated tree once per batch.
        if self._tree_dirty:
            self._tree_dirty = False
            self._enqueue_tree_update()

        # Notify any waiters of new significant events
        async with self._event_notify:
            self._event_notify.notify_all()

    def _store_media(
        self, run: Run, event: dict, media_src: Optional[tuple],
    ) -> str:
        """Decode a media event's payload once, stash the bytes, and return
        the content-addressed media_id. The event is mutated in place:
        `data` is popped (so broadcasts stay light) and `media_id` is set.

        Bytes land in the LRU always; durably in the blob table only when
        there is no `.nebo` file to reference (media_src is None).
        """
        raw = event.pop("data", b"")
        if isinstance(raw, str):
            raw = base64.b64decode(raw) if raw else b""
        media_id = media_id_for(raw)
        event["media_id"] = media_id
        self.media_lru.put(media_id, raw)
        if self.cache is None:
            self._media_fallback[media_id] = raw
        elif media_src is None:
            self._cache_put(("media_blob", media_id, raw))
        return media_id

    def _rehydrate_run(self, run_id: str) -> Run:
        """Rebuild a Run's ingest-state (no point history) from the cache.

        The returned run has `ram_complete=False`: ingest appends work
        (type locks, counters, edge dedup are restored) but reads route
        to SQL, where the full history lives.
        """
        st = self.cache.get_run_ingest_state(run_id)
        row = st["run_row"]

        def _dt(epoch):
            return datetime.fromtimestamp(epoch) if epoch else None

        run = Run(
            id=run_id,
            script_path=row.get("script_path") or "direct",
            args=json.loads(row["args_json"]) if row.get("args_json") else [],
            started_at=_dt(row.get("started_at")),
            run_name=row.get("run_name"),
            workflow_description=row.get("workflow_description"),
            source=row.get("source") or "network",
            last_event_at=time.time(),
            ram_complete=False,
            latest_step=st["latest_step"],
        )
        for key, attr in (
            ("config_json", "config"),
            ("run_config_json", "run_config"),
        ):
            if row.get(key):
                setattr(run, attr, json.loads(row[key]))
        if row.get("ui_config_json"):
            run.ui_config = json.loads(row["ui_config_json"])
        for lid, meta in st["loggables"].items():
            run.loggables[lid] = LoggableState(
                loggable_id=lid,
                kind=meta["kind"],
                func_name=meta["func_name"],
                docstring=meta["docstring"],
                group=meta["group"],
                ui_hints=meta["ui_hints"],
                params=meta["params"],
                exec_count=meta["exec_count"],
                is_source=meta["is_source"],
            )
        # Seed series type locks with empty entry lists — appends and
        # first-writer-wins keep working; history stays in SQL.
        for lid, types in st["series_types"].items():
            lg = run.loggables.get(lid)
            if lg is None:
                lg = LoggableState(loggable_id=lid, kind="node", auto_seeded=True)
                run.loggables[lid] = lg
            for name, mtype in types.items():
                lg.metrics[name] = {"type": mtype, "entries": []}
        run.edges = list(st["edges"])
        run._edge_set = {
            (e.get("source", ""), e.get("target", "")) for e in run.edges
        }
        self.runs[run_id] = run
        return run

    def _ensure_loggable(self, run: Run, lid: str) -> LoggableState:
        """Return ``run.loggables[lid]``, creating a placeholder if absent.

        Metric/image/audio events carry a ``loggable_id`` but used to be
        dropped outright when the matching ``loggable_register`` hadn't been
        seen yet. That register is only emitted once per run, so a daemon
        restart mid-run (or any out-of-order batch) silently lost every
        subsequent metric for that loggable while logs — which always append
        — survived. Seeding a placeholder here keeps the two symmetric; a
        later real register upgrades it via the ``auto_seeded`` flag.
        """
        lg = run.loggables.get(lid)
        if lg is None:
            lg = LoggableState(loggable_id=lid, kind="node", auto_seeded=True)
            run.loggables[lid] = lg
            self._cache_put(("loggable_upsert", run.id, lid, {"kind": "node"}))
        return lg

    def _process_event(
        self, run: Run, event: dict, source: str = "network",
    ) -> None:
        """Process a single event into run state."""
        # Watcher-annotated media source ref (path, offset, length). Internal —
        # popped before the remote-mode writer or the WS broadcast can see it.
        media_src = event.pop("_media_src", None)
        # In remote mode, persist every *network* event to the run's .nebo
        # file. Never watcher/loaded events — they came FROM a file, and
        # re-writing them would duplicate entries; worse, if the remote dir
        # ever aliases the watched logdir, the watcher would re-ingest the
        # daemon's own output in an unbounded loop. For media the daemon
        # writes itself, capture the on-disk frame span so the cache stores
        # a (path, offset, length) reference instead of a duplicate blob —
        # the same scheme watcher runs use. Flush right after a media frame
        # so a GET /media arriving before the next flush can still resolve
        # the reference off disk.
        writer = (
            getattr(run, "_file_writer", None) if source == "network" else None
        )
        if writer is not None:
            entry_type = event.get("type", "log")
            span = writer.write_entry(entry_type, dict(event))
            if (
                media_src is None
                and entry_type in ("image", "audio")
                and span is not None
                and getattr(run, "_file_path", None)
            ):
                start, length = span
                media_src = (run._file_path, start, length)
                run._file_stream.flush()

        etype = event.get("type", "")
        loggable_id = event.get("loggable_id")
        run.last_event_at = time.time()

        if etype == "log":
            entry = LogEntry(
                timestamp=event.get("timestamp", time.time()),
                node=loggable_id,
                message=event.get("message", ""),
                name=event.get("name") or "text",
                level=event.get("level", "info"),
                step=event.get("step"),
            )
            run.logs.append(entry)
            if loggable_id and loggable_id in run.loggables:
                run.loggables[loggable_id].logs.append(event)
            run.resident_points += 1
            self._cache_put(("log_row", run.id, loggable_id, entry.name,
                             entry.timestamp, entry.step, entry.level,
                             entry.message))

        elif etype == "metric":
            lid = event.get("loggable_id", "")
            if not lid:
                return
            lg = self._ensure_loggable(run, lid)
            mname = event.get("name", "")
            mtype = event.get("metric_type", "line")
            series = lg.metrics.setdefault(
                mname, {"type": mtype, "entries": []}
            )
            # Server is tolerant of type mismatches: first-writer-wins.
            new_entry: dict[str, Any] = {
                "step": event.get("step"),
                "value": event.get("value"),
                "tags": list(event.get("tags") or []),
                "timestamp": event.get("timestamp"),
            }
            if "colors" in event:
                new_entry["colors"] = bool(event["colors"])
            # Line and scatter accumulate over time; bar/pie/histogram
            # are snapshots — re-emitting the same name overwrites the
            # prior value rather than stacking another entry.
            op = "metric_row" if mtype in ("line", "scatter") else "metric_snapshot"
            if mtype in ("line", "scatter"):
                series["entries"].append(new_entry)
                step = new_entry.get("step")
                if step is not None and (
                    run.latest_step is None or step > run.latest_step
                ):
                    run.latest_step = step
            else:
                series["entries"] = [new_entry]
            run.resident_points += 1
            self._cache_put((op, run.id, lid, mname, mtype,
                             new_entry.get("step"), new_entry.get("timestamp"),
                             json.dumps(new_entry.get("value")),
                             json.dumps(new_entry.get("tags") or []),
                             new_entry.get("colors")))
            self._evaluate_alert_rules(run, lid, mname, new_entry)

        elif etype == "metric_batch":
            # Columnar batch of accumulating-metric points (format v4).
            # Equivalence rule: identical to N consecutive plain `metric`
            # events with the shared fields copied onto each.
            lid = event.get("loggable_id", "")
            mname = event.get("name", "")
            steps = event.get("steps") or []
            timestamps = event.get("timestamps") or []
            values = event.get("values") or []
            if not lid or not (len(steps) == len(timestamps) == len(values)):
                return  # malformed batch: drop rather than half-apply
            lg = self._ensure_loggable(run, lid)
            mtype = event.get("metric_type", "line")
            series = lg.metrics.setdefault(mname, {"type": mtype, "entries": []})
            tags = list(event.get("tags") or [])
            colors = event.get("colors") if "colors" in event else None
            for step, ts, value in zip(steps, timestamps, values):
                new_entry = {
                    "step": step,
                    "value": value,
                    "tags": list(tags),
                    "timestamp": ts,
                }
                if colors is not None:
                    new_entry["colors"] = bool(colors)
                series["entries"].append(new_entry)
                if step is not None and (
                    run.latest_step is None or step > run.latest_step
                ):
                    run.latest_step = step
                self._cache_put(("metric_row", run.id, lid, mname, mtype,
                                 step, ts, json.dumps(value),
                                 json.dumps(tags), colors))
                self._evaluate_alert_rules(run, lid, mname, new_entry)
            run.resident_points += len(steps)

        elif etype == "progress":
            if loggable_id and loggable_id in run.loggables:
                run.loggables[loggable_id].progress = event.get("data", {})
                self._cache_put(("loggable_upsert", run.id, loggable_id, {
                    "progress_json": json.dumps(event.get("data", {})),
                }))

        elif etype == "alert":
            data = event.get("data", event)
            alert = {
                "title": data.get("title", ""),
                "text": data.get("text", ""),
                "level": int(data.get("level") or 20),
                "level_name": data.get("level_name", ""),
                "triggered_by": data.get("triggered_by", "code"),
                "loggable_id": event.get("loggable_id") or data.get("loggable_id"),
                "timestamp": data.get("timestamp", time.time()),
            }
            run.alerts.append(alert)
            sig = {
                "type": "alert",
                "timestamp": alert["timestamp"],
                "loggable_id": alert["loggable_id"],
                "message": alert["title"],
            }
            run.significant_events.append(sig)
            self._cache_put(("alert_row", run.id, alert["timestamp"],
                             json.dumps(alert)))
            self._cache_put(("sig_event", run.id, alert["timestamp"], "alert",
                             json.dumps(sig)))

        elif etype == "loggable_register":
            data = event.get("data", {})
            lid = data.get("loggable_id", loggable_id or "")
            kind = data.get("kind", "node")
            existing = run.loggables.get(lid) if lid else None
            registered = False
            if lid and existing is None:
                run.loggables[lid] = LoggableState(
                    loggable_id=lid,
                    kind=kind,
                    func_name=data.get("func_name") or "",
                    docstring=data.get("docstring"),
                    group=data.get("group"),
                    ui_hints=data.get("ui_hints"),
                )
                registered = True
            elif existing is not None and existing.auto_seeded:
                # A real register arrived after a metric/media event seeded a
                # placeholder — fill in the metadata and clear the flag.
                existing.kind = kind
                existing.func_name = data.get("func_name") or ""
                existing.docstring = data.get("docstring")
                existing.group = data.get("group")
                existing.ui_hints = data.get("ui_hints")
                existing.auto_seeded = False
                registered = True
            if registered:
                self._cache_put(("loggable_upsert", run.id, lid, {
                    "kind": kind,
                    "func_name": data.get("func_name") or "",
                    "docstring": data.get("docstring"),
                    "grp": data.get("group"),
                    "ui_hints_json": (
                        json.dumps(data["ui_hints"])
                        if data.get("ui_hints") else None
                    ),
                }))

        elif etype == "node_executed":
            data = event.get("data", {})
            lid = data.get("loggable_id", loggable_id or "")
            caller = data.get("caller")
            # `count` is a delta (transport-coalesced ticks); absent = 1.
            count = int(data.get("count") or 1)
            if lid and lid in run.loggables:
                run.loggables[lid].exec_count += count
                self._cache_put(("loggable_upsert", run.id, lid, {
                    "exec_count": run.loggables[lid].exec_count,
                }))
            if caller and lid:
                key = (caller, lid)
                if key not in run._edge_set:
                    run._edge_set.add(key)
                    run.edges.append({"source": caller, "target": lid})
                    if lid in run.loggables:
                        run.loggables[lid].is_source = False
                        self._cache_put(("loggable_upsert", run.id, lid,
                                         {"is_source": 0}))
                    self._cache_put(("run_upsert", run.id, {
                        "edges_json": json.dumps(run.edges),
                    }))

        elif etype == "edge":
            data = event.get("data", {})
            src = data.get("source", "")
            tgt = data.get("target", "")
            key = (src, tgt)
            if key not in run._edge_set:
                run._edge_set.add(key)
                run.edges.append({"source": src, "target": tgt})
                if tgt in run.loggables:
                    run.loggables[tgt].is_source = False
                    self._cache_put(("loggable_upsert", run.id, tgt,
                                     {"is_source": 0}))
                self._cache_put(("run_upsert", run.id, {
                    "edges_json": json.dumps(run.edges),
                }))

        elif etype == "image":
            if loggable_id:
                lg = self._ensure_loggable(run, loggable_id)
                media_id = self._store_media(run, event, media_src)
                lg.images.append({
                    "media_id": media_id,
                    "name": event.get("name", ""),
                    "step": event.get("step"),
                    "timestamp": event.get("timestamp", time.time()),
                    "labels": event.get("labels"),
                })
                self._cache_put((
                    "media_occurrence", run.id, loggable_id, media_id,
                    "image", event.get("name", ""), event.get("step"),
                    event.get("timestamp", time.time()), None,
                    json.dumps(event["labels"]) if event.get("labels") else None,
                    media_src[0] if media_src else None,
                    media_src[1] if media_src else None,
                    media_src[2] if media_src else None,
                ))

        elif etype == "audio":
            if loggable_id:
                lg = self._ensure_loggable(run, loggable_id)
                media_id = self._store_media(run, event, media_src)
                lg.audio.append({
                    "media_id": media_id,
                    "name": event.get("name", ""),
                    "sr": event.get("sr", 16000),
                    "step": event.get("step"),
                    "timestamp": event.get("timestamp", time.time()),
                })
                self._cache_put((
                    "media_occurrence", run.id, loggable_id, media_id,
                    "audio", event.get("name", ""), event.get("step"),
                    event.get("timestamp", time.time()),
                    event.get("sr", 16000), None,
                    media_src[0] if media_src else None,
                    media_src[1] if media_src else None,
                    media_src[2] if media_src else None,
                ))

        elif etype == "description":
            desc = event.get("data", {}).get("description", "")
            if run.workflow_description:
                run.workflow_description += "\n\n" + desc
            else:
                run.workflow_description = desc
            self._cache_put(("run_upsert", run.id, {
                "workflow_description": run.workflow_description,
            }))

        elif etype == "config":
            cfg = event.get("data", {})
            run.config = cfg
            self._cache_put(("run_upsert", run.id, {
                "config_json": json.dumps(cfg),
            }))
            if loggable_id and loggable_id in run.loggables:
                run.loggables[loggable_id].params.update(cfg)
                self._cache_put(("loggable_upsert", run.id, loggable_id, {
                    "params_json": json.dumps(run.loggables[loggable_id].params),
                }))

        elif etype == "ui_config":
            run.ui_config = event.get("data", {})
            self._cache_put(("run_upsert", run.id, {
                "ui_config_json": json.dumps(run.ui_config),
            }))

        elif etype == "run_config":
            run.run_config = event.get("data", {})
            self._cache_put(("run_upsert", run.id, {
                "run_config_json": json.dumps(run.run_config),
            }))

        elif etype == "run_start":
            data = event.get("data", {})
            script_path = data.get("script_path", "")
            if script_path:
                run.script_path = script_path
            # Store run_name if provided
            run_name = data.get("run_name")
            if run_name is not None:
                run.run_name = run_name
            # Watcher-synthesized run_starts (shallow header registration)
            # carry the file's real started_at and args so a header-only run
            # lists correctly. SDK run_starts omit these — no behavior change.
            started_at = data.get("started_at")
            if started_at is not None:
                run.started_at = datetime.fromtimestamp(started_at)
            args = data.get("args")
            if args:
                run.args = list(args)
            self._cache_put(("run_upsert", run.id, {
                "script_path": run.script_path,
                "run_name": run.run_name,
                "args_json": json.dumps(run.args),
                "started_at": (
                    run.started_at.timestamp() if run.started_at else None
                ),
                "last_event_at": run.last_event_at,
                "source": run.source,
            }))
            # A shallow registration is not "the live run" — don't hijack
            # active_run_id with a historical file discovered on disk. The
            # real run_start in the file body (read when the run deepens) sets
            # it if the run is actually live.
            if not data.get("_shallow") and self.active_run_id is None:
                self.active_run_id = run.id
            # Seed the run tree from the run's birth group (seed-once: a run
            # already placed in tree.json is not re-placed). Broadcast happens
            # after the batch, in ingest_events.
            group = data.get("group")
            if group and self.tree is not None:
                try:
                    if self.tree.seed_run(run.id, group):
                        self._tree_dirty = True
                except ValueError:
                    logger.warning("ignoring invalid group %r on run %s",
                                   group, run.id)
            # Seed implicit loggables — __global__ for user logs outside an
            # @nb.fn context, __agent__ for MCP-authored entries from an agent.
            run.loggables.setdefault(
                "__global__",
                LoggableState(loggable_id="__global__", kind="global"),
            )
            run.loggables.setdefault(
                "__agent__",
                LoggableState(loggable_id="__agent__", kind="agent"),
            )
            # Open a remote-mode writer only for runs that arrived over the
            # network. Watcher/loaded runs (source != "network") already exist
            # on disk — writing them again would duplicate the file. The event
            # source is gated too: a watcher-discovered run_start for a run
            # the daemon itself wrote (its file placed where the watcher sees
            # it) must not open a second writer for the same run_id.
            if (
                self._remote_dir is not None
                and source == "network"
                and run.source == "network"
                and not getattr(run, "_file_writer", None)
            ):
                from nebo.core.fileformat import NeboFileWriter
                self._remote_dir.mkdir(parents=True, exist_ok=True)
                timestamp = time.strftime("%Y-%m-%d_%H%M%S")
                filepath = self._remote_dir / f"{timestamp}_{run.id}.nebo"
                run._file_stream = filepath.open("wb")
                run._file_path = str(filepath)
                run._file_writer = NeboFileWriter(
                    run._file_stream, run_id=run.id, script_path=script_path,
                )
                run._file_writer.write_header()

        elif etype == "run_completed":
            # A writer-finalization marker only — it carries no lifecycle
            # semantics. There is no ended_at; recency is last_event_at
            # (updated at the top of this method). Clearing active_run_id
            # keeps get_active_run() honest, and the significant event still
            # lets /events/wait fire on completion.
            if self.active_run_id == run.id:
                self.active_run_id = None
            # Prefer the event's own timestamp: a daemon-stamped time would
            # make the same run_completed produce a distinct sig row on every
            # re-ingest, defeating the cache's OR IGNORE dedup.
            sig = {
                "type": "run_completed",
                "timestamp": event.get("timestamp") or time.time(),
            }
            run.significant_events.append(sig)
            self._cache_put(("run_upsert", run.id, {
                "last_event_at": run.last_event_at,
            }))
            self._cache_put(("sig_event", run.id, sig["timestamp"],
                             "run_completed", json.dumps(sig)))
            self.finalize_run(run.id)

    def _evaluate_alert_rules(
        self, run: Run, loggable_id: str, name: str, entry: dict,
    ) -> None:
        """Fire any alert rule whose condition the incoming metric satisfies.

        Called from the metric branch of `_process_event` (under the ingest
        lock). Only numeric values are evaluated — snapshot chart types
        carry dict values and are skipped. Each rule fires at most once per
        run; the fired alert lands in `run.alerts`, where the existing
        `/runs/{id}/alerts/wait` endpoint (and thus `wait_for_alert`)
        picks it up via the post-ingest notify.
        """
        value = entry.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        for rule in self.alert_rules.values():
            cond = rule["condition"]
            if cond["metric"] == HEARTBEAT_METRIC:
                continue  # reserved: evaluated by the heartbeat loop, never by metrics
            if cond["metric"] != name:
                continue
            if cond.get("loggable_id") and cond["loggable_id"] != loggable_id:
                continue
            if rule.get("run_id") and rule["run_id"] != run.id:
                continue
            if any(f["run_id"] == run.id for f in rule["fired"]):
                continue  # fire once per run
            if not ALERT_CONDITION_OPS[cond["op"]](value, cond["value"]):
                continue
            self._fire_rule_alert(
                rule, run.id, value=value,
                loggable_id=loggable_id, step=entry.get("step"),
            )

    def _fire_rule_alert(
        self, rule: dict, run_id: str, *, value: Any,
        loggable_id: Optional[str], step: Optional[int],
        ts: Optional[float] = None,
    ) -> None:
        """Record a rule firing: mark the rule, append the alert to the run
        (when RAM-resident), and persist alert + significant-event rows to
        the cache so evicted/restarted reads still see it."""
        if ts is None:
            ts = time.time()
        rule["fired"].append({
            "run_id": run_id,
            "value": value,
            "step": step,
            "timestamp": ts,
        })
        level = int(rule.get("level") or 20)
        alert = {
            "title": rule.get("title", ""),
            "text": rule.get("text", ""),
            "level": level,
            "level_name": _ALERT_LEVEL_NAMES.get(level, str(level)),
            "triggered_by": "cli",
            "condition": format_alert_condition(rule["condition"]),
            "rule_id": rule["id"],
            "loggable_id": loggable_id,
            "value": value,
            "step": step,
            "timestamp": ts,
        }
        sig = {
            "type": "alert",
            "timestamp": ts,
            "loggable_id": loggable_id,
            "message": alert["title"],
        }
        run = self.runs.get(run_id)
        if run is not None:
            run.alerts.append(alert)
            run.significant_events.append(sig)
        self._cache_put(("alert_row", run_id, ts, json.dumps(alert)))
        self._cache_put(("sig_event", run_id, ts, "alert", json.dumps(sig)))

    def evaluate_heartbeat_rules(self, now: Optional[float] = None) -> bool:
        """Fire heartbeat (``last_event``) rules for runs idle past threshold.

        Called every ``HEARTBEAT_TICK_S`` by the always-on lifespan task
        (under the ingest lock), and directly by tests with an injected
        ``now``. Idle time is ``now - last_event_at``.

        Run-scoped rules evaluate regardless of when the rule was created —
        a run that already went quiet fires on the first tick, so waiting on
        an already-finished run returns immediately — and fall back to the
        cache when the run has been evicted from RAM (long-idle is exactly
        the evicted regime). Global rules scan RAM-resident runs only and
        skip runs whose last activity precedes the rule's creation, so dusty
        historical runs never insta-fire; the flip side is they can miss
        already-evicted runs — scope with run_id for reliable completion
        detection.

        Returns True if anything fired; the caller must then notify
        ``_event_notify`` itself — the run is quiet, so no ingest will.
        """
        if now is None:
            now = time.time()
        fired_any = False
        for rule in self.alert_rules.values():
            cond = rule["condition"]
            if cond["metric"] != HEARTBEAT_METRIC:
                continue
            op = ALERT_CONDITION_OPS[cond["op"]]
            if rule.get("run_id"):
                candidates = [rule["run_id"]]
            else:
                candidates = [
                    rid for rid, run in self.runs.items()
                    if run.last_event_at
                    and run.last_event_at >= rule["created_at"]
                ]
            for rid in candidates:
                if any(f["run_id"] == rid for f in rule["fired"]):
                    continue  # fire once per run
                run = self.runs.get(rid)
                last = run.last_event_at if run is not None else None
                if not last and self.cache is not None and self.cache.has_run(rid):
                    last = self.cache.run_recency(rid)
                if not last:
                    continue
                idle = now - last
                if not op(idle, cond["value"]):
                    continue
                self._fire_rule_alert(
                    rule, rid, value=round(idle, 3),
                    loggable_id=None, step=None, ts=now,
                )
                fired_any = True
        return fired_any


def create_daemon_app(state: DaemonState | None = None, port: int | None = None) -> Any:
    """Create the FastAPI daemon application.

    Args:
        state: Optional pre-existing DaemonState. Creates new if None.
        port: Unused; kept for backwards-compatible call sites.

    Returns:
        FastAPI application instance.
    """
    from contextlib import asynccontextmanager

    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from nebo.server.watcher import DirectoryWatcher

    if state is None:
        state = DaemonState()
        # The real server starts "local" — network runs are rejected unless
        # env opts into a remote mode below. A test-provided state keeps its
        # own default ("remote-ephemeral"), so direct ingest works flag-free.
        state.mode = "local"

    logdir = os.environ.get("NEBO_LOGDIR")
    remote = os.environ.get("NEBO_REMOTE")
    remote_ephemeral = bool(os.environ.get("NEBO_REMOTE_EPHEMERAL"))
    no_local = bool(os.environ.get("NEBO_NO_LOCAL"))

    if remote and remote_ephemeral:
        raise RuntimeError(
            "nebo serve: --remote and --remote-ephemeral are mutually exclusive"
        )
    if remote:
        state.mode = "remote"
        if state._remote_dir is None:
            if remote.strip().lower() in ("1", "true", "yes"):
                base = Path(logdir) if logdir else Path(".nebo")
                state._remote_dir = base / "remote"
            else:
                state._remote_dir = Path(remote)
    elif remote_ephemeral:
        state.mode = "remote-ephemeral"

    if state._logdir is None and logdir and not no_local:
        state._logdir = Path(logdir)

    # Backstop for launches that bypass `nebo serve` (uvicorn --factory as in
    # the Dockerfile, embedders, env-only config): a remote writer dir that
    # aliases the watched logdir feeds the watcher the daemon's own files —
    # every event re-ingests in a loop. cli.py validates the flag form; this
    # guards NEBO_REMOTE and directly-configured states. Nesting under the
    # logdir stays fine (the watcher is non-recursive).
    if (
        state._remote_dir is not None
        and state._logdir is not None
        and Path(state._remote_dir).resolve() == Path(state._logdir).resolve()
    ):
        raise RuntimeError(
            "nebo daemon: the remote dir cannot be the watched logdir "
            f"({Path(state._logdir).resolve()}) — the watcher would re-ingest "
            "the daemon's own files. Use a subdirectory (the default "
            "<logdir>/remote/) or disable the watcher (NEBO_NO_LOCAL=1)."
        )

    # The run tree lives under the workspace root (the logdir) in *every* mode
    # — it anchors meta/, so --no-local and remote daemons still have one.
    if state.tree is None and logdir:
        from nebo.server.tree import TreeStore
        state.tree = TreeStore(Path(logdir) / "meta")

    # SQLite cache: opt-in via NEBO_CACHE_PATH (set by `nebo serve` unless
    # --no-cache). Directly-constructed DaemonStates (tests, embedders)
    # stay pure-RAM unless they pass a cache themselves.
    cache_path = os.environ.get("NEBO_CACHE_PATH")
    if (
        state.cache is None
        and cache_path
        and not os.environ.get("NEBO_NO_CACHE")
    ):
        from nebo.server.cache import (
            DEFAULT_MEDIA_LRU_MB,
            DEFAULT_RETENTION_DAYS,
            sweep_cache_dir,
        )

        retention = int(
            os.environ.get("NEBO_CACHE_RETENTION_DAYS") or DEFAULT_RETENTION_DAYS
        )
        media_mb = int(
            os.environ.get("NEBO_MEDIA_LRU_MB") or DEFAULT_MEDIA_LRU_MB
        )
        sweep_cache_dir(Path(cache_path).parent, retention)
        run_cache = RunCache(
            cache_path, logdir=state._logdir, media_lru_mb=media_mb
        )
        run_cache.start()
        state.cache = run_cache
        state.media_lru = run_cache.media_lru
    ram_budget_mb = os.environ.get("NEBO_RAM_BUDGET_MB")
    if ram_budget_mb:
        from nebo.server.cache import BYTES_PER_POINT

        state.ram_budget_points = (
            int(ram_budget_mb) * 1024 * 1024
        ) // BYTES_PER_POINT

    @asynccontextmanager
    async def lifespan(app):
        watcher = None
        watcher_task = None
        if state._logdir is not None:
            watcher = DirectoryWatcher(state, logdir=state._logdir)
            state._watcher = watcher
            watcher_task = asyncio.create_task(watcher.run())
        janitor_task = None
        if state.cache is not None:
            async def _janitor_loop():
                while True:
                    await asyncio.sleep(60)
                    try:
                        # Hold the ingest lock so eviction never races a
                        # batch mid-processing; the flush barrier inside
                        # janitor_pass runs in a worker thread.
                        async with state._lock:
                            await asyncio.to_thread(state.janitor_pass)
                        state.cache.incremental_vacuum()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass  # the janitor must never die
            janitor_task = asyncio.create_task(_janitor_loop())

        # Always-on (unlike the cache-gated janitor): heartbeat rules must
        # work on --no-cache daemons too.
        async def _heartbeat_loop():
            while True:
                await asyncio.sleep(HEARTBEAT_TICK_S)
                try:
                    if not any(
                        r["condition"].get("metric") == HEARTBEAT_METRIC
                        for r in state.alert_rules.values()
                    ):
                        continue
                    async with state._lock:
                        fired = state.evaluate_heartbeat_rules()
                    if fired:
                        # The run went quiet, so no ingest will ever wake
                        # /runs/{id}/alerts/wait — notify explicitly.
                        async with state._event_notify:
                            state._event_notify.notify_all()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass  # must never die

        heartbeat_task = asyncio.create_task(_heartbeat_loop())
        try:
            yield
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            if janitor_task is not None:
                janitor_task.cancel()
                try:
                    await janitor_task
                except asyncio.CancelledError:
                    pass
            if watcher is not None:
                watcher.stop()
            if watcher_task is not None:
                await watcher_task
            if state.cache is not None:
                state.cache.close()

    app = FastAPI(title="Nebo Daemon Server", lifespan=lifespan)
    app.state.daemon = state

    # CORS for development (Vite dev server)
    from starlette.middleware.cors import CORSMiddleware
    from starlette.types import ASGIApp, Receive, Scope, Send

    class CORSWithWebSocket:
        """Wraps CORSMiddleware but lets WebSocket connections pass through."""
        def __init__(self, app: ASGIApp) -> None:
            self.app = app
            self.cors = CORSMiddleware(
                app,
                allow_origins=["*"],
                allow_methods=["*"],
                allow_headers=["*"],
            )

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "websocket":
                await self.app(scope, receive, send)
            else:
                await self.cors(scope, receive, send)

    app.add_middleware(CORSWithWebSocket)

    # Optional bearer-token auth, split into independent read/write
    # gates. When the daemon process has NEBO_API_TOKEN set (e.g. as a
    # Hugging Face Space secret) the gates kick in:
    #
    #   read mode (NEBO_READ_MODE, default 'public'):
    #     'public'  → GET requests pass without a token
    #     'private' → GET requests require a token
    #   write mode (NEBO_WRITE_MODE, default 'private'):
    #     'public'  → non-GET requests pass without a token
    #     'private' → non-GET requests require a token
    #
    # /health and the static UI bundle stay open in every mode so
    # external healthchecks and the iframe bootstrap HTML keep working.
    # Without NEBO_API_TOKEN set, both gates are open regardless of
    # mode — preserves the local-dev "no auth needed" workflow.
    expected_token = os.environ.get("NEBO_API_TOKEN")
    _read_private = os.environ.get("NEBO_READ_MODE", "public").lower() == "private"
    _write_private = os.environ.get("NEBO_WRITE_MODE", "private").lower() == "private"
    _GATED_PREFIXES = (
        "/events", "/ingest", "/run", "/runs",
        "/logs", "/loggables", "/load",
        "/graph", "/alerts", "/tree", "/groups",
    )

    def _is_read(method: str) -> bool:
        # GET/HEAD are reads; everything else (POST/PUT/PATCH/DELETE)
        # mutates state and gates on write mode.
        return method.upper() in ("GET", "HEAD")

    if expected_token:
        from fastapi.responses import JSONResponse

        @app.middleware("http")
        async def _auth_middleware(request, call_next):
            path = request.url.path
            if path == "/health" or not any(path.startswith(p) for p in _GATED_PREFIXES):
                return await call_next(request)
            # CORS preflight requests can't carry custom headers; let
            # them through and rely on the eventual real request to
            # authenticate.
            if request.method == "OPTIONS":
                return await call_next(request)
            require_token = _read_private if _is_read(request.method) else _write_private
            if not require_token:
                return await call_next(request)
            token = request.headers.get("x-nebo-token") or request.query_params.get("token")
            if token != expected_token:
                return JSONResponse(status_code=401, content={"error": "unauthorized"})
            return await call_next(request)

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "timestamp": time.time(),
            "active_run": state.active_run_id,
            "total_runs": len(state.runs),
            # "local" | "remote" | "remote-ephemeral" — lets the SDK fail fast
            # at connect time instead of after a rejected run_start.
            "mode": state.mode,
        }

    @app.post("/events")
    async def ingest_events(request: Request, run_id: str | None = None):
        # Two wire formats: application/msgpack (SDK — a concatenation of
        # individually-packed event maps, media bytes native) and JSON
        # (MCP/CLI writers, tests). Both fan into the same ingest path.
        content_type = request.headers.get("content-type", "")
        body = await request.body()
        if "msgpack" in content_type:
            import msgpack

            unpacker = msgpack.Unpacker(raw=False)
            unpacker.feed(body)
            events = [e for e in unpacker if isinstance(e, dict)]
        else:
            parsed = json.loads(body) if body else []
            events = parsed if isinstance(parsed, list) else []
        if state.local_mode_rejects(events, run_id):
            return JSONResponse(status_code=409, content=_LOCAL_ONLY_ERROR)
        await state.ingest_events(events, run_id)
        return {"status": "ok", "count": len(events)}

    # Legacy endpoint for backward compat
    @app.post("/ingest")
    async def ingest_legacy(events: list[dict[str, Any]]):
        if state.local_mode_rejects(events, None):
            return JSONResponse(status_code=409, content=_LOCAL_ONLY_ERROR)
        await state.ingest_events(events)
        return {"status": "ok", "count": len(events)}

    @app.get("/runs")
    async def list_runs():
        return {
            "runs": state.all_summaries(),
            "active_run": state.active_run_id,
        }

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str):
        await state.ensure_deep(run_id)
        summary = state.run_summary(run_id)
        if summary is None:
            return JSONResponse(status_code=404, content={"error": f"Run '{run_id}' not found"})
        return summary

    @app.get("/runs/{run_id}/graph")
    async def get_run_graph(run_id: str):
        await state.ensure_deep(run_id)
        graph = state.run_graph(run_id)
        if graph is None:
            return JSONResponse(status_code=404, content={"error": f"Run '{run_id}' not found"})
        return graph

    @app.get("/runs/{run_id}/logs")
    async def get_run_logs(run_id: str, loggable_id: str | None = None, limit: int = 100):
        await state.ensure_deep(run_id)
        logs = state.run_logs(run_id, loggable_id=loggable_id, limit=limit)
        if logs is None:
            return JSONResponse(status_code=404, content={"error": f"Run '{run_id}' not found"})
        return {"logs": logs}

    @app.get("/runs/{run_id}/metrics")
    async def get_run_metrics(run_id: str):
        await state.ensure_deep(run_id)
        metrics = state.run_metrics(run_id)
        if metrics is None:
            return JSONResponse(status_code=404, content={"error": f"Run '{run_id}' not found"})
        return {"metrics": metrics}

    @app.get("/runs/{run_id}/images")
    async def get_run_images(run_id: str):
        await state.ensure_deep(run_id)
        images = state.run_media_listing(run_id, "image")
        if images is None:
            return JSONResponse(status_code=404, content={"error": f"Run '{run_id}' not found"})
        return {"images": images}

    @app.get("/runs/{run_id}/audio")
    async def get_run_audio(run_id: str):
        await state.ensure_deep(run_id)
        audio = state.run_media_listing(run_id, "audio")
        if audio is None:
            return JSONResponse(status_code=404, content={"error": f"Run '{run_id}' not found"})
        return {"audio": audio}

    @app.get("/runs/{run_id}/alerts")
    async def get_run_alerts(run_id: str):
        """Fired alerts for one run — both code-fired (`nb.alert`) and
        rule-fired (`triggered_by: "cli"`) entries, in firing order."""
        await state.ensure_deep(run_id)
        alerts = state.run_alerts(run_id)
        if alerts is None:
            return JSONResponse(status_code=404, content={"error": f"Run '{run_id}' not found"})
        return {"alerts": alerts}

    @app.get("/runs/{run_id}/media/{media_id}")
    async def get_media(run_id: str, media_id: str, request: Request):
        await state.ensure_deep(run_id)
        raw = state.media_bytes(run_id, media_id)
        if raw is None:
            return JSONResponse(status_code=404, content={"error": f"Media '{media_id}' not found"})
        # media_id is content-addressed, so it doubles as a permanent ETag.
        if request.headers.get("if-none-match") == media_id:
            return Response(status_code=304)
        return Response(
            content=raw,
            media_type=_sniff_mime(raw),
            headers={
                "ETag": media_id,
                "Cache-Control": "public, max-age=31536000, immutable",
            },
        )

    @app.get("/runs/{run_id}/loggables/{loggable_id}")
    async def get_run_loggable(
        run_id: str,
        loggable_id: str,
        name: Optional[str] = None,
        tag: Optional[str] = None,
        step: Optional[int] = None,
    ):
        if not state.has_run_anywhere(run_id):
            return JSONResponse(status_code=404, content={"error": f"Run '{run_id}' not found"})
        await state.ensure_deep(run_id)
        payload = state.run_loggable(run_id, loggable_id)
        if payload is None:
            return JSONResponse(status_code=404, content={"error": f"Loggable '{loggable_id}' not found"})

        # Apply optional query-string filters to metrics. FastAPI does the
        # type coercion (and a 422 on bad input) via the parameter types.
        # ?name=X   — return only the named series (others are omitted entirely)
        # ?tag=X    — keep only entries whose tags list contains X (line/scatter)
        # ?step=N   — keep only entries whose step equals N (exact match)
        metrics = dict(payload["metrics"])
        if name is not None:
            metrics = {k: v for k, v in metrics.items() if k == name}
        if tag is not None or step is not None:
            filtered_metrics: dict[str, Any] = {}
            for mname, series in metrics.items():
                filtered_entries = []
                for entry in series.get("entries", []):
                    if tag is not None and tag not in (entry.get("tags") or []):
                        continue
                    if step is not None and entry.get("step") != step:
                        continue
                    filtered_entries.append(entry)
                series_out = dict(series)
                series_out["entries"] = filtered_entries
                filtered_metrics[mname] = series_out
            metrics = filtered_metrics
        payload["metrics"] = metrics
        return payload

    # --- Event wait endpoint ---

    @app.get("/runs/{run_id}/events/wait")
    async def wait_for_event(
        run_id: str,
        types: str = "run_completed",
        timeout: float = 300,
        since: float = 0,
    ):
        if not state.has_run_anywhere(run_id):
            return JSONResponse(status_code=404, content={"error": f"Run '{run_id}' not found"})
        await state.ensure_deep(run_id)

        wanted = set(t.strip() for t in types.split(","))
        deadline = asyncio.get_event_loop().time() + timeout

        def _find_match() -> Optional[dict]:
            for evt in state.run_significant_events(run_id) or []:
                if evt["type"] in wanted and evt.get("timestamp", 0) > since:
                    return evt
            return None

        # Check for existing matching events first
        match = _find_match()
        if match:
            return {"status": "event", "event": match}

        # Wait for new events with condition variable
        async with state._event_notify:
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(
                        state._event_notify.wait(), timeout=remaining
                    )
                except asyncio.TimeoutError:
                    break
                match = _find_match()
                if match:
                    return {"status": "event", "event": match}

        return {"status": "timeout"}

    # --- Alert wait endpoint ---

    @app.get("/runs/{run_id}/alerts/wait")
    async def runs_alerts_wait(
        run_id: str,
        timeout: float = 300.0,
        min_level: int = 20,
    ) -> dict:
        if not state.has_run_anywhere(run_id):
            return JSONResponse(status_code=404, content={"error": f"Run '{run_id}' not found"})
        await state.ensure_deep(run_id)

        deadline = asyncio.get_event_loop().time() + max(0.0, float(timeout))

        def _find_qualifying_alert(start: int) -> Optional[dict]:
            alerts = state.run_alerts(run_id) or []
            for alert in alerts[start:]:
                if alert.get("level", 20) >= min_level:
                    return alert
            return None

        # Check for existing qualifying alerts before waiting.
        seen = 0
        match = _find_qualifying_alert(seen)
        if match:
            return {"status": "alert", "alert": match}
        seen = len(state.run_alerts(run_id) or [])

        async with state._event_notify:
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(
                        state._event_notify.wait(), timeout=remaining
                    )
                except asyncio.TimeoutError:
                    break
                match = _find_qualifying_alert(seen)
                if match:
                    return {"status": "alert", "alert": match}
                seen = len(state.run_alerts(run_id) or [])

        return {"status": "timeout"}

    # --- Alert rules (created via CLI / MCP, evaluated on metric ingest) ---

    @app.get("/alerts")
    async def list_alerts(run_id: Optional[str] = None):
        """Unified alert listing: cli rules plus code-fired alerts.

        Rules created over the API carry `triggered_by: "cli"` and a
        structured condition; alerts fired by `nb.alert(...)` in pipeline
        code appear with `triggered_by: "code"` and the run they fired in.
        """
        if run_id is not None and not state.has_run_anywhere(run_id):
            return JSONResponse(status_code=404, content={"error": f"Run '{run_id}' not found"})
        items: list[dict] = []
        for rule in state.alert_rules.values():
            if run_id and rule.get("run_id") and rule["run_id"] != run_id:
                continue
            items.append({**rule, "condition_str": format_alert_condition(rule["condition"])})
        run_ids = [run_id] if run_id else state.known_run_ids()
        for rid in run_ids:
            for alert in state.run_alerts(rid) or []:
                if alert.get("triggered_by", "code") == "code":
                    items.append({**alert, "run_id": rid})
        return {"alerts": items}

    @app.post("/alerts")
    async def create_alert_rule(body: dict[str, Any]):
        title = (body.get("title") or "").strip()
        if not title:
            return JSONResponse(status_code=422, content={"error": "title is required"})
        err = validate_alert_condition(body.get("condition"))
        if err:
            return JSONResponse(status_code=422, content={"error": err})
        level = body.get("level", 20)
        if isinstance(level, bool) or not isinstance(level, int):
            return JSONResponse(status_code=422, content={"error": "level must be an integer"})
        run_id = body.get("run_id")
        # Anywhere, not RAM-only: heartbeat rules target long-idle runs,
        # which are exactly the ones evicted from RAM into the cache.
        if run_id is not None and not state.has_run_anywhere(run_id):
            return JSONResponse(status_code=404, content={"error": f"Run '{run_id}' not found"})
        condition = body["condition"]
        rule = {
            "id": uuid.uuid4().hex[:8],
            "title": title,
            "text": body.get("text") or "",
            "level": level,
            "triggered_by": "cli",
            "condition": {
                "metric": condition["metric"],
                "op": condition["op"],
                "value": condition["value"],
                "loggable_id": condition.get("loggable_id"),
            },
            "run_id": run_id,
            "created_at": time.time(),
            "fired": [],
        }
        state.alert_rules[rule["id"]] = rule
        return rule

    @app.get("/alerts/{rule_id}")
    async def get_alert_rule(rule_id: str):
        rule = state.alert_rules.get(rule_id)
        if rule is None:
            return JSONResponse(status_code=404, content={"error": f"Alert rule '{rule_id}' not found"})
        return {**rule, "condition_str": format_alert_condition(rule["condition"])}

    @app.delete("/alerts/{rule_id}")
    async def delete_alert_rule(rule_id: str):
        if rule_id not in state.alert_rules:
            return JSONResponse(status_code=404, content={"error": f"Alert rule '{rule_id}' not found"})
        del state.alert_rules[rule_id]
        return {"status": "deleted", "id": rule_id}

    # Backward-compatible endpoints (use latest run)
    @app.get("/graph")
    async def get_graph():
        run = state.get_latest_run()
        if not run:
            return {"nodes": {}, "edges": [], "workflow_description": None}
        await state.ensure_deep(run.id)
        return state.run_graph(run.id) or {
            "nodes": {}, "edges": [], "workflow_description": None,
        }

    @app.get("/logs")
    async def get_logs(loggable_id: str | None = None, limit: int = 100):
        run = state.get_latest_run()
        if not run:
            return {"logs": []}
        await state.ensure_deep(run.id)
        logs = state.run_logs(run.id, loggable_id=loggable_id, limit=limit) or []
        return {"logs": [
            {
                "timestamp": l["timestamp"],
                "loggable_id": l["loggable_id"],
                "name": l["name"],
                "message": l["message"],
            }
            for l in logs
        ]}

    @app.get("/loggables/{loggable_id}")
    async def get_loggable(loggable_id: str):
        run = state.get_latest_run()
        if not run:
            return JSONResponse(status_code=404, content={"error": "No runs"})
        await state.ensure_deep(run.id)
        payload = state.run_loggable(run.id, loggable_id)
        if payload is None:
            return JSONResponse(status_code=404, content={"error": f"Loggable '{loggable_id}' not found"})
        return payload

    @app.post("/load")
    async def load_file(body: dict[str, Any]):
        """Load a .nebo file into the daemon for viewing and Q&A."""
        filepath = body.get("filepath", "")
        if not filepath or not os.path.exists(filepath):
            return JSONResponse(status_code=404, content={"error": f"File not found: {filepath}"})
        try:
            await state.load_nebo_file(filepath)
            return {"status": "loaded", "filepath": filepath}
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": str(e)})

    # --- Run tree (groups + placements + docs) ---
    #
    # NOTE on route order: the doc routes (`/groups/{path:path}/docs/{name}`)
    # MUST be declared before the group catch-alls (`/groups/{path:path}`),
    # because `{path:path}` is greedy and would otherwise swallow a doc path.
    from nebo.server.tree import TreeConflict

    _NO_TREE = JSONResponse(
        status_code=503,
        content={"error": "the run tree needs a workspace (a --logdir)"},
    )

    @app.get("/tree")
    async def get_tree():
        return state._tree_payload()

    @app.post("/groups")
    async def create_group(body: dict[str, Any]):
        if state.tree is None:
            return _NO_TREE
        try:
            created = state.tree.create_group(body.get("path", ""))
        except ValueError as e:
            return JSONResponse(status_code=422, content={"error": str(e)})
        state._enqueue_tree_update()
        return JSONResponse(
            status_code=201 if created else 200, content=state._tree_payload()
        )

    @app.get("/groups/{path:path}/docs/{name}")
    async def get_group_doc(path: str, name: str):
        if state.tree is None:
            return _NO_TREE
        try:
            content = state.tree.get_doc(path, name)
        except ValueError as e:
            return JSONResponse(status_code=422, content={"error": str(e)})
        if content is None:
            return JSONResponse(
                status_code=404, content={"error": f"doc '{name}' not found"}
            )
        return Response(content=content, media_type="text/markdown")

    @app.put("/groups/{path:path}/docs/{name}")
    async def set_group_doc(path: str, name: str, request: Request):
        if state.tree is None:
            return _NO_TREE
        body = await request.body()
        if len(body) > 1_048_576:
            return JSONResponse(
                status_code=413, content={"error": "doc exceeds 1 MB"}
            )
        try:
            created = state.tree.set_doc(path, name, body.decode("utf-8"))
        except ValueError as e:
            return JSONResponse(status_code=422, content={"error": str(e)})
        state._enqueue_tree_update()
        return JSONResponse(status_code=201 if created else 200, content={"ok": True})

    @app.delete("/groups/{path:path}/docs/{name}")
    async def delete_group_doc(path: str, name: str):
        if state.tree is None:
            return _NO_TREE
        try:
            deleted = state.tree.delete_doc(path, name)
        except ValueError as e:
            return JSONResponse(status_code=422, content={"error": str(e)})
        if not deleted:
            return JSONResponse(
                status_code=404, content={"error": f"doc '{name}' not found"}
            )
        state._enqueue_tree_update()
        return Response(status_code=204)

    @app.patch("/groups/{path:path}")
    async def move_group(path: str, body: dict[str, Any]):
        if state.tree is None:
            return _NO_TREE
        try:
            state.tree.move_group(path, body.get("new_path", ""))
        except TreeConflict as e:
            return JSONResponse(status_code=409, content={"error": str(e)})
        except ValueError as e:
            return JSONResponse(status_code=422, content={"error": str(e)})
        state._enqueue_tree_update()
        return state._tree_payload()

    @app.delete("/groups/{path:path}")
    async def delete_group(path: str):
        if state.tree is None:
            return _NO_TREE
        try:
            state.tree.delete_group(path, set(state.known_run_ids()))
        except TreeConflict as e:
            return JSONResponse(status_code=409, content={"error": str(e)})
        except ValueError as e:
            return JSONResponse(status_code=422, content={"error": str(e)})
        state._enqueue_tree_update()
        return state._tree_payload()

    @app.put("/runs/{run_id}/group")
    async def set_run_group(run_id: str, body: dict[str, Any]):
        if state.tree is None:
            return _NO_TREE
        try:
            gp = state.tree.set_run_group(run_id, body.get("group", ""))
        except ValueError as e:
            return JSONResponse(status_code=422, content={"error": str(e)})
        state._enqueue_tree_update()
        return {"run_id": run_id, "group": gp}

    @app.websocket("/stream")
    async def websocket_endpoint(ws: WebSocket):
        # Browsers can't set custom WebSocket headers, so accept the
        # token from `?token=...` as well as `X-Nebo-Token`. The
        # handshake is gated by read mode (since the WS primarily
        # broadcasts state to subscribers) and inbound `receive_text`
        # is gated by write mode (clients can also push events here).
        client_authed = False
        if expected_token:
            token = ws.query_params.get("token") or ws.headers.get("x-nebo-token")
            client_authed = token == expected_token
            if _read_private and not client_authed:
                await ws.close(code=4401)
                return
        await ws.accept()
        client = _WsClient(ws)
        client.task = asyncio.create_task(client.sender())
        state._ws_clients.append(client)
        try:
            while True:
                data = await ws.receive_text()
                # If write mode is private, silently ignore inbound
                # events from unauthenticated clients. They keep the
                # subscription so they still receive broadcasts.
                if expected_token and _write_private and not client_authed:
                    continue
                events = decode_batch(data)
                # Local-only daemons don't create runs from network input.
                if state.local_mode_rejects(events, None):
                    continue
                await state.ingest_events(events)
        except (WebSocketDisconnect, Exception):
            pass
        finally:
            client.task.cancel()
            try:
                await client.task
            except (asyncio.CancelledError, Exception):
                pass
            if client in state._ws_clients:
                state._ws_clients.remove(client)

    # Serve the web UI static files (must be last — catches all unmatched routes)
    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        from fastapi.staticfiles import StaticFiles
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="ui")

    return app
