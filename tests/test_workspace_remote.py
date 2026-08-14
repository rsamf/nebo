"""Remote-workspace behavior, exercised offline through a fake backend.

Everything the bucket path does differently from a local directory —
off-loop I/O, change-token polling, missing mtimes, vanished and replaced
files, debounced tree writes, the RAM docs index — is driven here by an
in-memory `FakeWorkspace`. No network, no huggingface_hub.
"""

from __future__ import annotations

import asyncio
import io

import pytest

from nebo.core.fileformat import NeboFileWriter
from nebo.server.cache import RunCache
from nebo.server.daemon import DaemonState
from nebo.server.tree import TreeStore
from nebo.server.watcher import DirectoryWatcher
from nebo.server.workspace import FileStat, _OffsetStream

TEXT = {"type": "text", "loggable_id": "__global__", "message": "hi"}
METRIC = {
    "type": "metric", "loggable_id": "n", "name": "loss", "metric_type": "line",
    "value": 0.5, "step": 0, "tags": [], "timestamp": 1.0,
}


def build_nebo(
    run_id: str, events: list[dict], started_at: float | None = None, **header
) -> bytes:
    """A complete .nebo file as bytes."""
    buf = io.BytesIO()
    w = NeboFileWriter(buf, run_id=run_id, script_path="/x/s.py", **header)
    if started_at is not None:
        w._started_at = started_at
    w.write_header()
    for e in events:
        w.write_entry(e["type"], dict(e))
    w.close()
    return buf.getvalue()


class FakeWorkspace:
    """An in-memory workspace that behaves like a remote one.

    Deliberately mimics the awkward parts of a bucket: no mtimes, a content
    token instead of size-based change detection, a change marker that gates
    listing, and I/O that must be awaited.
    """

    is_remote = True
    default_poll_interval = 30.0

    def __init__(self, uri="hf://datasets/acme/runs", files=None):
        self.uri = uri
        self.files: dict[str, bytes] = dict(files or {})
        self.revision = 0          # bumped on every mutation
        self.commits: list[tuple[list[str], list[str], str]] = []
        self.io_calls = 0
        self.list_calls = 0
        self.read_only = False

    # -- test helpers ---------------------------------------------------

    def put(self, name: str, data: bytes) -> None:
        self.files[name] = data
        self.revision += 1

    def remove(self, name: str) -> None:
        self.files.pop(name, None)
        self.revision += 1

    def _uri(self, name: str) -> str:
        return f"{self.uri}/{name}"

    def _name(self, uri: str) -> str:
        return uri[len(self.uri) + 1:] if uri.startswith(self.uri + "/") else uri

    # -- Workspace protocol ---------------------------------------------

    def ensure_root(self) -> None:
        pass

    def change_token(self):
        return str(self.revision)

    def list_nebo(self) -> list[FileStat]:
        self.list_calls += 1
        return [
            FileStat(
                uri=self._uri(n),
                size=len(d),
                mtime=None,                       # buckets need not have one
                token=f"blob{hash(d) & 0xffff:04x}",
            )
            for n, d in sorted(self.files.items())
            if n.endswith(".nebo")
        ]

    def stat(self, uri):
        name = self._name(uri)
        if name not in self.files:
            return None
        data = self.files[name]
        return FileStat(
            uri=uri, size=len(data), mtime=None,
            token=f"blob{hash(data) & 0xffff:04x}",
        )

    def reader(self, uri, offset=0):
        data = self.files.get(self._name(uri))
        if data is None:
            raise FileNotFoundError(uri)
        return _OffsetStream(io.BytesIO(data[offset:]), offset)

    def read_range(self, uri, offset, length):
        data = self.files.get(self._name(uri))
        if data is None:
            return None
        return data[offset:offset + length]

    def read_bytes(self, rel):
        return self.files.get(rel.strip("/"))

    def list_dir(self, rel):
        base = rel.strip("/")
        prefix = f"{base}/" if base else ""
        return sorted(
            n[len(prefix):] for n in self.files
            if n.startswith(prefix) and "/" not in n[len(prefix):]
        )

    def list_tree(self, rel):
        base = rel.strip("/")
        prefix = f"{base}/" if base else ""
        return sorted(n[len(prefix):] for n in self.files if n.startswith(prefix))

    def commit(self, adds, deletes, *, message="nebo: update"):
        if self.read_only:
            raise PermissionError("403 Forbidden")
        self.commits.append(
            ([r for r, _ in adds], list(deletes), message)
        )
        for rel in deletes:
            rel = rel.strip("/")
            for name in [
                n for n in self.files if n == rel or n.startswith(rel + "/")
            ]:
                self.files.pop(name)
        for rel, data in adds:
            self.files[rel.strip("/")] = data
        self.revision += 1

    async def run_io(self, fn, *args):
        self.io_calls += 1
        return await asyncio.to_thread(fn, *args)


