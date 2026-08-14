"""Daemon-side workspace watcher.

Polls the workspace root for *.nebo files and replays their entries through
DaemonState.ingest_events. The root is a `Workspace` (see
`nebo/server/workspace.py`): a local directory, or a Hugging Face bucket
addressed as ``hf://datasets/<owner>/<name>``. Non-recursive — subdirectories
are ignored on purpose so the --remote writer dir (and meta/) can sit inside a
local --logdir without feeding back into the watcher.

Shallow ingest: an unknown file is registered by reading only its **header**
(a few hundred bytes) — a "shallow" run that appears in listings from header
facts alone. Its body is read later, either when the file grows (a live run)
or on the first detail read of that run (`ensure_deep`, driven by the read
endpoints). Cold-starting on a directory of 1000 historical runs therefore
costs ~1 KB of I/O per run instead of a full replay, and nothing bulk-ingests
into RAM at startup. That property is what makes a bucket workspace practical:
a daemon on ephemeral infrastructure rebuilds its whole run list from headers.

With a cache-backed DaemonState, per-file offsets **and the shallow flag** are
persisted to the `watch_files` table, so a daemon restart resumes exactly
where it left off — shallow files stay shallow (no re-read), deep files resume
tailing. Reads go through `NeboFileReader.read_entries_incremental`, which
parks cleanly at a torn tail frame (a writer caught mid-append) — the offset
only ever advances past *complete* entries, so no prefix is skipped and
nothing is ingested twice.

Files can also disappear or be swapped out wholesale. Object storage replaces
objects rather than appending to them, so a backend that reports a content
marker (`FileStat.token`) gets replacement semantics: a tracked file missing
from the listing is forgotten (`_reap`), and one whose marker moved is
re-registered from its header rather than resumed at a byte offset that now
points into unrelated bytes. Local files report no marker and keep the
historical size-based append detection.

Caveat worth knowing: nebo has no run deletion. Re-registering only rewinds
the *file*, not the run — so if a run had already been deep-ingested when its
file was republished with different bytes, the new events append to the
existing run instead of replacing them. A publisher that regenerates runs
under stable ids should restart the daemon (or clear its cache) for a clean
rehydrate; a run that is still shallow (the common case for a cold-started
viewer) re-registers with no duplication at all.

Remote work never runs on the event loop — every storage call goes through
``Workspace.run_io``, which is inline locally and a thread hop for HTTP-backed
backends. A remote poll first checks a cheap commit marker
(``Workspace.change_token``) and skips the listing entirely while it is
unchanged.

Image/audio events are annotated with ``_media_src = (uri, offset, length)``
before ingest so the daemon can store media by reference into the .nebo file
rather than copying bytes into the cache. The uri is self-describing, so the
cache can resolve it later with no workspace in hand.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any, Optional

from nebo.core.fileformat import NeboFileReader
from nebo.server.workspace import FileStat, Workspace, open_workspace

if TYPE_CHECKING:
    from nebo.server.daemon import DaemonState

logger = logging.getLogger(__name__)

# Deep-ingest flushes to the daemon every this many entries instead of
# buffering a whole (possibly huge) file in memory first.
_INGEST_CHUNK = 10_000


class DirectoryWatcher:
    """Polls a workspace for .nebo files and replays entries into DaemonState."""

    def __init__(
        self,
        state: "DaemonState",
        logdir: Workspace | os.PathLike | str,
        poll_interval: Optional[float] = None,
    ) -> None:
        self._state = state
        # Accepts a ready Workspace or a bare path/URI (tests pass paths).
        self._ws: Workspace = (
            logdir if hasattr(logdir, "list_nebo") else open_workspace(str(logdir))
        )
        self._ws.ensure_root()
        self._poll_interval = (
            self._ws.default_poll_interval if poll_interval is None else poll_interval
        )
        self._tracked: dict[str, _Tracked] = {}
        self._stopping = asyncio.Event()
        self._cache = getattr(state, "cache", None)
        # Last *successfully listed* workspace change marker; while it is
        # unchanged there is nothing to list. Always None for local backends
        # (scandir is cheap).
        self._change_token: Optional[str] = None
        self._list_failed = False   # warn once per outage, not once per tick
        # Per-run locks so a read-triggered ensure_deep and the tick loop
        # never deep-ingest the same file concurrently.
        self._deepen_locks: dict[str, asyncio.Lock] = {}
        if self._cache is not None:
            for path_str, info in self._cache.get_watch_files().items():
                self._tracked[path_str] = _Tracked(
                    offset=info["offset"],
                    run_id=info["run_id"],
                    shallow=info.get("shallow", False),
                    size=info.get("size") or 0,
                    token=info.get("token"),
                )

    def stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._tick()
            except Exception:
                logger.exception("watcher tick failed")
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._poll_interval,
                )
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        # Cheap change probe first: on a remote workspace this is one small
        # request, versus a full tree listing.
        token = None
        if self._ws.is_remote:
            token = await self._ws.run_io(self._ws.change_token)
            if token is not None and token == self._change_token and self._tracked:
                return

        try:
            stats = await self._ws.run_io(self._ws.list_nebo)
        except Exception as e:
            # Leave tracked state alone. An empty listing means "every file
            # was deleted" and would reap every offset we hold, so a
            # transiently unreadable root must not look like one — and the
            # change token must not advance past a commit we never read.
            if not self._list_failed:
                self._list_failed = True
                logger.warning("watcher: cannot list %s (%s)", self._ws.uri, e)
            return
        if self._list_failed:
            self._list_failed = False
            logger.info("watcher: listing %s recovered", self._ws.uri)
        self._change_token = token

        for stat in stats:
            await self._sync_file(stat)
        self._reap({s.uri for s in stats})

    def _reap(self, present: set[str]) -> None:
        """Forget files that vanished from the workspace.

        A republished bucket deletes and re-adds paths; a stale offset would
        make a reappearing path resume mid-file. The run itself stays in RAM /
        the cache — nebo has no run deletion.
        """
        for uri in [u for u in self._tracked if u not in present]:
            del self._tracked[uri]
            if self._cache is not None:
                self._cache.enqueue(("watch_file_drop", uri))

    async def _sync_file(self, stat: FileStat) -> None:
        uri, size = stat.uri, stat.size
        tracked = self._tracked.get(uri)
        if tracked is None:
            await self._register_shallow(stat)
            return
        # A content marker that moved means the bytes were replaced, not
        # appended — even at an identical size. Start over from the header.
        if (
            stat.token is not None
            and tracked.token is not None
            and stat.token != tracked.token
        ):
            del self._tracked[uri]
            await self._register_shallow(stat)
            return
        if tracked.token is None and stat.token is not None:
            tracked.token = stat.token  # first sighting (or pre-token cache row)
        if tracked.shallow:
            # Frozen-size baseline: a static historical file stays shallow
            # forever (do NOT use the ordinary size>offset tail trigger here,
            # or every historical file would deep-ingest on the next tick).
            if size == tracked.size:
                return
            if size < tracked.size:
                # Truncated/replaced — start over from the header.
                del self._tracked[uri]
                await self._register_shallow(stat)
                return
            # The file grew: it's live. Read its body and start tailing.
            await self._deepen(uri, tracked.run_id)
            return
        # Deep file — tail on growth (today's behavior).
        if size < tracked.offset:
            del self._tracked[uri]
            await self._register_shallow(stat)
            return
        if size == tracked.offset:
            return
        await self._read_appended(stat, tracked)

    async def _register_shallow(self, stat: FileStat) -> None:
        """Register a run from its header only. Synthesizes a run_start so the
        run appears in listings via the normal ingest / cache / WS / tree-seed
        path, without reading the body."""
        uri, size = stat.uri, stat.size
        try:
            meta, header_end = await self._ws.run_io(self._read_header, uri)
            run_id = meta["run_id"]
        except Exception:
            # Distinguish "will never parse" (a complete but malformed prefix
            # — skip forever) from "header still being written" (retry).
            if size >= 6:
                logger.warning("watcher: skipping malformed file %s", uri)
                self._tracked[uri] = _Tracked(
                    offset=size, run_id=None, shallow=False, size=size,
                    token=stat.token,
                )
            return
        event = {
            "type": "run_start",
            "data": {
                "script_path": meta.get("script_path", ""),
                "started_at": meta.get("started_at"),
                "run_name": meta.get("run_name"),
                "group": meta.get("group"),
                "args": meta.get("args", []),
                # Registration, not "this run is now live" — don't hijack
                # active_run_id with a file discovered on disk.
                "_shallow": True,
            },
        }
        await self._state.ingest_events(
            [event], run_id=run_id, source="watcher",
        )
        # A shallow run's file mtime is a better "last active" than the
        # header-registration time create_run stamped. Backends without one
        # (HF listings need not carry a timestamp) fall back to the header.
        self._state.set_recency(run_id, stat.mtime or meta.get("started_at"))
        self._tracked[uri] = _Tracked(
            offset=header_end, run_id=run_id, shallow=True, size=size,
            token=stat.token,
        )
        self._persist_offset(uri, run_id, header_end, shallow=True, stat=stat)

    def _read_header(self, uri: str) -> tuple[dict, int]:
        with self._ws.reader(uri) as f:
            reader = NeboFileReader(f)
            meta = reader.read_header()
            return meta, f.tell()

    async def ensure_deep(self, run_id: str) -> None:
        """Deep-ingest a shallow run's body on first detail read. No-op if the
        run isn't a shallow watched file (already deep, or not watched)."""
        uri = None
        for u, t in self._tracked.items():
            if t.run_id == run_id and t.shallow:
                uri = u
                break
        if uri is not None:
            await self._deepen(uri, run_id)

    async def _deepen(self, uri: str, run_id: Optional[str]) -> None:
        """Read a shallow file's body [header_end .. EOF] and mark it deep.

        Idempotent under the per-run lock: a concurrent caller that already
        deepened it finds shallow=False and returns.
        """
        if run_id is None:
            return
        lock = self._deepen_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            tracked = self._tracked.get(uri)
            if tracked is None or not tracked.shallow:
                return
            # Catch-up ingest of a file's whole history: don't broadcast it
            # over WS — connected browsers would drown in replayed events
            # (they hydrate via REST right after ensure_deep anyway). Live
            # tail growth (_read_appended) still broadcasts.
            new_offset = await self._ingest_from(
                uri, tracked.offset, run_id, broadcast=False,
            )
            stat = await self._ws.run_io(self._ws.stat, uri)
            tracked.shallow = False
            tracked.offset = new_offset
            tracked.size = stat.size if stat is not None else new_offset
            if stat is not None and stat.token is not None:
                tracked.token = stat.token
            self._persist_offset(uri, run_id, new_offset, shallow=False, stat=stat)

    async def _read_appended(self, stat: FileStat, tracked: "_Tracked") -> None:
        try:
            new_offset = await self._ingest_from(
                stat.uri, tracked.offset, tracked.run_id,
            )
        except OSError:
            logger.warning("watcher: failed to tail %s", stat.uri)
            return
        tracked.offset = new_offset
        tracked.size = stat.size
        if stat.token is not None:
            tracked.token = stat.token
        self._persist_offset(
            stat.uri, tracked.run_id, new_offset, shallow=False, stat=stat,
        )

    async def _ingest_from(
        self, uri: str, start_offset: int, run_id: Optional[str],
        broadcast: bool = True,
    ) -> int:
        """Ingest complete entries from ``start_offset`` to EOF, in chunks of
        ``_INGEST_CHUNK``. Returns the resume offset — parked at the first torn
        tail frame, so only complete entries are ever consumed.

        Parsing runs off the event loop for remote workspaces; each chunk is
        handed back here so ingest still interleaves with other requests.
        The reader stays **open** across chunks — reopening per chunk would
        re-fetch the whole remaining tail each time, making a large remote
        file quadratic in chunk count.
        """
        # `reader()` is a context manager in every backend; enter and exit it
        # by hand so the handle can outlive a single run_io hop.
        cm = self._ws.reader(uri, start_offset)
        f = await self._ws.run_io(cm.__enter__)
        try:
            offset = start_offset
            while True:
                batch, offset, done = await self._ws.run_io(
                    self._read_chunk, f, uri,
                )
                if batch:
                    await self._state.ingest_events(
                        batch, run_id=run_id, source="watcher",
                        broadcast=broadcast,
                    )
                if done:
                    return offset
        finally:
            await self._ws.run_io(cm.__exit__, None, None, None)

    def _read_chunk(self, f: Any, uri: str) -> tuple[list[dict], int, bool]:
        """Read up to ``_INGEST_CHUNK`` entries from an open reader."""
        # We seek past the header, so reader._version stays None
        # (passthrough) — safe because only current-format files grow.
        reader = NeboFileReader(f)
        batch: list[dict] = []
        for entry, entry_start, entry_end in reader.read_entries_incremental():
            # The payload's own "type" key (spread second) deliberately
            # wins over the byte-derived one: alert frames are written as
            # unregistered byte 255 ("unknown_255") with the full event
            # dict as payload, and this recovery is what ingests them.
            event = {"type": entry["type"], **entry["payload"]}
            if event.get("type") in ("image", "audio") and "data" in event:
                event["_media_src"] = (
                    uri, entry_start, entry_end - entry_start,
                )
            batch.append(event)
            if len(batch) >= _INGEST_CHUNK:
                return batch, f.tell(), False
        return batch, f.tell(), True

    def _persist_offset(
        self, uri: str, run_id: str | None, offset: int, *, shallow: bool,
        stat: Optional[FileStat] = None,
    ) -> None:
        # No re-stat fallback: the only caller that can pass None is _deepen
        # when the object vanished mid-read, and re-statting from async
        # context would put a blocking request back on the event loop.
        if self._cache is None or stat is None:
            return
        self._cache.enqueue(
            ("watch_file", uri, run_id, offset, stat.size, stat.mtime,
             shallow, stat.token)
        )


class _Tracked:
    __slots__ = ("offset", "run_id", "shallow", "size", "token")

    def __init__(
        self,
        offset: int,
        run_id: str | None = None,
        shallow: bool = False,
        size: int = 0,
        token: str | None = None,
    ) -> None:
        self.offset = offset      # resume offset (header end while shallow)
        self.run_id = run_id
        self.shallow = shallow
        self.size = size          # frozen baseline while shallow; last-seen else
        self.token = token        # content marker; None where unsupported
