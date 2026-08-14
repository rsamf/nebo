"""Bucket workspaces (`--logdir hf://...`).

Local workspaces are already covered by the rest of the suite — every
watcher/cache/tree test runs through `LocalWorkspace`. What is untested by
those is the remote path, so these four exercise it end to end against an
in-memory backend (no network, no huggingface_hub).
"""

from __future__ import annotations

import asyncio
import io
import time

import pytest

from nebo.core.fileformat import NeboFileWriter
from nebo.server.cache import RunCache, resolve_cache_path
from nebo.server.daemon import DaemonState
from nebo.server.tree import TreeStore, TreeWriteError
from nebo.server.watcher import DirectoryWatcher
from nebo.server.workspace import (
    FileStat,
    _OffsetStream,
    normalize_workspace,
    open_workspace,
    parse_hf_uri,
)

TEXT = {"type": "text", "loggable_id": "__global__", "message": "hi"}
METRIC = {
    "type": "metric", "loggable_id": "n", "name": "loss", "metric_type": "line",
    "value": 0.5, "step": 0, "tags": [], "timestamp": 1.0,
}


def build_nebo(run_id: str, events: list[dict], started_at: float | None = None) -> bytes:
    buf = io.BytesIO()
    w = NeboFileWriter(buf, run_id=run_id, script_path="/x/s.py")
    if started_at is not None:
        w._started_at = started_at
    w.write_header()
    for e in events:
        w.write_entry(e["type"], dict(e))
    w.close()
    return buf.getvalue()


class FakeWorkspace:
    """An in-memory stand-in that behaves like a bucket.

    Deliberately mimics the awkward parts: no mtimes, a content token
    instead of size-based change detection, a change marker that gates
    listing, and I/O that must be awaited.
    """

    is_remote = True
    default_poll_interval = 30.0

    def __init__(self, uri="hf://datasets/acme/runs", files=None):
        self.uri = uri
        self.files: dict[str, bytes] = dict(files or {})
        self.revision = 0
        self.commits: list[list[str]] = []
        self.deletes: list[str] = []
        self.io_calls = 0
        self.list_calls = 0
        self.read_only = False

    def put(self, name, data):
        self.files[name] = data
        self.revision += 1

    def remove(self, name):
        self.files.pop(name, None)
        self.revision += 1

    def _uri(self, name):
        return f"{self.uri}/{name}"

    def _name(self, uri):
        return uri[len(self.uri) + 1:] if uri.startswith(self.uri + "/") else uri

    def ensure_root(self):
        pass

    def change_token(self):
        return str(self.revision)

    def list_nebo(self):
        self.list_calls += 1
        return [
            FileStat(
                uri=self._uri(n), size=len(d), mtime=None,   # buckets need no mtime
                token=f"blob{hash(d) & 0xffff:04x}",
            )
            for n, d in sorted(self.files.items()) if n.endswith(".nebo")
        ]

    def stat(self, uri):
        data = self.files.get(self._name(uri))
        if data is None:
            return None
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
        return None if data is None else data[offset:offset + length]

    def read_bytes(self, rel):
        return self.files.get(rel.strip("/"))

    def list_dir(self, rel):
        prefix = f"{rel.strip('/')}/" if rel.strip("/") else ""
        return sorted(
            n[len(prefix):] for n in self.files
            if n.startswith(prefix) and "/" not in n[len(prefix):]
        )

    def list_tree(self, rel):
        prefix = f"{rel.strip('/')}/" if rel.strip("/") else ""
        return sorted(n[len(prefix):] for n in self.files if n.startswith(prefix))

    def commit(self, adds, deletes, *, message="nebo: update"):
        if self.read_only:
            raise PermissionError("403 Forbidden")
        self.commits.append([r for r, _ in adds])
        self.deletes.extend(deletes)
        for rel in deletes:
            rel = rel.strip("/")
            for n in [k for k in self.files if k == rel or k.startswith(rel + "/")]:
                self.files.pop(n)
        for rel, data in adds:
            self.files[rel.strip("/")] = data
        self.revision += 1

    async def run_io(self, fn, *args):
        self.io_calls += 1
        return await asyncio.to_thread(fn, *args)