# ---------------------------------------------------------------------------
# Watcher over a remote workspace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registers_runs_from_headers_only():
    ws = FakeWorkspace(files={"a.nebo": build_nebo("remoteaaaaa1", [METRIC, TEXT])})
    state = DaemonState()
    await DirectoryWatcher(state, logdir=ws)._tick()

    assert "remoteaaaaa1" in state.runs
    run = state.runs["remoteaaaaa1"]
    assert not run.texts                    # body not read
    assert "n" not in run.loggables


@pytest.mark.asyncio
async def test_all_io_goes_off_the_event_loop():
    """Blocking HTTP on the loop would stall the whole daemon."""
    ws = FakeWorkspace(files={"a.nebo": build_nebo("remoteaaaaa2", [TEXT])})
    await DirectoryWatcher(DaemonState(), logdir=ws)._tick()
    assert ws.io_calls > 0


@pytest.mark.asyncio
async def test_unchanged_workspace_skips_listing():
    ws = FakeWorkspace(files={"a.nebo": build_nebo("remoteaaaaa3", [TEXT])})
    w = DirectoryWatcher(DaemonState(), logdir=ws)
    await w._tick()
    assert ws.list_calls == 1
    await w._tick()
    await w._tick()
    assert ws.list_calls == 1               # commit marker unchanged
    ws.put("b.nebo", build_nebo("remoteaaaaa4", [TEXT]))
    await w._tick()
    assert ws.list_calls == 2


@pytest.mark.asyncio
async def test_missing_mtime_falls_back_to_header_started_at():
    """HF listings need not carry a timestamp; recency must survive that."""
    data = build_nebo("remoteaaaaa5", [TEXT], started_at=1234.5)
    ws = FakeWorkspace(files={"a.nebo": data})
    state = DaemonState()
    await DirectoryWatcher(state, logdir=ws)._tick()
    assert state.runs["remoteaaaaa5"].last_event_at == 1234.5


@pytest.mark.asyncio
async def test_deepen_reads_the_body():
    ws = FakeWorkspace(files={"a.nebo": build_nebo("remoteaaaaa6", [METRIC, TEXT])})
    state = DaemonState()
    w = DirectoryWatcher(state, logdir=ws)
    await w._tick()
    await w.ensure_deep("remoteaaaaa6")

    run = state.runs["remoteaaaaa6"]
    assert [t.message for t in run.texts] == ["hi"]
    assert "n" in run.loggables


