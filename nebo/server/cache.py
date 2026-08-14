"""Daemon-internal SQLite cache — disposable, rebuildable, write-behind.

`.nebo` files remain the sole source of truth; this cache is derived
state. Deleting the database never loses data that exists in a `.nebo`
file (runs on a `--remote-ephemeral` daemon are the documented exception —
they live only in RAM + this cache, ephemeral by user choice).

Design:
  * Ingest keeps mutating RAM synchronously (the hot path is untouched)
    and additionally enqueues typed ops here. A single background writer
    thread drains the queue and applies ops in one transaction per batch
    (WAL journal, synchronous=NORMAL).
  * Reads open thread-local connections; WAL allows concurrent readers
    alongside the writer.
  * `flush()` is a barrier: it returns once every op enqueued before it
    has been committed.

The cache lives at ``~/.nebo/cache/<sha1(logdir)[:16]>.db`` (see
`resolve_cache_path`). `meta` rows record the schema version and the
logdir the db was built for; a mismatch on open drops and recreates the
database (it is a cache — rebuild is always safe).

Single ownership: `start()` takes an exclusive flock on ``<db>.lock`` and
raises `CacheLockedError` if another live process holds it — two daemons
sharing one cache would duplicate history rows and clobber each other's
watcher offsets. The kernel releases the flock on process death, so a
crashed daemon never blocks a restart. As defense-in-depth, the history
tables (texts, metrics, media, alerts, significant_events) carry unique
indexes over one event's identity and insert with OR IGNORE, so re-ingest
of already-cached events is a no-op.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from nebo.server.workspace import normalize_workspace, read_frame_bytes

try:
    import fcntl
except ImportError:  # Windows: no flock — the single-owner guard degrades
    fcntl = None      # to best-effort (SQLite WAL still serializes writes).

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "7"  # v7: watch_files.token (content marker for replacements)

DEFAULT_RAM_BUDGET_MB = 384
BYTES_PER_POINT = 372  # measured: dict-per-point daemon entry overhead
DEFAULT_MEDIA_LRU_MB = 256
DEFAULT_RETENTION_DAYS = 30

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY, script_path TEXT, run_name TEXT, args_json TEXT,
  started_at REAL, last_event_at REAL, source TEXT,
  workflow_description TEXT, config_json TEXT, ui_config_json TEXT,
  run_config_json TEXT, edges_json TEXT
);
CREATE TABLE loggables (
  run_id TEXT, loggable_id TEXT, kind TEXT, func_name TEXT, docstring TEXT,
  grp TEXT, ui_hints_json TEXT, params_json TEXT,
  exec_count INTEGER DEFAULT 0, is_source INTEGER DEFAULT 1, progress_json TEXT,
  PRIMARY KEY (run_id, loggable_id)
);
CREATE TABLE metrics (
  run_id TEXT, loggable_id TEXT, name TEXT, metric_type TEXT,
  step INTEGER, ts REAL, value_json TEXT, tags_json TEXT, colors INTEGER
);
CREATE INDEX idx_metrics ON metrics(run_id, loggable_id, name, step);
CREATE TABLE texts (
  run_id TEXT, loggable_id TEXT, name TEXT, ts REAL, step INTEGER,
  message TEXT
);
CREATE INDEX idx_texts ON texts(run_id, ts);
CREATE TABLE alerts (run_id TEXT, ts REAL, json TEXT);
CREATE TABLE significant_events (run_id TEXT, ts REAL, type TEXT, json TEXT);
CREATE TABLE media (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT, loggable_id TEXT, media_id TEXT, kind TEXT,
  name TEXT, step INTEGER, ts REAL, sr INTEGER, labels_json TEXT,
  src_path TEXT, src_offset INTEGER, src_length INTEGER
);
CREATE INDEX idx_media_run ON media(run_id, loggable_id);
CREATE INDEX idx_media_mid ON media(media_id);

-- Re-ingest idempotence. History tables pair with INSERT OR IGNORE so
-- replaying events that are already cached (a re-scanned file, a replayed
-- wire batch) is a no-op, matching the upsert semantics of runs/loggables
-- and the content-addressed media_blobs. Keys cover one event's identity;
-- NULLs are distinct in SQLite unique indexes, so nullable columns go
-- through COALESCE sentinels.
CREATE UNIQUE INDEX ux_texts_row ON texts(
  run_id, COALESCE(loggable_id, ''), name, ts, COALESCE(step, -1),
  message
);
CREATE UNIQUE INDEX ux_metrics_row ON metrics(
  run_id, loggable_id, name, COALESCE(step, -1), COALESCE(ts, -1), value_json
);
CREATE UNIQUE INDEX ux_media_row ON media(
  run_id, loggable_id, kind, media_id, name, COALESCE(step, -1),
  COALESCE(ts, -1)
);
CREATE UNIQUE INDEX ux_alerts_row ON alerts(run_id, ts, json);
CREATE UNIQUE INDEX ux_sig_events_row ON significant_events(
  run_id, ts, type, json
);
CREATE TABLE media_blobs (media_id TEXT PRIMARY KEY, blob BLOB);
CREATE TABLE watch_files (
  path TEXT PRIMARY KEY, run_id TEXT, offset INTEGER, size INTEGER, mtime REAL,
  shallow INTEGER NOT NULL DEFAULT 0, token TEXT
);
"""