def test_a_bucket_logdir_never_degrades_into_a_local_path(monkeypatch, tmp_path):
    """`Path("hf://x").resolve()` silently yields `$CWD/hf:/x`.

    Cache identity is a sha1 of this string and `_meta_matches` drops the
    entire database when it differs, so a cwd-dependent key would wipe a
    daemon's cache every time it started from a different directory.
    """
    uri = "hf://datasets/acme/runs"

    assert normalize_workspace(uri) == uri
    assert normalize_workspace(uri + "/") == uri            # canonical
    assert normalize_workspace(str(tmp_path)) == str(tmp_path.resolve())
    assert normalize_workspace(None) == ""                  # historical "no logdir"

    # Stable across machines and working directories.
    before = resolve_cache_path(uri)
    monkeypatch.chdir(tmp_path)
    assert resolve_cache_path(uri) == before
    assert resolve_cache_path(uri + "/") == before
    assert resolve_cache_path("hf://datasets/acme/other") != before

    ref = parse_hf_uri("hf://datasets/acme/runs@main/docs")
    assert (ref.repo_type, ref.repo_id, ref.revision, ref.prefix) == (
        "dataset", "acme/runs", "main", "docs"
    )
    assert parse_hf_uri(ref.uri).uri == ref.uri             # round-trips
    assert ref.join("a.nebo").fs_path == "datasets/acme/runs@main/docs/a.nebo"

    ws = open_workspace(uri)
    assert ws.is_remote and ws.uri == uri
    assert ws.default_poll_interval == 30.0                 # HTTP, not scandir
    assert open_workspace(str(tmp_path)).default_poll_interval == 0.5


@pytest.mark.asyncio
async def test_watcher_serves_runs_out_of_a_bucket():
    """The whole read path: list -> header-only register -> deep ingest ->
    media resolved by reference."""
    img_bytes = b"\x89PNG\r\n\x1a\n-payload"
    img = {
        "type": "image", "loggable_id": "n", "name": "hero.png",
        "data": img_bytes, "step": 0, "timestamp": 1.0,
    }
    ws = FakeWorkspace(files={
        "a.nebo": build_nebo("bucketaaaaa1", [METRIC, TEXT, img], started_at=1234.5),
    })
    state = DaemonState()
    w = DirectoryWatcher(state, logdir=ws)
    await w._tick()

    # Registered from the header alone — the body is not resident yet. This
    # is what lets a cold-started daemon list 1000 runs cheaply.
    assert "bucketaaaaa1" in state.runs
    assert not state.runs["bucketaaaaa1"].texts
    assert ws.io_calls > 0, "blocking HTTP must not run on the event loop"
    # No mtime in the listing, so recency falls back to the header.
    assert state.runs["bucketaaaaa1"].last_event_at == 1234.5

    # An unchanged bucket costs one commit-marker check, not a full listing.
    await w._tick()
    await w._tick()
    assert ws.list_calls == 1

    # A listing that fails must not advance the change marker, or every file
    # in that commit stays invisible until some later commit bumps the sha.
    ws.put("b.nebo", build_nebo("bucketaaaaa9", [TEXT]))
    real_list, boom = ws.list_nebo, [True]

    def flaky():
        if boom[0]:
            boom[0] = False
            raise RuntimeError("502 from the Hub")
        return real_list()

    ws.list_nebo = flaky
    await w._tick()                                  # fails
    await w._tick()                                  # retries, must re-list
    ws.list_nebo = real_list
    assert "bucketaaaaa9" in state.runs

    uri = ws._uri("a.nebo")
    header_end = w._tracked[uri].offset

    await w.ensure_deep("bucketaaaaa1")
    run = state.runs["bucketaaaaa1"]
    assert [t.message for t in run.texts] == ["hi"]
    assert "n" in run.loggables

    # Media is stored as a (uri, offset, length) reference, and the uri must
    # be self-describing: the cache resolves it with no workspace in hand.
    with ws.reader(uri, header_end) as f:
        batch, _, _ = w._read_chunk(f, uri)
    src = next((e["_media_src"] for e in batch if "_media_src" in e), None)
    assert src is not None and src[0] == "hf://datasets/acme/runs/a.nebo"

    from nebo.server import workspace as ws_mod
    monkey = ws_mod._remote_for
    ws_mod._remote_for = lambda _u: ws
    try:
        assert RunCache._read_media_ref(*src) == img_bytes
    finally:
        ws_mod._remote_for = monkey