@pytest.mark.asyncio
async def test_republished_file_is_re_registered_not_tailed():
    """Object storage replaces objects; it never appends to them.

    So a changed content marker means "new file at the same path", and the
    watcher must restart from the header rather than resume at a byte offset
    that now points into unrelated bytes.
    """
    ws = FakeWorkspace(files={"a.nebo": build_nebo("remoteaaaaa7", [TEXT])})
    state = DaemonState()
    w = DirectoryWatcher(state, logdir=ws)
    await w._tick()
    uri = ws._uri("a.nebo")
    header_end = w._tracked[uri].offset

    ws.put("a.nebo", build_nebo("remoteaaaaa7", [TEXT, TEXT, METRIC]))
    await w._tick()

    tracked = w._tracked[uri]
    assert tracked.shallow is True          # back to header-only
    assert tracked.offset == header_end     # body not yet read

    await w.ensure_deep("remoteaaaaa7")
    run = state.runs["remoteaaaaa7"]
    assert len(run.texts) == 2      # the new body, ingested exactly once
    assert "n" in run.loggables


@pytest.mark.asyncio
async def test_vanished_file_is_forgotten():
    """A republished bucket deletes paths; a stale offset would resume
    mid-file if the same path came back."""
    ws = FakeWorkspace(files={"a.nebo": build_nebo("remoteaaaaa8", [TEXT])})
    w = DirectoryWatcher(DaemonState(), logdir=ws)
    await w._tick()
    assert len(w._tracked) == 1

    ws.remove("a.nebo")
    await w._tick()
    assert w._tracked == {}


@pytest.mark.asyncio
async def test_replaced_content_at_the_same_size_is_re_registered():
    """Size-based detection alone would miss a same-length rewrite."""
    ws = FakeWorkspace(files={"a.nebo": build_nebo("remoteaaaaa9", [TEXT])})
    state = DaemonState()
    w = DirectoryWatcher(state, logdir=ws)
    await w._tick()
    first = dict(w._tracked)

    replaced = build_nebo("remotebbbbb1", [TEXT])
    assert len(replaced) == len(next(iter(first)) and ws.files["a.nebo"])
    ws.put("a.nebo", replaced)
    await w._tick()

    assert "remotebbbbb1" in state.runs
    assert w._tracked[ws._uri("a.nebo")].run_id == "remotebbbbb1"


@pytest.mark.asyncio
async def test_dropped_offset_is_persisted_to_the_cache(tmp_path):
    cache = RunCache(tmp_path / "cache.db", logdir="hf://datasets/acme/runs")
    cache.start()
    try:
        ws = FakeWorkspace(files={"a.nebo": build_nebo("remotebbbbb2", [TEXT])})
        state = DaemonState(cache=cache)
        w = DirectoryWatcher(state, logdir=ws)
        await w._tick()
        cache.flush()
        assert list(cache.get_watch_files()) == [ws._uri("a.nebo")]

        ws.remove("a.nebo")
        await w._tick()
        cache.flush()
        assert cache.get_watch_files() == {}
    finally:
        cache.close()


@pytest.mark.asyncio
async def test_watch_offsets_survive_a_restart(tmp_path):
    cache = RunCache(tmp_path / "cache.db", logdir="hf://datasets/acme/runs")
    cache.start()
    try:
        ws = FakeWorkspace(files={"a.nebo": build_nebo("remotebbbbb3", [TEXT])})
        w = DirectoryWatcher(DaemonState(cache=cache), logdir=ws)
        await w._tick()
        cache.flush()

        # A fresh watcher over the same cache must not re-read the file.
        w2 = DirectoryWatcher(DaemonState(cache=cache), logdir=ws)
        assert ws._uri("a.nebo") in w2._tracked
        assert w2._tracked[ws._uri("a.nebo")].shallow is True
    finally:
        cache.close()


@pytest.mark.asyncio
async def test_media_refs_are_remote_uris():
    """src_path must be self-describing so the cache can resolve it later."""
    img = {
        "type": "image", "loggable_id": "n", "name": "hero.png",
        "data": b"\x89PNG-not-really", "step": 0, "timestamp": 1.0,
    }
    ws = FakeWorkspace(files={"a.nebo": build_nebo("remotebbbbb4", [img])})
    state = DaemonState()
    w = DirectoryWatcher(state, logdir=ws)
    await w._tick()

    batch, _, _ = w._read_chunk(ws._uri("a.nebo"), w._tracked[ws._uri("a.nebo")].offset)
    src = [e["_media_src"] for e in batch if "_media_src" in e]
    assert src and src[0][0] == "hf://datasets/acme/runs/a.nebo"


