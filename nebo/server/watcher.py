"""Daemon-side directory watcher.

Polls a directory for *.nebo files and replays their entries through
DaemonState.ingest_events. Non-recursive — subdirectories are ignored
on purpose so the --remote writer dir (and meta/) can sit inside --logdir
without feeding back into the watcher.

Shallow ingest: an unknown file is registered by reading only its **header**
(a few hundred bytes) — a "shallow" run that appears in listings from header
facts alone. Its body is read later, either when the file grows (a live run)
or on the first detail read of that run (`ensure_deep`, driven by the read
endpoints). Cold-starting on a directory of 1000 historical runs therefore
costs ~1 KB of I/O per run instead of a full replay, and nothing bulk-ingests
into RAM at startup.

With a cache-backed DaemonState, per-file offsets **and the shallow flag** are
persisted to the `watch_files` table, so a daemon restart resumes exactly
where it left off — shallow files stay shallow (no re-read), deep files resume
tailing. Reads go through `NeboFileReader.read_entries_incremental`, which
parks cleanly at a torn tail frame (a writer caught mid-append) — the offset
only ever advances past *complete* entries, so no prefix is skipped and
nothing is ingested twice.

Image/audio events are annotated with ``_media_src = (path, offset, length)``
before ingest so the daemon can store media by reference into the .nebo file
rather than copying bytes into the cache.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from nebo.core.fileformat import NeboFileReader

if TYPE_CHECKING:
    from nebo.server.daemon import DaemonState

logger = logging.getLogger(__name__)

# Deep-ingest flushes to the daemon every this many entries instead of
# buffering a whole (possibly huge) file in memory first.
_INGEST_CHUNK = 10_000


class DirectoryWatcher:
    """Polls a directory for .nebo files and replays entries into DaemonState."""

    def __init__(
        self,
        state: "DaemonState",
        logdir: os.PathLike | str,
        poll_interval: float = 0.5,
    ) -> None:
        self._state = state
        self._logdir = Path(logdir)
        self._logdir.mkdir(parents=True, exist_ok=True)
        self._poll_interval = poll_interval
        self._tracked: dict[Path, _Tracked] = {}
        self._stopping = asyncio.Event()
        self._cache = getattr(state, "cache", None)
        # Per-run locks so a read-triggered ensure_deep and the tick loop
        # never deep-ingest the same file concurrently.
        self._deepen_locks: dict[str, asyncio.Lock] = {}
        if self._cache is not None:
            for path_str, info in self._cache.get_watch_files().items():
                self._tracked[Path(path_str)] = _Tracked(
                    offset=info["offset"],
                    run_id=info["run_id"],
                    shallow=info.get("shallow", False),
                    size=info.get("size") or 0,
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
        try:
            entries = [
                Path(e.path) for e in os.scandir(self._logdir)
                if e.is_file() and e.name.endswith(".nebo")
            ]
        except FileNotFoundError:
            return
        for path in entries:
            await self._sync_file(path)

    async def _sync_file(self, path: Path) -> None:
        tracked = self._tracked.get(path)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return
        if tracked is None:
            await self._register_shallow(path, size)
            return
        if tracked.shallow:
            # Frozen-size baseline: a static historical file stays shallow
            # forever (do NOT use the ordinary size>offset tail trigger here,
            # or every historical file would deep-ingest on the next tick).
            if size == tracked.size:
                return
            if size < tracked.size:
                # Truncated/replaced — start over from the header.
                del self._tracked[path]
                await self._register_shallow(path, size)
                return
            # The file grew: it's live. Read its body and start tailing.
            await self._deepen(path, tracked.run_id)
            return
        # Deep file — tail on growth (today's behavior).
        if size < tracked.offset:
            del self._tracked[path]
            await self._register_shallow(path, size)
            return
        if size == tracked.offset:
            return
        await self._read_appended(path, tracked, size)

    async def _register_shallow(self, path: Path, size: int) -> None:
        """Register a run from its header only. Synthesizes a run_start so the
        run appears in listings via the normal ingest / cache / WS / tree-seed
        path, without reading the body."""
        try:
            with path.open("rb") as f:
                reader = NeboFileReader(f)
                meta = reader.read_header()
                run_id = meta["run_id"]
                header_end = f.tell()
        except Exception:
            # Distinguish "will never parse" (a complete but malformed prefix
            # — skip forever) from "header still being written" (retry).
            if size >= 6:
                logger.warning("watcher: skipping malformed file %s", path)
                self._tracked[path] = _Tracked(
                    offset=size, run_id=None, shallow=False, size=size,
                )
            return
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            mtime = None
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
        # header-registration time create_run stamped.
        self._state.set_recency(run_id, mtime)
        self._tracked[path] = _Tracked(
            offset=header_end, run_id=run_id, shallow=True, size=size,
        )
        self._persist_offset(path, run_id, header_end, shallow=True)

    async def ensure_deep(self, run_id: str) -> None:
        """Deep-ingest a shallow run's body on first detail read. No-op if the
        run isn't a shallow watched file (already deep, or not watched)."""
        path = None
        for p, t in self._tracked.items():
            if t.run_id == run_id and t.shallow:
                path = p
                break
        if path is not None:
            await self._deepen(path, run_id)

    async def _deepen(self, path: Path, run_id: Optional[str]) -> None:
        """Read a shallow file's body [header_end .. EOF] and mark it deep.

        Idempotent under the per-run lock: a concurrent caller that already
        deepened it finds shallow=False and returns.
        """
        if run_id is None:
            return
        lock = self._deepen_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            tracked = self._tracked.get(path)
            if tracked is None or not tracked.shallow:
                return
            # Catch-up ingest of a file's whole history: don't broadcast it
            # over WS — connected browsers would drown in replayed events
            # (they hydrate via REST right after ensure_deep anyway). Live
            # tail growth (_read_appended) still broadcasts.
            new_offset = await self._ingest_from(
                path, tracked.offset, run_id, broadcast=False,
            )
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                size = new_offset
            tracked.shallow = False
            tracked.offset = new_offset
            tracked.size = size
            self._persist_offset(path, run_id, new_offset, shallow=False)

    async def _read_appended(
        self, path: Path, tracked: "_Tracked", size: int,
    ) -> None:
        try:
            new_offset = await self._ingest_from(path, tracked.offset, tracked.run_id)
        except OSError:
            logger.warning("watcher: failed to tail %s", path)
            return
        tracked.offset = new_offset
        tracked.size = size
        self._persist_offset(path, tracked.run_id, new_offset, shallow=False)

    async def _ingest_from(
        self, path: Path, start_offset: int, run_id: Optional[str],
        broadcast: bool = True,
    ) -> int:
        """Ingest complete entries from ``start_offset`` to EOF, in chunks of
        ``_INGEST_CHUNK``. Returns the resume offset — parked at the first torn
        tail frame, so only complete entries are ever consumed.
        """
        with path.open("rb") as f:
            f.seek(start_offset)
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
                        str(path), entry_start, entry_end - entry_start,
                    )
                batch.append(event)
                if len(batch) >= _INGEST_CHUNK:
                    await self._state.ingest_events(
                        batch, run_id=run_id, source="watcher",
                        broadcast=broadcast,
                    )
                    batch = []
            if batch:
                await self._state.ingest_events(
                    batch, run_id=run_id, source="watcher",
                    broadcast=broadcast,
                )
            return f.tell()

    def _persist_offset(
        self, path: Path, run_id: str | None, offset: int, *, shallow: bool,
    ) -> None:
        if self._cache is None:
            return
        try:
            st = path.stat()
        except FileNotFoundError:
            return
        self._cache.enqueue(
            ("watch_file", str(path), run_id, offset, st.st_size, st.st_mtime,
             shallow)
        )


class _Tracked:
    __slots__ = ("offset", "run_id", "shallow", "size")

    def __init__(
        self,
        offset: int,
        run_id: str | None = None,
        shallow: bool = False,
        size: int = 0,
    ) -> None:
        self.offset = offset      # resume offset (header end while shallow)
        self.run_id = run_id
        self.shallow = shallow
        self.size = size          # frozen baseline while shallow; last-seen else
