"""Global session state for nebo."""

from __future__ import annotations

import threading
import weakref
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Deque, Literal, Optional


# Number of recent text entries the SDK keeps per loggable for the
# terminal "Recent" panel. The daemon's `.nebo` file is the source of
# truth for the full text history; the SDK only mirrors a small tail
# to render locally.
RECENT_TEXTS_MAXLEN = 200

# Cap on return-origin entries that must hold STRONG references (values
# whose type doesn't support weakrefs: list/dict/tuple/str/...). Beyond
# this, the oldest strong entries are evicted — DAG inference only needs
# values that flow between calls soon after being returned, so a bounded
# recency window is semantically fine and stops the tracker from pinning
# user data for the whole run. Weakrefable values (ndarrays, tensors,
# custom objects — the big-memory cases) are never pinned at all.
RETURN_ORIGINS_MAX = 4096

# Sweep cadence for dead weakref entries (every N inserts).
_ORIGIN_SWEEP_EVERY = 1024


def _identity_is_provenance(value: Any) -> bool:
    """Whether seeing ``value`` again (by ``is``) implies this node made it.

    Origin tracking equates object identity with provenance. CPython
    interns/caches some immutables — bools, ints in [-5, 256], 0- and
    1-char strings/bytes, the empty tuple — making them program-wide
    singletons, so identity on them carries no provenance: a node that
    returns ``{"count": 7}`` did not produce every later ``7`` (e.g. a
    step counter), and tracking it manufactures false data-flow edges,
    including cycles and self-edges. Such values are never registered.
    (Multi-char string literals can also be interned per code object;
    that residual risk is accepted — excluding all strings would drop
    real edges like a generated text flowing into a consumer.)
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return not (-5 <= value <= 256)
    if isinstance(value, (str, bytes)):
        return len(value) > 1
    if isinstance(value, tuple):
        return len(value) > 0
    return True


@dataclass
class MetricCursor:
    """Tiny per-(loggable, metric-name) state kept on the SDK.

    The SDK no longer mirrors the full metric series in memory — that
    job belongs to the daemon. The cursor keeps just enough to
    enforce the per-(loggable, name) chart-type lock and to assign
    auto-step values for line metrics.
    """
    type: str
    next_step: int = 0


@dataclass
class LoggableInfo:
    """Base class for any entity that the terminal renders.

    The SDK keeps the bare minimum the local terminal display needs
    (recent text entries, progress). Metric values, image metadata,
    and audio metadata are no longer mirrored — those flow straight
    to the daemon, which persists them in the `.nebo` file.
    """
    loggable_id: str = ""
    kind: Literal["node", "global", "agent"] = "node"
    texts: Deque[dict] = field(default_factory=lambda: deque(maxlen=RECENT_TEXTS_MAXLEN))
    progress: Optional[dict] = None


@dataclass
class NodeInfo(LoggableInfo):
    """A loggable that is also a DAG node — produced by @nb.fn()."""
    name: str = ""  # mirrors loggable_id; kept for terminal display strings
    func_name: str = ""
    docstring: Optional[str] = None
    exec_count: int = 0
    is_source: bool = True
    params: dict = field(default_factory=dict)
    materialized: bool = False
    group: Optional[str] = None
    ui_hints: Optional[dict] = None
    kind: Literal["node", "global", "agent"] = "node"


@dataclass
class GlobalInfo(LoggableInfo):
    """The single process-wide loggable catching logs outside any @fn context."""
    kind: Literal["node", "global", "agent"] = "global"


@dataclass
class AgentInfo(LoggableInfo):
    """Sandbox loggable for entries authored by an external agent via MCP.

    Parallel to GlobalInfo, but namespaced separately so agent-computed
    metrics don't intermix with user-emitted ones routed to __global__.
    """
    kind: Literal["node", "global", "agent"] = "agent"


@dataclass
class DAGEdge:
    """An edge in the DAG."""

    source: str
    target: str


@dataclass
class _RunSnapshot:
    """Snapshot of per-run state fields for save/restore across runs."""
    loggables: dict
    edges: list
    edge_set: set
    return_origins: dict
    node_parents: dict
    workflow_description: Optional[str]
    ui_config: Optional[dict]
    linear_last: Optional[str]
    metric_cursors: dict


# Events that *describe* a run rather than carry run data. A run whose
# transport has only ever seen these is "virgin" — start_run upgrades it
# in place instead of opening a sibling. Distinct from
# `nebo/core/client.py:STRUCTURAL_TYPES` (the backpressure whitelist,
# which also includes edge/node_executed — those are real run data here);
# do not merge the two.
IDENTITY_EVENT_TYPES = frozenset({
    "run_start", "run_completed", "description", "ui_config",
    "run_config", "loggable_register",
})


class SessionState:
    """Global singleton managing all nebo state."""

    _instance: Optional[SessionState] = None
    _lock = threading.Lock()

    def __new__(cls) -> SessionState:
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        self.loggables: dict[str, LoggableInfo] = {}
        # Seed the global loggable so logs emitted outside any @fn context
        # have a home even before the first run_start / clear_run_state.
        self.loggables["__global__"] = GlobalInfo(
            loggable_id="__global__", kind="global"
        )
        # Seed the agent loggable so MCP-driven writes from external
        # agents have a stable, namespaced home distinct from __global__.
        self.loggables["__agent__"] = AgentInfo(
            loggable_id="__agent__", kind="agent"
        )
        self.edges: list[DAGEdge] = []
        self._edge_set: set[tuple[str, str]] = set()
        # Per-loggable, per-metric-name cursor for type-lock + auto-step.
        # Replaces the old loggable.metrics["entries"] mirror.
        self._metric_cursors: dict[str, dict[str, MetricCursor]] = {}
        self.workflow_description: Optional[str] = None
        self.port: int = 7861
        self.server_process: Any = None
        self._transport: Any = None  # Transport instance (file or network)
        self._mode: str = "file"  # "file" or "network"
        # id(value) -> (node_id, ref, is_weak, seq). See _store_origin.
        self._return_origins: dict[int, tuple[str, Any, bool, int]] = {}
        self._strong_origin_order: Deque[tuple[int, int]] = deque()
        self._origin_seq: int = 0
        self._node_parents: dict[str, Optional[str]] = {}  # node_id -> parent node_id
        # For dag_strategy="linear": id of the most recent first-execution node,
        # used to chain newly-encountered nodes regardless of actual data flow.
        self._linear_last: Optional[str] = None
        self.dag_strategy: str = "object"
        self._lock_state = threading.Lock()
        self.ui_config: Optional[dict] = None  # Run-level UI defaults
        # Multi-run support
        self._run_snapshots: dict[str, _RunSnapshot] = {}
        self._active_run_id: Optional[str] = None
        # Webhook alerts (configured via nb.init)
        self.webhook_url: Optional[str] = None
        self.webhook_min_level: int = 20  # AlertLevel.INFO
        # Stashed by nb.init() for deferred consumption by _ensure_run /
        # _create_run_transport. Cleared after the first run is materialized.
        # `_pending_transport` carries the eagerly-built NetworkTransport
        # (network mode only — file-mode transports stay lazy).
        self._pending_mode: Any = None  # nebo.core.uri.Mode | None
        self._pending_dest: str = ""
        self._pending_flush_interval: float = 0.1
        self._pending_api_token: Optional[str] = None
        self._pending_run_id: Optional[str] = None
        self._pending_group: str = ""  # run tree group from nb.init(group=)
        self._pending_transport: Any = None
        # Flips True once a run's transport has been opened (or skipped
        # under NEBO_NO_STORE in file mode). Guards _ensure_run from
        # re-firing on every subsequent emit.
        self._run_materialized: bool = False
        # Script-level metadata template: nb.md()/nb.ui() called OUTSIDE a
        # live run land here instead of materializing one, and are applied
        # to every run this process materializes (never cleared on run
        # roll — only reset() clears them; resume never re-applies).
        self._script_description: Optional[str] = None
        self._script_ui_config: Optional[dict] = None
        # Origin of the current run ("implicit" via _ensure_run, "explicit"
        # via start_run) and whether it has carried any non-identity event —
        # together they let start_run adopt a virgin implicit run in place
        # instead of opening a sibling.
        self._run_origin: Optional[str] = None
        self._run_has_real_events: bool = False

    def run_is_live(self) -> bool:
        """True once a run is materialized. A pre-attached transport (e.g.
        tests' capturing_client fixture) counts as live, mirroring
        `_ensure_run`'s accommodation of it."""
        return self._run_materialized or self._transport is not None

    def _send_to_client(self, event: dict) -> None:
        """Forward an event to the active transport (file or network)."""
        if self._transport is not None:
            if event.get("type") not in IDENTITY_EVENT_TYPES:
                self._run_has_real_events = True
            try:
                self._transport.send_event(event)
            except Exception:
                pass

    def register_node(
        self,
        node_id: str,
        func_name: str,
        docstring: Optional[str] = None,
        group: Optional[str] = None,
        ui_hints: Optional[dict] = None,
    ) -> NodeInfo:
        """Register a new node locally but do NOT send loggable_register event.

        The node stays unmaterialized until ensure_loggable() is called
        (triggered by the first log/metric/image/audio/text call).
        """
        with self._lock_state:
            existing = self.loggables.get(node_id)
            if existing is None or not isinstance(existing, NodeInfo):
                self.loggables[node_id] = NodeInfo(
                    loggable_id=node_id,
                    name=node_id,
                    func_name=func_name,
                    docstring=docstring,
                    group=group,
                    ui_hints=ui_hints,
                )
            elif group is not None and existing.group is None:
                existing.group = group
        return self.loggables[node_id]  # type: ignore[return-value]

    def ensure_loggable(self, loggable_id: str) -> None:
        """Materialize a loggable and emit its register event if it's a node.

        Called by the ``@nb.fn`` wrapper as soon as a decorated function
        starts executing, so every executed node appears in the graph
        regardless of whether it calls a log function. Also called
        defensively by the log/metric/image/audio/text paths so that
        logging from an already-executing node is a no-op on the
        already-materialized node (idempotent).

        For global-kind loggables this is a no-op — the global loggable
        is seeded at state init and has no materialize event.
        """
        node = self.loggables.get(loggable_id)
        if node is None or not isinstance(node, NodeInfo) or node.materialized:
            return
        node.materialized = True
        self._send_to_client({
            "type": "loggable_register",
            "loggable_id": loggable_id,
            "data": {
                "loggable_id": loggable_id,
                "kind": node.kind,
                "func_name": node.func_name,
                "docstring": node.docstring,
                "group": node.group,
                "ui_hints": node.ui_hints,
            },
        })

    def get_loggable(self, loggable_id: str) -> Optional[LoggableInfo]:
        """Return the loggable with the given id, or None if not present."""
        return self.loggables.get(loggable_id)

    def add_edge(self, source: str, target: str) -> None:
        """Add a DAG edge. Marks target as non-source."""
        added = False
        with self._lock_state:
            key = (source, target)
            if key not in self._edge_set:
                self._edge_set.add(key)
                self.edges.append(DAGEdge(source=source, target=target))
                target_node = self.loggables.get(target)
                if isinstance(target_node, NodeInfo):
                    target_node.is_source = False
                added = True
        if added:
            self._send_to_client({
                "type": "edge",
                "data": {"source": source, "target": target},
            })

    def _store_origin(self, node_id: str, value: Any) -> None:
        """Record one value's producer without pinning it in memory.

        Weakrefable values (arrays, tensors, custom objects) are stored
        via weakref — the tracker never keeps them alive. Builtin
        containers/scalars don't support weakrefs and keep a strong ref
        inside a bounded recency window (RETURN_ORIGINS_MAX). Interned
        singletons are skipped — identity on them is not provenance
        (see _identity_is_provenance). Caller holds _lock_state.
        """
        if not _identity_is_provenance(value):
            return
        try:
            ref: Any = weakref.ref(value)
            is_weak = True
        except TypeError:
            ref = value
            is_weak = False
        self._origin_seq += 1
        self._return_origins[id(value)] = (node_id, ref, is_weak, self._origin_seq)
        if not is_weak:
            self._strong_origin_order.append((id(value), self._origin_seq))
            while len(self._strong_origin_order) > RETURN_ORIGINS_MAX:
                old_id, old_seq = self._strong_origin_order.popleft()
                entry = self._return_origins.get(old_id)
                # Only evict if the slot still holds THIS entry (ids are
                # reused after gc; a newer entry must survive).
                if entry is not None and not entry[2] and entry[3] == old_seq:
                    del self._return_origins[old_id]
        if self._origin_seq % _ORIGIN_SWEEP_EVERY == 0:
            dead = [
                key for key, entry in self._return_origins.items()
                if entry[2] and entry[1]() is None
            ]
            for key in dead:
                del self._return_origins[key]

    def track_return(self, node_id: str, value: Any) -> None:
        """Record that a return value was produced by a given node.

        Tracks id(value) and, for tuples/lists/dicts, also tracks
        id(element) for each element one level deep. Skips None and
        interned singletons (bools, small ints, 0/1-char strings, the
        empty tuple) — their identity carries no provenance.

        Entries store a weakref where the type supports it (so the
        tracker never extends user data's lifetime) or a strong ref in
        a bounded LRU otherwise; find_producers verifies identity to
        guard against id() reuse after garbage collection.
        """
        if value is None:
            return
        with self._lock_state:
            self._store_origin(node_id, value)
            if isinstance(value, (tuple, list)):
                for item in value:
                    if item is not None:
                        self._store_origin(node_id, item)
            elif isinstance(value, dict):
                for v in value.values():
                    if v is not None:
                        self._store_origin(node_id, v)

    def _origin_for(self, arg: Any) -> Optional[str]:
        """Producer node_id for one argument, or None. Caller holds lock."""
        entry = self._return_origins.get(id(arg))
        if entry is None:
            return None
        node_id, ref, is_weak, _seq = entry
        target = ref() if is_weak else ref
        if target is None or target is not arg:
            return None
        return node_id

    def find_producers(
        self, args: tuple, kwargs: dict, parent: Optional[str] = None,
    ) -> set[str]:
        """Find which sibling steps produced the given arguments.

        Checks id() of each arg/kwarg against _return_origins, then verifies
        with an identity check (``is``) to guard against id() reuse after
        garbage collection (dead weakrefs are safe misses).

        Only returns producers that are **siblings** of the current node
        (share the same *parent*).  This ensures data-flow edges are short:
        an object creates one edge per hop and does not skip levels.

        Returns:
            Set of node_id strings for sibling steps that produced any
            of the arguments.
        """
        producers: set[str] = set()
        with self._lock_state:
            for arg in args:
                producer_id = self._origin_for(arg)
                if producer_id is not None and (
                    self._node_parents.get(producer_id) == parent
                ):
                    producers.add(producer_id)
            for v in kwargs.values():
                producer_id = self._origin_for(v)
                if producer_id is not None and (
                    self._node_parents.get(producer_id) == parent
                ):
                    producers.add(producer_id)
        return producers

    def increment_count(self, node_id: str) -> None:
        """Increment the execution count for a node."""
        with self._lock_state:
            node = self.loggables.get(node_id)
            if isinstance(node, NodeInfo):
                node.exec_count += 1
        self._send_to_client({
            "type": "node_executed",
            "loggable_id": node_id,
            "data": {"loggable_id": node_id},
        })

    def get_sources(self) -> list[str]:
        """Return all nodes with in-degree 0."""
        return [
            lid for lid, l in self.loggables.items()
            if isinstance(l, NodeInfo) and l.is_source
        ]

    def get_graph_dict(self) -> dict:
        """Return the graph as a serializable dictionary."""
        return {
            "nodes": {
                lid: {
                    "name": l.name,
                    "func_name": l.func_name,
                    "docstring": l.docstring,
                    "exec_count": l.exec_count,
                    "is_source": l.is_source,
                    "params": l.params,
                    "progress": l.progress,
                    "group": l.group,
                    "ui_hints": l.ui_hints,
                }
                for lid, l in self.loggables.items()
                if isinstance(l, NodeInfo) and l.materialized
            },
            "edges": [{"source": e.source, "target": e.target} for e in self.edges],
            "workflow_description": self.workflow_description,
        }

    def save_run_state(self, run_id: str) -> None:
        """Snapshot current per-run fields into _run_snapshots[run_id]."""
        with self._lock_state:
            self._run_snapshots[run_id] = _RunSnapshot(
                loggables=dict(self.loggables),
                edges=list(self.edges),
                edge_set=set(self._edge_set),
                return_origins=dict(self._return_origins),
                node_parents=dict(self._node_parents),
                workflow_description=self.workflow_description,
                ui_config=self.ui_config,
                linear_last=self._linear_last,
                metric_cursors={
                    lid: dict(cursors)
                    for lid, cursors in self._metric_cursors.items()
                },
            )

    def restore_run_state(self, run_id: str) -> None:
        """Restore per-run fields from a snapshot."""
        snap = self._run_snapshots.get(run_id)
        if snap is None:
            self.clear_run_state()
            return
        with self._lock_state:
            self.loggables = dict(snap.loggables)
            self.edges = list(snap.edges)
            self._edge_set = set(snap.edge_set)
            self._return_origins = dict(snap.return_origins)
            # Rebuild strong-entry eviction order from the restored dict
            # (insertion order == seq order).
            self._strong_origin_order = deque(
                (key, entry[3])
                for key, entry in self._return_origins.items()
                if not entry[2]
            )
            self._node_parents = dict(snap.node_parents)
            self.workflow_description = snap.workflow_description
            self.ui_config = snap.ui_config
            self._linear_last = snap.linear_last
            self._metric_cursors = {
                lid: dict(cursors)
                for lid, cursors in snap.metric_cursors.items()
            }

    def clear_run_state(self) -> None:
        """Reset per-run fields to empty (for new runs)."""
        with self._lock_state:
            self.loggables.clear()
            self.loggables["__global__"] = GlobalInfo(
                loggable_id="__global__", kind="global"
            )
            self.loggables["__agent__"] = AgentInfo(
                loggable_id="__agent__", kind="agent"
            )
            self.edges.clear()
            self._edge_set.clear()
            self._return_origins.clear()
            self._strong_origin_order.clear()
            self._node_parents.clear()
            self._metric_cursors.clear()
            self.workflow_description = None
            self.ui_config = None
            self._linear_last = None
            # _script_description/_script_ui_config are deliberately NOT
            # cleared: the template is per-process, not per-run — every
            # run the script opens gets it. Only reset() clears it.

    def reset(self) -> None:
        """Reset all state. Primarily for testing."""
        with self._lock_state:
            self.loggables.clear()
            self.loggables["__global__"] = GlobalInfo(
                loggable_id="__global__", kind="global"
            )
            self.loggables["__agent__"] = AgentInfo(
                loggable_id="__agent__", kind="agent"
            )
            self.edges.clear()
            self._edge_set.clear()
            self._return_origins.clear()
            self._strong_origin_order.clear()
            self._node_parents.clear()
            self._metric_cursors.clear()
            self._linear_last = None
            self.dag_strategy = "object"
            self.workflow_description = None
            self._transport = None
            self._mode = "file"
            self.ui_config = None
            self._run_snapshots.clear()
            self._active_run_id = None
            self._pending_mode = None
            self._pending_dest = ""
            self._pending_flush_interval = 0.1
            self._pending_api_token = None
            self._pending_run_id = None
            self._pending_group = ""
            self._pending_transport = None
            self._run_materialized = False
            self._script_description = None
            self._script_ui_config = None
            self._run_origin = None
            self._run_has_real_events = False

    @classmethod
    def reset_singleton(cls) -> None:
        """Completely reset the singleton. For testing only."""
        with cls._lock:
            if cls._instance is not None:
                cls._instance.reset()
                cls._instance._initialized = False
                cls._instance = None


# ContextVar for tracking current executing node
_current_node: ContextVar[Optional[str]] = ContextVar("current_node", default=None)

# ContextVar for tracking current class group context
_current_group: ContextVar[Optional[str]] = ContextVar("current_group", default=None)


def get_state() -> SessionState:
    """Get the global session state."""
    return SessionState()
