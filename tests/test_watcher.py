"""Tests for the daemon's directory watcher."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nebo.core.fileformat import NeboFileWriter
from nebo.server.daemon import DaemonState
from nebo.server.watcher import DirectoryWatcher


def _write_run_file(path: Path, run_id: str, events: list[dict]) -> Path:
    filepath = path / f"2026-05-16_120000_{run_id}.nebo"
    f = filepath.open("wb")
    writer = NeboFileWriter(f, run_id=run_id, script_path="/x/s.py")
    writer.write_header()
    for e in events:
        writer.write_entry(e["type"], dict(e))
    writer.close()
    f.close()
    return filepath


@pytest.mark.asyncio
async def test_watcher_picks_up_new_file(tmp_path):
    state = DaemonState()
    watcher = DirectoryWatcher(state, logdir=tmp_path, poll_interval=0.05)
    task = asyncio.create_task(watcher.run())

    _write_run_file(
        tmp_path, "newrun123456",
        [{"type": "text", "loggable_id": "__global__", "message": "hello"}],
    )

    await asyncio.sleep(0.3)
    watcher.stop()
    await task

    # Shallow registration: the run is listed from the header alone; a static
    # file's body is not read until a detail request deepens it.
    assert "newrun123456" in state.runs
    run = state.runs["newrun123456"]
    assert not any(l.message == "hello" for l in run.texts)

    await watcher.ensure_deep("newrun123456")
    assert any(l.message == "hello" for l in state.runs["newrun123456"].texts)


@pytest.mark.asyncio
async def test_watcher_tails_appended_entries(tmp_path):
    state = DaemonState()
    watcher = DirectoryWatcher(state, logdir=tmp_path, poll_interval=0.05)
    task = asyncio.create_task(watcher.run())

    filepath = _write_run_file(
        tmp_path, "tailrun654321",
        [{"type": "text", "loggable_id": "__global__", "message": "first"}],
    )
    await asyncio.sleep(0.2)

    f = filepath.open("ab")
    writer = NeboFileWriter(f, run_id="tailrun654321", script_path="/x/s.py")
    writer.write_entry(
        "text", {"type": "text", "loggable_id": "__global__", "message": "second"},
    )
    writer.close()
    f.close()

    await asyncio.sleep(0.2)
    watcher.stop()
    await task

    msgs = [l.message for l in state.runs["tailrun654321"].texts]
    assert msgs == ["first", "second"]


def _cache_state(tmp_path):
    from nebo.server.cache import RunCache

    cache = RunCache(tmp_path / "cache.db", logdir=tmp_path / "logs")
    cache.start()
    return DaemonState(cache=cache), cache


@pytest.mark.asyncio
async def test_offsets_persist_across_restart(tmp_path):
    logdir = tmp_path / "logs"
    logdir.mkdir()
    state, cache = _cache_state(tmp_path)
    try:
        _write_run_file(
            logdir, "persistrun01",
            [{"type": "text", "loggable_id": "__global__", "message": "one"}],
        )
        watcher = DirectoryWatcher(state, logdir=logdir, poll_interval=0.05)
        await watcher._tick()                     # shallow register
        await watcher.ensure_deep("persistrun01")  # deep-ingest the body
        assert cache.flush()
        assert state.runs["persistrun01"].source == "watcher"
        n_before = cache._read_conn().execute(
            "SELECT COUNT(*) FROM texts"
        ).fetchone()[0]
        assert n_before == 1
        cache.close()

        # "Restart": fresh cache handle + state + watcher over the same db.
        from nebo.server.cache import RunCache

        cache2 = RunCache(tmp_path / "cache.db", logdir=tmp_path / "logs")
        cache2.start()
        state2 = DaemonState(cache=cache2)
        watcher2 = DirectoryWatcher(state2, logdir=logdir, poll_interval=0.05)
        await watcher2._tick()
        assert cache2.flush()
        # Nothing re-ingested: no RAM run materialized, row count unchanged.
        assert "persistrun01" not in state2.runs
        n_after = cache2._read_conn().execute(
            "SELECT COUNT(*) FROM texts"
        ).fetchone()[0]
        assert n_after == n_before
        # And the run is still fully queryable from SQL.
        assert state2.run_summary("persistrun01")["text_count"] == 1
        cache2.close()
    finally:
        if cache._running:
            cache.close()


@pytest.mark.asyncio
async def test_torn_tail_parks_and_resumes(tmp_path):
    logdir = tmp_path / "logs"
    logdir.mkdir()
    state, cache = _cache_state(tmp_path)
    try:
        filepath = _write_run_file(
            logdir, "tornrun00001",
            [{"type": "text", "loggable_id": "__global__", "message": f"m{i}"}
             for i in range(3)],
        )
        whole = filepath.read_bytes()
        # Cut the file mid-way through the last frame.
        filepath.write_bytes(whole[:-7])

        watcher = DirectoryWatcher(state, logdir=logdir, poll_interval=0.05)
        await watcher._tick()                     # shallow register
        # Deepening reads the body but parks at the torn last frame.
        await watcher.ensure_deep("tornrun00001")
        msgs = [l.message for l in state.runs["tornrun00001"].texts]
        assert msgs == ["m0", "m1"]

        # Complete the write; the tail resumes and only the missing entry arrives.
        filepath.write_bytes(whole)
        await watcher._tick()
        msgs = [l.message for l in state.runs["tornrun00001"].texts]
        assert msgs == ["m0", "m1", "m2"]
    finally:
        cache.close()


@pytest.mark.asyncio
async def test_watcher_media_by_reference(tmp_path):
    import base64

    logdir = tmp_path / "logs"
    logdir.mkdir()
    state, cache = _cache_state(tmp_path)
    try:
        png = b"\x89PNG\r\n\x1a\n" + b"z" * 40
        _write_run_file(
            logdir, "mediarun0001",
            [{"type": "image", "loggable_id": "__global__", "name": "f",
              "data": base64.b64encode(png).decode("ascii"), "timestamp": 1.0}],
        )
        watcher = DirectoryWatcher(state, logdir=logdir, poll_interval=0.05)
        await watcher._tick()
        await watcher.ensure_deep("mediarun0001")
        assert cache.flush()

        row = cache._read_conn().execute(
            "SELECT src_path, src_offset, src_length FROM media"
        ).fetchone()
        assert row["src_path"] is not None
        assert row["src_offset"] > 0 and row["src_length"] > len(png)
        n_blobs = cache._read_conn().execute(
            "SELECT COUNT(*) FROM media_blobs"
        ).fetchone()[0]
        assert n_blobs == 0  # by reference, not by copy

        mid = cache._read_conn().execute(
            "SELECT media_id FROM media"
        ).fetchone()["media_id"]
        # Wipe the LRU so the read exercises the file reference.
        state.media_lru.__init__(budget_bytes=1)
        assert state.media_bytes("mediarun0001", mid) == png
    finally:
        cache.close()


@pytest.mark.asyncio
async def test_watcher_ignores_non_nebo_files(tmp_path):
    state = DaemonState()
    (tmp_path / "junk.txt").write_text("not a nebo file")
    watcher = DirectoryWatcher(state, logdir=tmp_path, poll_interval=0.05)
    task = asyncio.create_task(watcher.run())
    await asyncio.sleep(0.2)
    watcher.stop()
    await task
    assert state.runs == {}


@pytest.mark.asyncio
async def test_v4_file_end_to_end(tmp_path):
    """FileTransport (coalesced v4 file, bytes media) -> watcher -> daemon -> cache."""
    from nebo.core.transport import FileTransport

    logdir = tmp_path / "logs"
    logdir.mkdir()
    state, cache = _cache_state(tmp_path)
    try:
        png = b"\x89PNG\r\n\x1a\n" + b"e2e" * 16
        t = FileTransport(logdir=logdir, run_id="v4endtoend01", script_path="/x/s.py")
        try:
            for i in range(6):
                t.send_event({
                    "type": "metric", "loggable_id": "__global__",
                    "name": "loss", "metric_type": "line",
                    "value": 1.0 - i * 0.1, "step": i, "tags": [],
                    "timestamp": 100.0 + i,
                })
            t.send_event({
                "type": "image", "loggable_id": "__global__", "name": "f",
                "data": png, "step": None, "timestamp": 200.0,
            })
            assert t.flush(timeout=2.0)
        finally:
            t.close()

        # The file must actually contain batched frames (not per-point).
        from nebo.core.fileformat import NeboFileReader

        (path,) = logdir.glob("*.nebo")
        with path.open("rb") as f:
            r = NeboFileReader(f)
            r.read_header()
            types = [e["type"] for e in r.read_entries()]
        assert "metric_batch" in types
        assert "metric" not in types

        watcher = DirectoryWatcher(state, logdir=logdir, poll_interval=0.05)
        await watcher._tick()
        await watcher.ensure_deep("v4endtoend01")
        assert cache.flush()

        run = state.runs["v4endtoend01"]
        entries = run.loggables["__global__"].metrics["loss"]["entries"]
        assert [e["step"] for e in entries] == list(range(6))
        assert run.latest_step == 5

        # Media by reference works even for v4 (bin) frames.
        mid = cache._read_conn().execute(
            "SELECT media_id FROM media"
        ).fetchone()["media_id"]
        state.media_lru.__init__(budget_bytes=1)
        assert state.media_bytes("v4endtoend01", mid) == png

        # And SQL rows fanned out per point.
        n = cache._read_conn().execute(
            "SELECT COUNT(*) FROM metrics WHERE name='loss'"
        ).fetchone()[0]
        assert n == 6
    finally:
        cache.close()


@pytest.mark.asyncio
async def test_alert_file_end_to_end(tmp_path):
    """File-mode nb.alert survives its unregistered (byte 255) serialization.

    Alert frames are intentionally NOT in ENTRY_TYPES — they land on disk as
    unknown byte 255 with the full event dict as payload, and the watcher's
    payload-type recovery (`{"type": ..., **entry["payload"]}`) is what
    ingests them. Pins that recovery against anyone making the byte
    authoritative.
    """
    from nebo.core.transport import FileTransport

    logdir = tmp_path / "logs"
    logdir.mkdir()
    state = DaemonState()

    t = FileTransport(logdir=logdir, run_id="alertfileee1", script_path="/x/s.py")
    try:
        t.send_event({
            "type": "alert", "loggable_id": None,
            "data": {"title": "probe", "text": "final check", "level": 30,
                     "level_name": "WARN", "timestamp": 123.0},
        })
        assert t.flush(timeout=2.0)
    finally:
        t.close()

    watcher = DirectoryWatcher(state, logdir=logdir, poll_interval=0.05)
    await watcher._tick()
    await watcher.ensure_deep("alertfileee1")

    run = state.runs["alertfileee1"]
    assert [a["title"] for a in run.alerts] == ["probe"]
    assert run.alerts[0]["level"] == 30
    assert run.significant_events[-1]["type"] == "alert"