def resolve_cache_path(logdir: Optional[Path | str]) -> Path:
    """Cache db path for a logdir: ~/.nebo/cache/<sha1(workspace key)[:16]>.db.

    The key comes from `normalize_workspace` — an absolute path for a local
    logdir, the canonical URI for a bucket. `RunCache` must derive its stored
    `meta.logdir` the same way or `_meta_matches` drops the whole database.
    """
    key = normalize_workspace(logdir)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return Path.home() / ".nebo" / "cache" / f"{digest}.db"


class CacheLockedError(RuntimeError):
    """Another live process holds this cache database.

    Two daemons sharing one cache corrupt derived state (duplicate history
    rows, clobbered watcher offsets), so the second opener fails fast."""

    def __init__(self, path: Path | str, holder: Optional[dict] = None) -> None:
        self.path = Path(path)
        self.holder = holder or {}
        detail = ""
        if self.holder.get("pid"):
            detail = f" (pid {self.holder['pid']})"
        super().__init__(
            f"cache database {path} is already in use by another nebo "
            f"daemon{detail}. Two daemons must not share a logdir — stop the "
            "other daemon, or serve a different --logdir/--cache-path."
        )


def _lock_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".lock")


def cache_lock_holder(db_path: Path | str) -> Optional[dict]:
    """Probe a cache db's single-owner lock without taking ownership.

    Returns the holder-info dict ({} if unreadable) when a live process
    holds the lock, else None. Advisory only — the authoritative check is
    `RunCache.start()`, which raises `CacheLockedError`; this exists so
    `nebo serve` can fail with a friendly message before spawning uvicorn.
    """
    if fcntl is None:
        return None
    try:
        f = open(_lock_path(Path(db_path)), "r")
    except OSError:
        return None
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            try:
                return json.loads(f.read() or "{}") or {}
            except ValueError:
                return {}
        fcntl.flock(f, fcntl.LOCK_UN)
        return None
    finally:
        f.close()


def sweep_cache_dir(
    cache_dir: Path, retention_days: int, *, now: Optional[float] = None,
) -> list[Path]:
    """Delete cache dbs whose mtime is older than the retention window.

    A db's mtime advances whenever its daemon runs, so a stale mtime
    means no daemon has served that logdir within the window. Returns
    the deleted paths. Safe by the disposability invariant.
    """
    if now is None:
        now = time.time()
    cutoff = now - retention_days * 86400
    deleted: list[Path] = []
    try:
        entries = list(Path(cache_dir).glob("*.db"))
    except OSError:
        return deleted
    if not Path(cache_dir).is_dir():
        return deleted
    for path in entries:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                for suffix in ("-wal", "-shm", ".lock"):
                    side = path.with_name(path.name + suffix)
                    if side.exists():
                        side.unlink()
                deleted.append(path)
        except OSError:
            continue
    return deleted


def media_id_for(data: bytes) -> str:
    """Content-addressed media id: stable across daemon restarts and cache
    rebuilds (enables dedup and immutable HTTP caching)."""
    return hashlib.sha256(data).hexdigest()[:16]


