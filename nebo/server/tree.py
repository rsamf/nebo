"""The run tree: groups + per-run placements, persisted to meta/tree.json.

A **virtual** hierarchy over run_ids — ``.nebo`` files never move; the physical
layout stays flat. ``tree.json`` is the single durable placement store (there
is no birth-placement fallback and no override layer): the ``group`` recorded
at run start only *seeds* the map on first sight, after which the map is
authoritative and every move is explicit.

The daemon holds the tree in RAM and rewrites the whole (tiny) JSON on each
mutation — human/agent-frequency, so a synchronous write is fine. A
``threading.Lock`` guards it because mutations come from both the async HTTP
endpoints and the synchronous ingest seed hook. Group docs are real markdown
files under ``meta/docs/<group-path>/``.

Storage goes through a `Workspace` (see `nebo/server/workspace.py`), so the
tree lives wherever the workspace root does. Locally that is the same
tmp+fsync+rename write it has always been. On a bucket workspace two things
change, both because a write is an HTTP commit rather than a rename:

* **Writes are debounced.** A cold start seeds one group per run; without
  coalescing that would be one commit per run. Mutations mark the tree dirty
  and arm a short timer, and `flush()` (called on daemon shutdown) drains it.
  Doc mutations bypass the timer — a `PUT` must be readable by the next `GET`
  — and carry any pending tree change along in the same commit.
* **Doc *names* are indexed in RAM.** `to_payload()` lists every group's docs,
  and it runs on every ``GET /tree`` and every ``tree_updated`` broadcast. One
  listing per group per read is free locally and untenable over HTTP, so the
  remote backend indexes names once at load and maintains them in place. Doc
  *contents* stay lazy.

A bucket workspace the daemon can read but not write (a public dataset, no HF
token) is a supported configuration: write failures log once and the tree
keeps working in RAM. Placements re-seed from run headers on the next start.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Optional

from nebo.core.groups import ancestors, validate_doc_name, validate_group_path
from nebo.server.workspace import LocalWorkspace, Workspace

logger = logging.getLogger(__name__)

TREE_VERSION = 1

# How long a remote write waits for more mutations before committing, and the
# longest a steady stream of them can defer one.
REMOTE_SAVE_DEBOUNCE_S = 2.0
REMOTE_SAVE_MAX_DELAY_S = 15.0


class TreeConflict(Exception):
    """A mutation refused because it would violate structure (HTTP 409),
    e.g. deleting a non-empty group."""


class TreeStore:
    """Load / mutate / persist ``meta/tree.json`` and ``meta/docs/``."""

    def __init__(
        self,
        meta: Workspace | Path | str,
        prefix: str = "meta",
        debounce: Optional[float] = None,
    ) -> None:
        # Accepts a Workspace (+ the prefix within it) or a bare meta
        # directory, which becomes a local workspace rooted at that directory.
        if hasattr(meta, "commit"):
            self._ws: Workspace = meta  # type: ignore[assignment]
            self._prefix = prefix.strip("/")
        else:
            self._ws = LocalWorkspace(meta)
            self._prefix = ""
        self._lock = threading.RLock()
        self._groups: dict[str, dict] = {}  # path -> {} (reserved for later)
        self._runs: dict[str, str] = {}     # run_id -> group path ("" = root)

        # Remote-only write coalescing.
        self._debounce = (
            REMOTE_SAVE_DEBOUNCE_S if debounce is None else debounce
        )
        self._dirty = False
        self._timer: Optional[threading.Timer] = None
        self._deadline: Optional[float] = None
        self._write_failed = False
        # group path -> doc names. Only maintained for remote workspaces;
        # local reads the directory so externally-added files show up.
        self._doc_index: Optional[dict[str, list[str]]] = None

        self._load()

    @property
    def workspace(self) -> Workspace:
        return self._ws

    # -- paths -----------------------------------------------------------

    def _rel(self, *parts: str) -> str:
        segs = [self._prefix, *parts] if self._prefix else list(parts)
        return "/".join(s.strip("/") for s in segs if s and s.strip("/"))

    @property
    def _tree_rel(self) -> str:
        return self._rel("tree.json")

    def _docs_rel(self, path: str = "") -> str:
        return self._rel("docs", path)

    # -- persistence ---------------------------------------------------

    def _load(self) -> None:
        raw = self._ws.read_bytes(self._tree_rel)
        if raw is not None:
            try:
                doc = json.loads(raw.decode("utf-8"))
            except Exception as e:  # noqa: BLE001 — refuse to discard curation
                raise RuntimeError(
                    f"nebo: {self._ws.uri}/{self._tree_rel} is unparseable ({e}). "
                    "Refusing to start rather than silently discard run "
                    "organization — fix or remove the file."
                )
            self._groups = {
                k: dict(v or {}) for k, v in (doc.get("groups") or {}).items()
            }
            self._runs = dict(doc.get("runs") or {})
        if self._ws.is_remote:
            self._build_doc_index()

    def _build_doc_index(self) -> None:
        """One recursive listing of meta/docs/ -> {group path: [doc names]}."""
        index: dict[str, list[str]] = {}
        for rel in self._ws.list_tree(self._docs_rel()):
            rel = rel.replace("\\", "/")
            if not rel.endswith(".md"):
                continue
            group, _, name = rel.rpartition("/")
            index.setdefault(group, []).append(name)
        for names in index.values():
            names.sort()
        self._doc_index = index

    def _payload_bytes(self) -> bytes:
        data = {
            "version": TREE_VERSION,
            "groups": self._groups,
            "runs": self._runs,
        }
        return json.dumps(data, indent=2, sort_keys=True).encode("utf-8")

    def _save(self) -> None:
        """Persist the tree. Caller holds the lock."""
        if self._ws.is_remote:
            self._dirty = True
            self._arm_timer()
            return
        self._apply([], [], tree=True)

    def _apply(
        self,
        adds: list[tuple[str, bytes]],
        deletes: list[str],
        *,
        tree: bool,
        message: str = "nebo: update run tree",
    ) -> None:
        """Write through to the workspace now. Caller holds the lock."""
        if tree or self._dirty:
            adds = [(self._tree_rel, self._payload_bytes()), *adds]
        if not adds and not deletes:
            return
        try:
            self._ws.commit(adds, deletes, message=message)
        except Exception as e:  # noqa: BLE001
            if not self._ws.is_remote:
                raise
            # A read-only bucket (public dataset, no write token) is a
            # supported deployment: keep serving from RAM.
            if not self._write_failed:
                self._write_failed = True
                logger.warning(
                    "nebo: cannot write the run tree to %s (%s). The tree stays "
                    "in memory for this process; run placements re-seed from "
                    ".nebo headers on restart. Set HF_TOKEN with write access "
                    "to persist it.",
                    self._ws.uri, e,
                )
            return
        self._dirty = False
        self._cancel_timer()

    # -- debounced remote writes ------------------------------------------

    def _arm_timer(self) -> None:
        """(Re)start the coalescing window. Caller holds the lock."""
        import time

        now = time.monotonic()
        if self._deadline is None:
            self._deadline = now + REMOTE_SAVE_MAX_DELAY_S
        # A steady stream of mutations must not defer the write forever.
        delay = max(0.0, min(self._debounce, self._deadline - now))
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(delay, self._on_timer)
        self._timer.daemon = True
        self._timer.start()

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._deadline = None

    def _on_timer(self) -> None:
        with self._lock:
            self._timer = None
            self._deadline = None
            if self._dirty:
                self._apply([], [], tree=True)

    def flush(self) -> None:
        """Commit any pending tree write. Called on daemon shutdown."""
        with self._lock:
            self._cancel_timer()
            if self._dirty:
                self._apply([], [], tree=True)

    # -- helpers -------------------------------------------------------

    def _list_docs_locked(self, path: str) -> list[str]:
        if self._doc_index is not None:
            names = list(self._doc_index.get(path, ()))
        else:
            names = [
                n for n in self._ws.list_dir(self._docs_rel(path))
                if n.endswith(".md")
            ]
        names.sort()
        # README first, then the rest alphabetically.
        readme = [n for n in names if n.lower() == "readme.md"]
        return readme + [n for n in names if n.lower() != "readme.md"]

    def _index_add(self, group: str, name: str) -> None:
        if self._doc_index is None:
            return
        names = self._doc_index.setdefault(group, [])
        if name not in names:
            names.append(name)
            names.sort()

    def _index_remove(self, group: str, name: str) -> None:
        if self._doc_index is None:
            return
        names = self._doc_index.get(group)
        if names and name in names:
            names.remove(name)
        if names is not None and not names:
            self._doc_index.pop(group, None)

    def _index_drop_subtree(self, group: str) -> None:
        if self._doc_index is None:
            return
        for g in [
            g for g in self._doc_index
            if g == group or g.startswith(group + "/")
        ]:
            self._doc_index.pop(g, None)

    def _ensure_group_locked(self, path: str) -> bool:
        """Create ``path`` and all ancestors. Returns True if anything was
        added. Caller holds the lock."""
        added = False
        for gp in ancestors(path):
            if gp not in self._groups:
                self._groups[gp] = {}
                added = True
        return added

    def _group_doc_dir(self, path: str) -> str:
        return self._docs_rel(path)

    def _read_docs_subtree(self, group: str) -> list[tuple[str, bytes]]:
        """Every doc under ``group``, as (name-relative-to-group, content)."""
        base = self._docs_rel(group)
        out: list[tuple[str, bytes]] = []
        for rel in self._ws.list_tree(base):
            rel = rel.replace("\\", "/")
            data = self._ws.read_bytes(f"{base}/{rel}")
            if data is not None:
                out.append((rel, data))
        return out

    # -- mutations -----------------------------------------------------

    def seed_run(self, run_id: str, group: object) -> bool:
        """Seed-once: record a run's birth group **only if it has no placement
        yet**. A moved run therefore stays moved when its file is re-scanned —
        the header never re-wins. Returns True if the tree changed."""
        gp = validate_group_path(group)
        with self._lock:
            if run_id in self._runs:
                return False
            if not gp:
                return False  # root needs no entry (absent = root)
            self._runs[run_id] = gp
            self._ensure_group_locked(gp)
            self._save()
            return True

    def create_group(self, path: object) -> bool:
        """Create a group (and ancestors). Returns True if newly created,
        False if it already existed. Raises ValueError on a bad/root path."""
        gp = validate_group_path(path)
        if not gp:
            raise ValueError("cannot create the root group")
        with self._lock:
            if gp in self._groups:
                return False
            self._ensure_group_locked(gp)
            self._save()
            return True

    def move_group(self, old: object, new: object) -> None:
        """Rename/move a group subtree: rewrites the group key, all descendant
        group keys, all placements under the subtree (incl. dangling ones), and
        moves the docs directory."""
        old_gp = validate_group_path(old)
        new_gp = validate_group_path(new)
        if not old_gp:
            raise ValueError("cannot move the root group")
        if not new_gp:
            raise ValueError("cannot move a group to root")
        with self._lock:
            if old_gp not in self._groups:
                raise ValueError(f"group not found: {old_gp!r}")
            if new_gp == old_gp:
                return
            if new_gp in self._groups or new_gp.startswith(old_gp + "/"):
                raise TreeConflict(
                    f"cannot move {old_gp!r} onto {new_gp!r} (exists or is a "
                    "descendant)"
                )

            def _remap(gp: str) -> str:
                if gp == old_gp:
                    return new_gp
                if gp.startswith(old_gp + "/"):
                    return new_gp + gp[len(old_gp):]
                return gp

            self._groups = {_remap(gp): v for gp, v in self._groups.items()}
            self._ensure_group_locked(new_gp)
            self._runs = {rid: _remap(gp) for rid, gp in self._runs.items()}

            # Read the subtree, then re-add it under the new prefix and drop
            # both the source and any pre-existing destination — one commit.
            docs = self._read_docs_subtree(old_gp)
            adds = [
                (f"{self._docs_rel(new_gp)}/{rel}", data) for rel, data in docs
            ]
            deletes = [self._docs_rel(old_gp) + "/", self._docs_rel(new_gp) + "/"]
            if self._doc_index is not None:
                moved = {
                    _remap(g): names
                    for g, names in self._doc_index.items()
                    if g == old_gp or g.startswith(old_gp + "/")
                }
                self._index_drop_subtree(old_gp)
                self._index_drop_subtree(new_gp)
                self._doc_index.update(moved)
            self._apply(
                adds, deletes, tree=True,
                message=f"nebo: move group {old_gp} -> {new_gp}",
            )

    def delete_group(self, path: object, known_run_ids: set[str]) -> None:
        """Delete an empty group. Raises TreeConflict if it has subgroups or
        known member runs. Drops dangling placements pointing at it and its
        docs."""
        gp = validate_group_path(path)
        if not gp:
            raise ValueError("cannot delete the root group")
        with self._lock:
            if gp not in self._groups:
                raise ValueError(f"group not found: {gp!r}")
            if any(g.startswith(gp + "/") for g in self._groups):
                raise TreeConflict(f"group {gp!r} has subgroups")
            members = [
                rid for rid, g in self._runs.items()
                if g == gp and rid in known_run_ids
            ]
            if members:
                raise TreeConflict(
                    f"group {gp!r} still has {len(members)} run(s) — move them "
                    "out first (nebo has no run deletion)"
                )
            del self._groups[gp]
            # Drop dangling placements (unknown runs) that pointed here.
            self._runs = {rid: g for rid, g in self._runs.items() if g != gp}
            self._index_drop_subtree(gp)
            self._apply(
                [], [self._docs_rel(gp) + "/"], tree=True,
                message=f"nebo: delete group {gp}",
            )

    def set_run_group(self, run_id: str, group: object) -> str:
        """Explicitly place a run (override). ``""`` moves it to root (kept as
        an explicit entry so a later re-scan won't re-seed it). Auto-creates
        the target group. Returns the normalized group path."""
        gp = validate_group_path(group)
        with self._lock:
            if gp:
                self._ensure_group_locked(gp)
            self._runs[run_id] = gp
            self._save()
            return gp

    # -- docs ----------------------------------------------------------

    def get_doc(self, path: object, name: object) -> str | None:
        gp = validate_group_path(path)
        doc = validate_doc_name(name)
        data = self._ws.read_bytes(f"{self._docs_rel(gp)}/{doc}")
        if data is None:
            return None
        return data.decode("utf-8")

    def set_doc(self, path: object, name: object, content: str) -> bool:
        """Write a doc (auto-creating the group). Returns True if the file was
        newly created, False if it overwrote an existing one."""
        gp = validate_group_path(path)
        doc = validate_doc_name(name)
        with self._lock:
            if gp:
                self._ensure_group_locked(gp)
            existed = doc in self._list_docs_locked(gp)
            self._index_add(gp, doc)
            # Written immediately, not debounced: a PUT must be visible to the
            # next GET. Any pending tree change rides along in the same commit.
            self._apply(
                [(f"{self._docs_rel(gp)}/{doc}", content.encode("utf-8"))],
                [],
                tree=True,
                message=f"nebo: set doc {gp}/{doc}" if gp else f"nebo: set doc {doc}",
            )
            return not existed

    def delete_doc(self, path: object, name: object) -> bool:
        gp = validate_group_path(path)
        doc = validate_doc_name(name)
        with self._lock:
            if doc not in self._list_docs_locked(gp):
                return False
            self._index_remove(gp, doc)
            self._apply(
                [], [f"{self._docs_rel(gp)}/{doc}"], tree=False,
                message=f"nebo: delete doc {gp}/{doc}" if gp else f"nebo: delete doc {doc}",
            )
            return True

    # -- read ----------------------------------------------------------

    def to_payload(self, known_run_ids: set[str]) -> dict:
        """The GET /tree / tree_updated body. Placements are filtered to
        currently-known runs; a placement to a since-deleted group reads as
        root (omitted)."""
        with self._lock:
            groups = {
                gp: {"docs": self._list_docs_locked(gp)}
                for gp in sorted(self._groups)
            }
            runs = {
                rid: gp
                for rid, gp in self._runs.items()
                if rid in known_run_ids and gp and gp in self._groups
            }
        return {"groups": groups, "runs": runs}
