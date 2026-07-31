"""Shallow ingest: header-only registration + deepen-on-growth/read."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nebo.core.fileformat import NeboFileWriter
from nebo.server.cache import RunCache
from nebo.server.daemon import DaemonState, create_daemon_app
from nebo.server.watcher import DirectoryWatcher


def _write(path: Path, run_id: str, events: list[dict], *, ts="120000") -> Path:
    fp = path / f"2026-05-16_{ts}_{run_id}.nebo"
    f = fp.open("wb")
    w = NeboFileWriter(f, run_id=run_id, script_path="/x/s.py")
    w.write_header()
    for e in events:
        w.write_entry(e["type"], dict(e))
    w.close()
    f.close()
    return fp


def _cache_state(tmp_path):
    cache = RunCache(tmp_path / "cache.db", logdir=tmp_path / "logs")
    cache.start()
    return DaemonState(cache=cache), cache


TEXT = {"type": "text", "loggable_id": "__global__", "message": "hi"}
METRIC = {"type": "metric", "loggable_id": "n", "name": "loss",
          "metric_type": "line", "value": 0.5, "step": 0, "tags": [],
          "timestamp": 1.0}


@pytest.mark.asyncio
async def test_header_only_registration(tmp_path):
    state = DaemonState()
    _write(tmp_path, "shallow00001", [METRIC, TEXT])
    watcher = DirectoryWatcher(state, logdir=tmp_path)
    await watcher._tick()
    # Listed from the header, but the body (metric/text) is NOT resident.
    assert "shallow00001" in state.runs
    run = state.runs["shallow00001"]
    assert not run.texts
    assert all(not lg.metrics for lg in run.loggables.values())
    assert "n" not in run.loggables  # body-declared loggable not read yet


@pytest.mark.asyncio
async def test_static_file_stays_shallow(tmp_path):
    state, cache = _cache_state(tmp_path)
    logdir = tmp_path / "logs"
    logdir.mkdir()
    try:
        _write(logdir, "static000001", [TEXT])
        watcher = DirectoryWatcher(state, logdir=logdir)
        await watcher._tick()
        await watcher._tick()
        await watcher._tick()
        assert cache.flush()
        # Still shallow after repeated ticks; body never read.
        assert not state.runs["static000001"].texts
        wf = cache.get_watch_files()
        (info,) = wf.values()
        assert info["shallow"] is True
    finally:
        cache.close()


@pytest.mark.asyncio
async def test_growth_deepens_exactly_once(tmp_path):
    state = DaemonState()
    logdir = tmp_path
    fp = _write(logdir, "growrun00001",
                [{**TEXT, "message": "a"}])
    watcher = DirectoryWatcher(state, logdir=logdir)
    await watcher._tick()  # shallow
    assert not state.runs["growrun00001"].texts

    # Append -> file grows -> deepen reads the whole body once (a + b), no dup.
    f = fp.open("ab")
    w = NeboFileWriter(f, run_id="growrun00001", script_path="/x/s.py")
    w.write_entry("text", {**TEXT, "message": "b"})
    w.close(); f.close()
    await watcher._tick()
    assert [l.message for l in state.runs["growrun00001"].texts] == ["a", "b"]

    # Further growth just tails.
    f = fp.open("ab")
    w = NeboFileWriter(f, run_id="growrun00001", script_path="/x/s.py")
    w.write_entry("text", {**TEXT, "message": "c"})
    w.close(); f.close()
    await watcher._tick()
    assert [l.message for l in state.runs["growrun00001"].texts] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_ensure_deep_idempotent_under_concurrency(tmp_path):
    state = DaemonState()
    _write(tmp_path, "concur000001",
           [{**TEXT, "message": f"m{i}"} for i in range(4)])
    watcher = DirectoryWatcher(state, logdir=tmp_path)
    await watcher._tick()
    # Fire ensure_deep concurrently — the per-run lock must make the body land
    # exactly once (no duplicated entries).
    await asyncio.gather(*[watcher.ensure_deep("concur000001") for _ in range(5)])
    assert [l.message for l in state.runs["concur000001"].texts] == \
        ["m0", "m1", "m2", "m3"]


@pytest.mark.asyncio
async def test_chunked_ingest_equals_one_shot(tmp_path, monkeypatch):
    import nebo.server.watcher as watcher_mod
    monkeypatch.setattr(watcher_mod, "_INGEST_CHUNK", 2)  # force multiple chunks
    state = DaemonState()
    _write(tmp_path, "chunk0000001",
           [{**TEXT, "message": f"m{i}"} for i in range(5)])
    watcher = DirectoryWatcher(state, logdir=tmp_path)
    await watcher._tick()
    await watcher.ensure_deep("chunk0000001")
    assert [l.message for l in state.runs["chunk0000001"].texts] == \
        ["m0", "m1", "m2", "m3", "m4"]


@pytest.mark.asyncio
async def test_run_list_never_deepens_but_detail_does(tmp_path):
    state, cache = _cache_state(tmp_path)
    logdir = tmp_path / "logs"
    logdir.mkdir()
    try:
        _write(logdir, "listrun00001", [TEXT])
        watcher = DirectoryWatcher(state, logdir=logdir)
        state._watcher = watcher  # what the lifespan wires up
        await watcher._tick()     # shallow register

        client = TestClient(create_daemon_app(state))
        # The run appears in the list, and listing does NOT deepen it.
        runs = client.get("/runs").json()["runs"]
        assert any(r["id"] == "listrun00001" for r in runs)
        assert not state.runs["listrun00001"].texts

        # A detail read deepens it.
        texts = client.get("/runs/listrun00001/text").json()["texts"]
        assert texts and texts[-1]["message"] == "hi"
    finally:
        cache.close()