@pytest.mark.asyncio
async def test_republished_objects_do_not_resume_mid_file(tmp_path):
    """Object storage replaces objects; it never appends to them.

    A changed content marker means "different file at the same path", so a
    persisted byte offset now points into unrelated bytes. Same for a path
    that disappears and later comes back.
    """
    cache = RunCache(tmp_path / "cache.db", logdir="hf://datasets/acme/runs")
    cache.start()
    try:
        ws = FakeWorkspace(files={"a.nebo": build_nebo("bucketaaaaa2", [TEXT])})
        state = DaemonState(cache=cache)
        w = DirectoryWatcher(state, logdir=ws)
        await w._tick()
        uri = ws._uri("a.nebo")
        header_end = w._tracked[uri].offset
        cache.flush()
        assert list(cache.get_watch_files()) == [uri]

        # Same path, different bytes -> back to header-only, not tailed.
        ws.put("a.nebo", build_nebo("bucketaaaaa3", [TEXT, TEXT, METRIC]))
        await w._tick()
        assert w._tracked[uri].shallow is True
        assert w._tracked[uri].offset == header_end
        await w.ensure_deep("bucketaaaaa3")
        assert len(state.runs["bucketaaaaa3"].texts) == 2   # ingested once

        # An unreadable root is not an empty one: a listing that raises must
        # leave every offset alone, or a transiently missing directory reaps
        # the lot and re-ingests it on the way back.
        ws.list_nebo = lambda: (_ for _ in ()).throw(OSError("root gone"))
        await w._tick()
        assert len(w._tracked) == 1

        # Gone from the bucket -> forget the offset, in RAM and in the cache.
        del ws.list_nebo
        ws.remove("a.nebo")
        await w._tick()
        cache.flush()
        assert w._tracked == {}
        assert cache.get_watch_files() == {}
    finally:
        cache.close()


def test_bucket_run_tree_coalesces_writes_and_survives_a_read_only_repo(caplog):
    """Every tree write is an HTTP commit, so a cold start seeding one group
    per run must not be one commit per run — and a repo the daemon can read
    but not write has to keep working rather than fail the request."""
    ws = FakeWorkspace()
    tree = TreeStore(ws, "meta", debounce=30.0)

    for i in range(20):
        tree.seed_run(f"run{i}", "exp/a")
    assert ws.commits == []                     # still inside the window
    tree.flush()
    assert ws.commits == [["meta/tree.json"]]   # one commit, not twenty
    tree.flush()
    assert len(ws.commits) == 1                 # nothing pending -> no-op

    # A doc write bypasses the timer (a PUT must be visible to the next GET)
    # and carries the pending tree change with it.
    tree.seed_run("run99", "exp/b")
    tree.set_doc("exp/a", "README.md", "hello")
    assert len(ws.commits) == 2
    assert set(ws.commits[1]) == {"meta/tree.json", "meta/docs/exp/a/README.md"}
    assert tree.get_doc("exp/a", "README.md") == "hello"

    # Doc names come from a RAM index: to_payload() runs on every GET /tree
    # and every tree_updated broadcast, so it must not list per group.
    reloaded = TreeStore(ws, "meta", debounce=30.0)
    payload = reloaded.to_payload({"run0", "run99"})
    assert payload["groups"]["exp/a"]["docs"] == ["README.md"]
    assert payload["runs"] == {"run0": "exp/a", "run99": "exp/b"}

    # Timer path (not just flush()).
    ticking = TreeStore(ws, "meta", debounce=0.05)
    ticking.create_group("exp/c")
    deadline = time.monotonic() + 5
    while len(ws.commits) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(ws.commits) == 3

    # Deletes are only emitted for docs folders that exist: the Hub rejects a
    # commit deleting a missing path, which would take the bundled tree.json
    # write down with it.
    docless = FakeWorkspace()
    d = TreeStore(docless, "meta", debounce=30.0)
    d.create_group("exp/a")
    d.flush()
    d.move_group("exp/a", "exp/b")
    d.create_group("exp/c")
    d.flush()
    d.delete_group("exp/c", set())
    assert docless.deletes == [], "no docs -> nothing to delete"
    d.set_doc("exp/b", "README.md", "hi")
    d.move_group("exp/b", "exp/d")
    assert docless.deletes == ["meta/docs/exp/b/"]
    assert d.get_doc("exp/d", "README.md") == "hi"

    # A public repo with no write token: degrade to RAM, warn once — but a
    # doc write has no RAM fallback, so it must report failure rather than
    # advertise a doc that reads back 404.
    ws.read_only = True
    ro = TreeStore(ws, "meta", debounce=30.0)
    with caplog.at_level("WARNING", logger="nebo.server.tree"):
        for i in range(5):
            ro.create_group(f"ro/{i}")
            ro.flush()
    assert ro.to_payload(set())["groups"].keys() >= {"ro/0", "ro/4"}
    assert sum("cannot write the run tree" in r.message for r in caplog.records) == 1
    with pytest.raises(TreeWriteError):
        ro.set_doc("ro/0", "README.md", "nope")
    assert ro.to_payload(set())["groups"]["ro/0"]["docs"] == []

    # The warning latch clears on success, so a later outage is not silent.
    ws.read_only = False
    ro.create_group("ro/back")
    ro.flush()
    ws.read_only = True
    with caplog.at_level("WARNING", logger="nebo.server.tree"):
        caplog.clear()
        ro.create_group("ro/again")
        ro.flush()
    assert sum("cannot write the run tree" in r.message for r in caplog.records) == 1