@pytest.mark.asyncio
async def test_media_bytes_resolve_through_the_workspace():
    from nebo.server import workspace as ws_mod

    img_bytes = b"\x89PNG\r\n\x1a\n-payload"
    img = {
        "type": "image", "loggable_id": "n", "name": "hero.png",
        "data": img_bytes, "step": 0, "timestamp": 1.0,
    }
    ws = FakeWorkspace(files={"a.nebo": build_nebo("remotebbbbb5", [img])})
    w = DirectoryWatcher(DaemonState(), logdir=ws)
    await w._tick()
    uri = ws._uri("a.nebo")
    batch, _, _ = w._read_chunk(uri, w._tracked[uri].offset)
    _, offset, length = next(e["_media_src"] for e in batch if "_media_src" in e)

    # Resolve the frame the way RunCache._read_media_ref does.
    orig = ws_mod._remote_for
    ws_mod._remote_for = lambda _uri: ws
    try:
        from nebo.server.cache import RunCache as _RC
        assert _RC._read_media_ref(uri, offset, length) == img_bytes
    finally:
        ws_mod._remote_for = orig


# ---------------------------------------------------------------------------
# TreeStore over a remote workspace
# ---------------------------------------------------------------------------


def _tree(ws, **kw):
    return TreeStore(ws, "meta", **kw)


def test_remote_writes_are_debounced():
    """A cold start seeds one group per run — that must be one commit."""
    ws = FakeWorkspace()
    t = _tree(ws, debounce=30.0)
    for i in range(20):
        t.seed_run(f"run{i}", "exp/a")
    assert ws.commits == []          # still inside the window

    t.flush()
    assert len(ws.commits) == 1
    assert ws.commits[0][0] == ["meta/tree.json"]


def test_flush_is_a_noop_when_nothing_changed():
    ws = FakeWorkspace()
    t = _tree(ws, debounce=30.0)
    t.flush()
    assert ws.commits == []


def test_debounce_timer_fires_on_its_own():
    ws = FakeWorkspace()
    t = _tree(ws, debounce=0.05)
    t.create_group("exp/a")
    deadline = __import__("time").monotonic() + 5
    while not ws.commits and __import__("time").monotonic() < deadline:
        __import__("time").sleep(0.01)
    assert len(ws.commits) == 1


def test_tree_state_round_trips_through_the_bucket():
    ws = FakeWorkspace()
    t = _tree(ws, debounce=30.0)
    t.create_group("exp/a")
    t.set_run_group("run1", "exp/a")
    t.flush()

    reloaded = _tree(ws, debounce=30.0)
    assert reloaded.to_payload({"run1"})["runs"] == {"run1": "exp/a"}
    assert "exp/a" in reloaded.to_payload({"run1"})["groups"]


def test_doc_writes_are_not_debounced():
    """A PUT must be readable by the next GET."""
    ws = FakeWorkspace()
    t = _tree(ws, debounce=30.0)
    t.set_doc("exp/a", "README.md", "hello")
    assert len(ws.commits) == 1
    assert t.get_doc("exp/a", "README.md") == "hello"


def test_doc_write_carries_pending_tree_changes():
    ws = FakeWorkspace()
    t = _tree(ws, debounce=30.0)
    t.seed_run("run1", "exp/a")          # dirty, deferred
    t.set_doc("exp/a", "README.md", "hi")
    assert len(ws.commits) == 1
    assert set(ws.commits[0][0]) == {"meta/tree.json", "meta/docs/exp/a/README.md"}
    assert _tree(ws, debounce=30.0).to_payload({"run1"})["runs"] == {"run1": "exp/a"}


