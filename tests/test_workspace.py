"""Bucket workspaces (`--logdir hf://...`).

Local workspaces are already covered by the rest of the suite — every
watcher/cache/tree test runs through `LocalWorkspace`. What is untested by
those is the remote path, so these four exercise it end to end against an
in-memory backend (no network, no huggingface_hub).
"""

from __future__ import annotations

import asyncio
import io
import json

import pytest

from nebo.core.fileformat import NeboFileWriter
from nebo.server.cache import RunCache, resolve_cache_path
from nebo.server.daemon import DaemonState
from nebo.server.tree import TreeReadOnly, TreeStore
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

    Deliberately mimics the awkward parts: read-only, no mtimes, a content
    token instead of size-based change detection, a change marker that gates
    listing, and I/O that must be awaited.
    """

    is_remote = True
    writable = False
    default_poll_interval = 30.0

    def __init__(self, uri="hf://datasets/acme/runs", files=None):
        self.uri = uri
        self.files: dict[str, bytes] = dict(files or {})
        self.revision = 0
        self.commits: list[list[str]] = []
        self.deletes: list[str] = []
        self.io_calls = 0
        self.list_calls = 0

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
        # Recorded, not refused: the assertion that matters is that nothing
        # ever calls this, so a silent write would show up as a non-empty list.
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


def test_bucket_run_tree_is_read_only():
    """A .nebo file is an append-only stream and object storage cannot append,
    so the daemon never writes to an archive. What matters is that this costs
    nothing: placements are re-derivable from the run headers, and archived
    curation still loads."""
    ws = FakeWorkspace()
    tree = TreeStore(ws, "meta")
    assert tree.writable is False

    # seed_run restates what the .nebo header already says, so it stays
    # allowed — it just updates RAM and writes nothing.
    for i in range(20):
        assert tree.seed_run(f"run{i}", "exp/a") is True
    assert ws.commits == [], "the daemon must not write to an archive"
    assert tree.to_payload({"run0", "run19"})["runs"] == {
        "run0": "exp/a", "run19": "exp/a",
    }

    # Everything a header cannot express is refused, with a message that says
    # what to do instead.
    for call in (
        lambda: tree.create_group("exp/b"),
        lambda: tree.move_group("exp/a", "exp/b"),
        lambda: tree.delete_group("exp/a", set()),
        lambda: tree.set_run_group("run0", "exp/b"),
        lambda: tree.set_doc("exp/a", "README.md", "x"),
        lambda: tree.delete_doc("exp/a", "README.md"),
    ):
        with pytest.raises(TreeReadOnly, match="NEBO_GROUP"):
            call()
    assert ws.commits == []

    # Curation a publisher archived alongside the runs still loads, and still
    # wins over the header (seed-once), exactly as a local move does.
    curated = FakeWorkspace(files={
        "meta/tree.json": json.dumps({
            "version": 1,
            "groups": {"curated": {}, "exp/a": {}},
            "runs": {"run0": "curated"},
        }).encode(),
        "meta/docs/curated/README.md": b"# hand-written",
    })
    t2 = TreeStore(curated, "meta")
    assert t2.seed_run("run0", "exp/a") is False      # archived placement wins
    assert t2.seed_run("run1", "exp/a") is True       # unseen run seeds from header
    payload = t2.to_payload({"run0", "run1"})
    assert payload["runs"] == {"run0": "curated", "run1": "exp/a"}
    # Doc names come from a RAM index: to_payload runs on every GET /tree and
    # every tree_updated broadcast, so it must not list per group.
    assert payload["groups"]["curated"]["docs"] == ["README.md"]
    assert t2.get_doc("curated", "README.md") == "# hand-written"
    assert curated.commits == []


def test_local_run_tree_stays_fully_writable(tmp_path):
    """The read-only rule is about archives only — a local workspace keeps
    every mutation, and a group move is still a directory rename."""
    tree = TreeStore(tmp_path / "meta")
    assert tree.writable is True

    tree.create_group("exp/a")
    tree.set_doc("exp/a", "README.md", "hi")
    tree.set_run_group("run1", "exp/a")
    tree.move_group("exp/a", "exp/b")

    assert tree.get_doc("exp/b", "README.md") == "hi"
    assert tree.get_doc("exp/a", "README.md") is None
    assert tree.to_payload({"run1"})["runs"] == {"run1": "exp/b"}
    assert (tmp_path / "meta" / "tree.json").is_file()

    # Survives a reload, and a group with no docs deletes cleanly.
    tree.create_group("exp/c")
    tree.delete_group("exp/c", set())
    assert "exp/b" in TreeStore(tmp_path / "meta").to_payload(set())["groups"]