class MediaLRU:
    """Byte-budgeted LRU for decoded media blobs."""

    def __init__(self, budget_bytes: int) -> None:
        from collections import OrderedDict

        self._budget = budget_bytes
        self._size = 0
        self._items: "OrderedDict[str, bytes]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, media_id: str) -> Optional[bytes]:
        with self._lock:
            data = self._items.get(media_id)
            if data is not None:
                self._items.move_to_end(media_id)
            return data

    def put(self, media_id: str, data: bytes) -> None:
        with self._lock:
            old = self._items.pop(media_id, None)
            if old is not None:
                self._size -= len(old)
            self._items[media_id] = data
            self._size += len(data)
            while self._size > self._budget and len(self._items) > 1:
                _, evicted = self._items.popitem(last=False)
                self._size -= len(evicted)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


class RunCache:
    """Write-behind SQLite store for daemon run state."""

    def __init__(
        self,
        path: Path | str,
        logdir: Optional[Path | str] = None,
        media_lru_mb: int = DEFAULT_MEDIA_LRU_MB,
    ) -> None:
        self._path = Path(path)
        # Must match resolve_cache_path's key exactly — `_meta_matches` drops
        # and recreates the database when the stored logdir differs.
        self._logdir = normalize_workspace(logdir)
        self._media_lru_mb = media_lru_mb
        self._queue: queue.Queue[Any] = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._local = threading.local()
        self._lock_file: Optional[Any] = None
        self.media_lru = MediaLRU(media_lru_mb * 1024 * 1024)

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        """Open (or recreate) the database and start the writer thread.

        Raises `CacheLockedError` if another live process owns this db."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._acquire_lock()
        if self._path.exists() and not self._meta_matches():
            self._delete_db_files()
        if not self._path.exists():
            self._create_db()
        self._running = True
        self._thread = threading.Thread(
            target=self._writer_loop, daemon=True, name="nebo-cache-writer"
        )
        self._thread.start()

    def close(self) -> None:
        """Flush pending ops, stop the writer thread, close connections."""
        if self._running:
            self._running = False
            self._queue.put(None)  # poison pill; writer drains the rest first
            if self._thread is not None:
                self._thread.join(timeout=10.0)
            conn = getattr(self._local, "conn", None)
            if conn is not None:
                conn.close()
                self._local.conn = None
        self._release_lock()

    def _acquire_lock(self) -> None:
        """Take the single-owner flock for this db, failing fast if held.

        The kernel drops a flock when its holder dies, so a crashed daemon
        never wedges a restart — the lockfile itself is inert; only the
        held lock matters. Holder info (pid, logdir) is written into the
        file purely for the error message on the losing side. No-op where
        flock is unavailable (Windows)."""
        if fcntl is None or self._lock_file is not None:
            return
        lock_file = open(_lock_path(self._path), "a+")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_file.seek(0)
            try:
                holder = json.loads(lock_file.read() or "{}")
            except ValueError:
                holder = {}
            lock_file.close()
            raise CacheLockedError(self._path, holder) from None
        lock_file.seek(0)
        lock_file.truncate()
        json.dump(
            {"pid": os.getpid(), "logdir": self._logdir,
             "acquired_at": time.time()},
            lock_file,
        )
        lock_file.flush()
        self._lock_file = lock_file

    def _release_lock(self) -> None:
        if self._lock_file is None:
            return
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_UN)
        except OSError:
            pass
        self._lock_file.close()
        self._lock_file = None

    def _meta_matches(self) -> bool:
        try:
            conn = sqlite3.connect(self._path)
            try:
                rows = dict(
                    conn.execute("SELECT key, value FROM meta").fetchall()
                )
            finally:
                conn.close()
        except sqlite3.Error:
            return False
        return (
            rows.get("schema_version") == SCHEMA_VERSION
            and rows.get("logdir") == self._logdir
        )

    def _delete_db_files(self) -> None:
        for path in (
            self._path,
            self._path.with_name(self._path.name + "-wal"),
            self._path.with_name(self._path.name + "-shm"),
        ):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def _create_db(self) -> None:
        conn = sqlite3.connect(self._path)
        try:
            # auto_vacuum must be set before any table exists.
            conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('logdir', ?)",
                (self._logdir,),
            )
            conn.commit()
        finally:
            conn.close()

    # -- write-behind --------------------------------------------------

    def enqueue(self, op: tuple) -> None:
        """Queue a typed op for the writer thread. Thread-safe, non-blocking."""
        if self._running:
            self._queue.put(op)

    def flush(self, timeout: float = 5.0) -> bool:
        """Barrier: block until every previously-enqueued op is committed."""
        if not self._running:
            return True
        event = threading.Event()
        self._queue.put(("__barrier__", event))
        return event.wait(timeout)

    def incremental_vacuum(self) -> None:
        """Reclaim freed pages opportunistically (janitor calls this)."""
        self.enqueue(("__vacuum__",))

    def _writer_loop(self) -> None:
        conn = _connect(self._path)
        try:
            while True:
                try:
                    first = self._queue.get(timeout=0.25)
                except queue.Empty:
                    if not self._running:
                        break
                    continue
                batch: list[Any] = [first]
                while True:
                    try:
                        batch.append(self._queue.get_nowait())
                    except queue.Empty:
                        break
                stop = self._apply_batch(conn, batch)
                if stop:
                    break
        finally:
            conn.commit()
            conn.close()

    def _apply_batch(self, conn: sqlite3.Connection, batch: list[Any]) -> bool:
        """Apply ops in order inside one transaction. Returns True on poison pill."""
        stop = False
        barriers: list[threading.Event] = []
        for op in batch:
            if op is None:
                stop = True
                continue  # keep draining ops that were queued before close()
            kind = op[0]
            if kind == "__barrier__":
                conn.commit()
                for ev in barriers:
                    ev.set()
                barriers = []
                op[1].set()
                continue
            if kind == "__vacuum__":
                conn.commit()
                try:
                    conn.execute("PRAGMA incremental_vacuum")
                except sqlite3.Error:
                    pass
                continue
            try:
                self._apply_op(conn, op)
            except Exception:
                logger.warning("cache: dropping bad op %r", op[:1], exc_info=True)
        conn.commit()
        for ev in barriers:
            ev.set()
        return stop

    # Columns accepted by the two partial-upsert ops. Guards the dynamic
    # SQL below against arbitrary keys.
    _RUN_COLS = frozenset({
        "script_path", "run_name", "args_json", "started_at", "last_event_at",
        "source", "workflow_description", "config_json", "ui_config_json",
        "run_config_json", "edges_json",
    })
    _LOGGABLE_COLS = frozenset({
        "kind", "func_name", "docstring", "grp", "ui_hints_json",
        "params_json", "exec_count", "is_source", "progress_json",
    })

    def _apply_op(self, conn: sqlite3.Connection, op: tuple) -> None:
        # History-row inserts are OR IGNORE against the ux_* unique indexes:
        # a row identical to one already cached is a re-ingest by
        # construction (re-scanned file, replayed batch), never new data.
        kind = op[0]
        if kind == "text_row":
            _, run_id, lid, name, ts, step, message = op
            conn.execute(
                "INSERT OR IGNORE INTO texts"
                " (run_id, loggable_id, name, ts, step, message)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, lid, name, ts, step, message),
            )
        elif kind == "metric_row":
            _, run_id, lid, name, mtype, step, ts, value_json, tags_json, colors = op
            conn.execute(
                "INSERT OR IGNORE INTO metrics (run_id, loggable_id, name,"
                " metric_type, step, ts, value_json, tags_json, colors)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, lid, name, mtype, step, ts, value_json, tags_json, colors),
            )
        elif kind == "metric_snapshot":
            _, run_id, lid, name, mtype, step, ts, value_json, tags_json, colors = op
            conn.execute(
                "DELETE FROM metrics WHERE run_id=? AND loggable_id=? AND name=?",
                (run_id, lid, name),
            )
            conn.execute(
                "INSERT OR IGNORE INTO metrics (run_id, loggable_id, name,"
                " metric_type, step, ts, value_json, tags_json, colors)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, lid, name, mtype, step, ts, value_json, tags_json, colors),
            )
        elif kind == "run_upsert":
            _, run_id, fields = op
            cols = [c for c in fields if c in self._RUN_COLS]
            conn.execute(
                "INSERT INTO runs (run_id) VALUES (?)"
                " ON CONFLICT(run_id) DO NOTHING",
                (run_id,),
            )
            if cols:
                sets = ", ".join(f"{c}=?" for c in cols)
                conn.execute(
                    f"UPDATE runs SET {sets} WHERE run_id=?",
                    [fields[c] for c in cols] + [run_id],
                )
        elif kind == "loggable_upsert":
            _, run_id, lid, fields = op
            cols = [c for c in fields if c in self._LOGGABLE_COLS]
            conn.execute(
                "INSERT INTO loggables (run_id, loggable_id) VALUES (?, ?)"
                " ON CONFLICT(run_id, loggable_id) DO NOTHING",
                (run_id, lid),
            )
            if cols:
                sets = ", ".join(f"{c}=?" for c in cols)
                conn.execute(
                    f"UPDATE loggables SET {sets} WHERE run_id=? AND loggable_id=?",
                    [fields[c] for c in cols] + [run_id, lid],
                )
        elif kind == "alert_row":
            _, run_id, ts, json_str = op
            conn.execute(
                "INSERT OR IGNORE INTO alerts (run_id, ts, json) VALUES (?, ?, ?)",
                (run_id, ts, json_str),
            )
        elif kind == "sig_event":
            _, run_id, ts, etype, json_str = op
            conn.execute(
                "INSERT OR IGNORE INTO significant_events (run_id, ts, type, json)"
                " VALUES (?, ?, ?, ?)",
                (run_id, ts, etype, json_str),
            )
        elif kind == "media_occurrence":
            (_, run_id, lid, media_id, mkind, name, step, ts, sr,
             labels_json, src_path, src_offset, src_length) = op
            conn.execute(
                "INSERT OR IGNORE INTO media"
                " (run_id, loggable_id, media_id, kind, name,"
                " step, ts, sr, labels_json, src_path, src_offset, src_length)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, lid, media_id, mkind, name, step, ts, sr,
                 labels_json, src_path, src_offset, src_length),
            )
        elif kind == "media_blob":
            _, media_id, blob = op
            conn.execute(
                "INSERT OR IGNORE INTO media_blobs (media_id, blob) VALUES (?, ?)",
                (media_id, blob),
            )
        elif kind == "watch_file":
            _, path, run_id, offset, size, mtime, shallow, token = op
            conn.execute(
                "INSERT INTO watch_files"
                " (path, run_id, offset, size, mtime, shallow, token)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(path) DO UPDATE SET"
                " run_id=excluded.run_id, offset=excluded.offset,"
                " size=excluded.size, mtime=excluded.mtime,"
                " shallow=excluded.shallow, token=excluded.token",
                (path, run_id, offset, size, mtime, int(shallow), token),
            )
        elif kind == "watch_file_drop":
            # The file is gone from the workspace; forget its offset so a
            # path that reappears is registered from its header again.
            conn.execute("DELETE FROM watch_files WHERE path=?", (op[1],))
        else:
            raise ValueError(f"unknown cache op kind: {kind!r}")

    # -- reads ---------------------------------------------------------

    def _read_conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = _connect(self._path)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def has_run(self, run_id: str) -> bool:
        row = self._read_conn().execute(
            "SELECT 1 FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        return row is not None

    def run_ids(self) -> list[str]:
        return [
            r["run_id"]
            for r in self._read_conn().execute(
                "SELECT run_id FROM runs ORDER BY started_at"
            ).fetchall()
        ]

    def _run_row(self, run_id: str) -> Optional[sqlite3.Row]:
        return self._read_conn().execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()

    def run_recency(self, run_id: str) -> Optional[float]:
        """The run's ``last_event_at`` — heartbeat evaluation for runs
        that have been evicted from RAM."""
        row = self._run_row(run_id)
        return row["last_event_at"] if row is not None else None

    def get_summary(self, run_id: str) -> Optional[dict]:
        """Mirror of Run.get_summary() built from SQL."""
        import json
        from datetime import datetime

        row = self._run_row(run_id)
        if row is None:
            return None
        conn = self._read_conn()
        node_count = conn.execute(
            "SELECT COUNT(*) FROM loggables WHERE run_id=? AND kind='node'",
            (run_id,),
        ).fetchone()[0]
        text_count = conn.execute(
            "SELECT COUNT(*) FROM texts WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        series = conn.execute(
            "SELECT DISTINCT loggable_id, name FROM metrics WHERE run_id=?"
            " ORDER BY loggable_id, name",
            (run_id,),
        ).fetchall()
        metrics_index: dict[str, list[str]] = {}
        for s in series:
            metrics_index.setdefault(s["loggable_id"], []).append(s["name"])
        latest_step = conn.execute(
            "SELECT MAX(step) FROM metrics WHERE run_id=?"
            " AND metric_type IN ('line', 'scatter')",
            (run_id,),
        ).fetchone()[0]
        edges = json.loads(row["edges_json"]) if row["edges_json"] else []

        def _iso(epoch):
            return datetime.fromtimestamp(epoch).isoformat() if epoch else None

        return {
            "id": run_id,
            "script_path": row["script_path"],
            "args": json.loads(row["args_json"]) if row["args_json"] else [],
            "started_at": _iso(row["started_at"]),
            "last_event_at": row["last_event_at"],
            "node_count": node_count,
            "edge_count": len(edges),
            "text_count": text_count,
            "run_name": row["run_name"],
            "run_config": json.loads(row["run_config_json"]) if row["run_config_json"] else {},
            "metrics_index": metrics_index,
            "metric_series_count": len(series),
            "latest_step": latest_step,
        }

    def list_summaries(self) -> list[dict]:
        run_ids = [
            r["run_id"]
            for r in self._read_conn().execute(
                "SELECT run_id FROM runs ORDER BY started_at"
            ).fetchall()
        ]
        out = []
        for rid in run_ids:
            summary = self.get_summary(rid)
            if summary is not None:
                out.append(summary)
        return out

    def _loggable_rows(self, run_id: str) -> list[sqlite3.Row]:
        return self._read_conn().execute(
            "SELECT * FROM loggables WHERE run_id=?", (run_id,)
        ).fetchall()

    @staticmethod
    def _node_dict(row: sqlite3.Row) -> dict:
        import json

        return {
            "name": row["loggable_id"],
            "func_name": row["func_name"] or "",
            "docstring": row["docstring"],
            "exec_count": row["exec_count"] or 0,
            "is_source": bool(row["is_source"]),
            "params": json.loads(row["params_json"]) if row["params_json"] else {},
            "progress": json.loads(row["progress_json"]) if row["progress_json"] else None,
            "group": row["grp"],
            "ui_hints": json.loads(row["ui_hints_json"]) if row["ui_hints_json"] else None,
        }

    def get_graph(self, run_id: str) -> Optional[dict]:
        """Mirror of Run.get_graph() built from SQL."""
        import json

        row = self._run_row(run_id)
        if row is None:
            return None
        nodes = {
            lg["loggable_id"]: self._node_dict(lg)
            for lg in self._loggable_rows(run_id)
            if lg["kind"] == "node"
        }
        edges = json.loads(row["edges_json"]) if row["edges_json"] else []
        edges = [
            e for e in edges
            if e.get("source") in nodes and e.get("target") in nodes
        ]
        return {
            "nodes": nodes,
            "edges": edges,
            "workflow_description": row["workflow_description"],
            "ui_config": json.loads(row["ui_config_json"]) if row["ui_config_json"] else None,
            "run_config": json.loads(row["run_config_json"]) if row["run_config_json"] else {},
        }

    def get_texts(
        self, run_id: str, loggable_id: Optional[str] = None, limit: int = 100,
    ) -> list[dict]:
        conn = self._read_conn()
        if loggable_id:
            rows = conn.execute(
                "SELECT * FROM (SELECT rowid AS rid, * FROM texts"
                " WHERE run_id=? AND loggable_id=?"
                " ORDER BY rid DESC LIMIT ?) ORDER BY rid ASC",
                (run_id, loggable_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM (SELECT rowid AS rid, * FROM texts WHERE run_id=?"
                " ORDER BY rid DESC LIMIT ?) ORDER BY rid ASC",
                (run_id, limit),
            ).fetchall()
        return [
            {
                "timestamp": r["ts"],
                "loggable_id": r["loggable_id"],
                "name": r["name"],
                "message": r["message"],
                "step": r["step"],
            }
            for r in rows
        ]

    def _json_rows(self, table: str, run_id: str) -> list[dict]:
        import json

        rows = self._read_conn().execute(
            f"SELECT json FROM {table} WHERE run_id=? ORDER BY rowid", (run_id,)
        ).fetchall()
        return [json.loads(r["json"]) for r in rows]

    def get_alerts(self, run_id: str) -> list[dict]:
        return self._json_rows("alerts", run_id)

    def get_significant_events(self, run_id: str) -> list[dict]:
        import json

        rows = self._read_conn().execute(
            "SELECT json FROM significant_events WHERE run_id=? ORDER BY rowid",
            (run_id,),
        ).fetchall()
        return [json.loads(r["json"]) for r in rows]

    def _metrics_for(
        self, run_id: str, loggable_id: Optional[str] = None,
    ) -> dict[str, dict[str, dict]]:
        import json

        conn = self._read_conn()
        if loggable_id:
            rows = conn.execute(
                "SELECT * FROM metrics WHERE run_id=? AND loggable_id=? ORDER BY rowid",
                (run_id, loggable_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM metrics WHERE run_id=? ORDER BY rowid", (run_id,)
            ).fetchall()
        out: dict[str, dict[str, dict]] = {}
        for r in rows:
            series = out.setdefault(r["loggable_id"], {}).setdefault(
                r["name"], {"type": r["metric_type"], "entries": []}
            )
            entry: dict[str, Any] = {
                "step": r["step"],
                "value": json.loads(r["value_json"]) if r["value_json"] else None,
                "tags": json.loads(r["tags_json"]) if r["tags_json"] else [],
                "timestamp": r["ts"],
            }
            if r["colors"] is not None:
                entry["colors"] = bool(r["colors"])
            series["entries"].append(entry)
        return out

    def get_metrics(self, run_id: str) -> dict[str, dict[str, dict]]:
        return self._metrics_for(run_id)

    def get_loggable(self, run_id: str, loggable_id: str) -> Optional[dict]:
        """Mirror of the /runs/{id}/loggables/{lid} payload built from SQL."""
        import json

        row = self._read_conn().execute(
            "SELECT * FROM loggables WHERE run_id=? AND loggable_id=?",
            (run_id, loggable_id),
        ).fetchone()
        if row is None:
            return None
        metrics = self._metrics_for(run_id, loggable_id).get(loggable_id, {})
        return {
            "loggable_id": loggable_id,
            "kind": row["kind"] or "node",
            "func_name": row["func_name"] or "",
            "docstring": row["docstring"],
            "exec_count": row["exec_count"] or 0,
            "is_source": bool(row["is_source"]),
            "params": json.loads(row["params_json"]) if row["params_json"] else {},
            "recent_texts": self.get_texts(run_id, loggable_id=loggable_id, limit=20),
            "metrics": metrics,
            "progress": json.loads(row["progress_json"]) if row["progress_json"] else None,
        }

    def get_run_ingest_state(self, run_id: str) -> Optional[dict]:
        """Everything needed to rehydrate a Run's ingest-state (no points)."""
        import json

        row = self._run_row(run_id)
        if row is None:
            return None
        conn = self._read_conn()
        loggables: dict[str, dict] = {}
        for lg in self._loggable_rows(run_id):
            loggables[lg["loggable_id"]] = {
                "kind": lg["kind"] or "node",
                "func_name": lg["func_name"] or "",
                "docstring": lg["docstring"],
                "group": lg["grp"],
                "ui_hints": json.loads(lg["ui_hints_json"]) if lg["ui_hints_json"] else None,
                "params": json.loads(lg["params_json"]) if lg["params_json"] else {},
                "exec_count": lg["exec_count"] or 0,
                "is_source": bool(lg["is_source"]),
            }
        series_types: dict[str, dict[str, str]] = {}
        for r in conn.execute(
            "SELECT DISTINCT loggable_id, name, metric_type FROM metrics"
            " WHERE run_id=?",
            (run_id,),
        ).fetchall():
            series_types.setdefault(r["loggable_id"], {})[r["name"]] = r["metric_type"]
        latest_step = conn.execute(
            "SELECT MAX(step) FROM metrics WHERE run_id=?"
            " AND metric_type IN ('line', 'scatter')",
            (run_id,),
        ).fetchone()[0]
        counts = {
            "texts": conn.execute(
                "SELECT COUNT(*) FROM texts WHERE run_id=?", (run_id,)
            ).fetchone()[0],
            "metric_series": conn.execute(
                "SELECT COUNT(DISTINCT loggable_id || '/' || name) FROM metrics"
                " WHERE run_id=?",
                (run_id,),
            ).fetchone()[0],
        }
        return {
            "loggables": loggables,
            "series_types": series_types,
            "edges": json.loads(row["edges_json"]) if row["edges_json"] else [],
            "latest_step": latest_step,
            "counts": counts,
            "run_row": dict(row),
        }

    def list_media(self, run_id: str, kind: str) -> dict[str, list[dict]]:
        """Occurrence listing per loggable, in insertion order."""
        import json

        rows = self._read_conn().execute(
            "SELECT * FROM media WHERE run_id=? AND kind=? ORDER BY id",
            (run_id, kind),
        ).fetchall()
        out: dict[str, list[dict]] = {}
        for r in rows:
            item = {
                "loggable_id": r["loggable_id"],
                "media_id": r["media_id"],
                "name": r["name"] or "",
                "step": r["step"],
                "timestamp": r["ts"] or 0,
            }
            if kind == "image":
                item["labels"] = (
                    json.loads(r["labels_json"]) if r["labels_json"] else None
                )
            else:
                item["sr"] = r["sr"] if r["sr"] is not None else 16000
            out.setdefault(r["loggable_id"], []).append(item)
        return out

    def get_media(self, media_id: str) -> Optional[bytes]:
        """Resolve media bytes: LRU -> blob table -> .nebo file reference."""
        data = self.media_lru.get(media_id)
        if data is not None:
            return data
        conn = self._read_conn()
        row = conn.execute(
            "SELECT blob FROM media_blobs WHERE media_id=?", (media_id,)
        ).fetchone()
        if row is not None:
            data = bytes(row["blob"])
            self.media_lru.put(media_id, data)
            return data
        ref = conn.execute(
            "SELECT src_path, src_offset, src_length FROM media"
            " WHERE media_id=? AND src_path IS NOT NULL LIMIT 1",
            (media_id,),
        ).fetchone()
        if ref is None:
            return None
        data = self._read_media_ref(
            ref["src_path"], ref["src_offset"], ref["src_length"]
        )
        if data is not None:
            self.media_lru.put(media_id, data)
        return data

    @staticmethod
    def _read_media_ref(path: str, offset: int, length: int) -> Optional[bytes]:
        """Read one frame [type][u32 size][msgpack payload] from a .nebo file
        and extract its media bytes (base64 str in v3 files, bin in v4).

        `path` is a self-describing URI — an absolute path for a local
        workspace, an ``hf://`` URI for a bucket one — because the reference
        was persisted long ago and no workspace is in hand here.
        """
        import base64
        import struct as _struct

        import msgpack

        try:
            frame = read_frame_bytes(path, offset, length)
            if frame is None or len(frame) < 5:
                return None
            size = _struct.unpack(">I", frame[1:5])[0]
            payload = msgpack.unpackb(frame[5:5 + size], raw=False)
            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, bytes):
                return data
            if isinstance(data, str):
                return base64.b64decode(data)
        except Exception:
            logger.warning("cache: failed to read media ref %s@%d", path, offset)
        return None

    def get_watch_files(self) -> dict[str, dict]:
        rows = self._read_conn().execute("SELECT * FROM watch_files").fetchall()
        return {
            r["path"]: {
                "run_id": r["run_id"],
                "offset": r["offset"],
                "size": r["size"],
                "mtime": r["mtime"],
                "shallow": bool(r["shallow"]),
                "token": r["token"],
            }
            for r in rows
        }