def test_docs_index_avoids_listing_on_every_read():
    ws = FakeWorkspace()
    t = _tree(ws, debounce=30.0)
    t.set_doc("exp/a", "README.md", "hi")
    t.set_doc("exp/a", "notes.md", "n")

    payload = t.to_payload(set())
    assert payload["groups"]["exp/a"]["docs"] == ["README.md", "notes.md"]


def test_docs_index_is_rebuilt_on_load():
    ws = FakeWorkspace()
    t = _tree(ws, debounce=30.0)
    t.set_doc("exp/a", "README.md", "hi")
    t.set_doc("exp/a", "z.md", "z")
    t.set_doc("", "root.md", "r")
    t.flush()

    reloaded = _tree(ws, debounce=30.0)
    groups = reloaded.to_payload(set())["groups"]
    assert groups["exp/a"]["docs"] == ["README.md", "z.md"]


def test_delete_doc_updates_the_index_and_the_bucket():
    ws = FakeWorkspace()
    t = _tree(ws, debounce=30.0)
    t.set_doc("exp/a", "notes.md", "n")
    assert t.delete_doc("exp/a", "notes.md") is True
    assert t.to_payload(set())["groups"]["exp/a"]["docs"] == []
    assert t.get_doc("exp/a", "notes.md") is None
    assert t.delete_doc("exp/a", "notes.md") is False


def test_move_group_relocates_docs():
    ws = FakeWorkspace()
    t = _tree(ws, debounce=30.0)
    t.set_doc("exp/a", "README.md", "hi")
    t.set_doc("exp/a/deep", "d.md", "deep")
    t.move_group("exp/a", "exp/b")

    assert t.get_doc("exp/b", "README.md") == "hi"
    assert t.get_doc("exp/b/deep", "d.md") == "deep"
    assert t.get_doc("exp/a", "README.md") is None
    groups = t.to_payload(set())["groups"]
    assert groups["exp/b"]["docs"] == ["README.md"]
    assert "exp/a" not in groups


def test_delete_group_removes_its_docs():
    ws = FakeWorkspace()
    t = _tree(ws, debounce=30.0)
    t.set_doc("exp/a", "README.md", "hi")
    t.delete_group("exp/a", set())
    assert t.get_doc("exp/a", "README.md") is None
    assert "exp/a" not in t.to_payload(set())["groups"]


def test_read_only_bucket_degrades_to_ram(caplog):
    """A public dataset with no write token is a supported deployment."""
    ws = FakeWorkspace()
    ws.read_only = True
    t = _tree(ws, debounce=30.0)

    t.create_group("exp/a")
    t.flush()
    t.set_run_group("run1", "exp/a")
    t.flush()

    assert ws.commits == []
    # Still fully usable in memory.
    assert t.to_payload({"run1"})["runs"] == {"run1": "exp/a"}


def test_read_only_bucket_warns_once(caplog):
    import logging

    ws = FakeWorkspace()
    ws.read_only = True
    t = _tree(ws, debounce=30.0)
    with caplog.at_level(logging.WARNING, logger="nebo.server.tree"):
        for i in range(5):
            t.create_group(f"exp/{i}")
            t.flush()
    warnings = [r for r in caplog.records if "cannot write the run tree" in r.message]
    assert len(warnings) == 1


def test_unparseable_remote_tree_refuses_to_start():
    ws = FakeWorkspace(files={"meta/tree.json": b"{not json"})
    with pytest.raises(RuntimeError, match="unparseable"):
        _tree(ws)


def test_seed_run_is_still_seed_once_on_a_bucket():
    ws = FakeWorkspace()
    t = _tree(ws, debounce=30.0)
    t.seed_run("run1", "birth/group")
    t.set_run_group("run1", "moved/here")
    assert t.seed_run("run1", "birth/group") is False
    assert t.to_payload({"run1"})["runs"] == {"run1": "moved/here"}
