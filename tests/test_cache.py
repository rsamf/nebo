"""Tests for the daemon's SQLite write-behind cache (nebo/server/cache.py)."""

from __future__ import annotations

import os
import time

import pytest

from nebo.server.cache import (
    SCHEMA_VERSION,
    RunCache,
    resolve_cache_path,
    sweep_cache_dir,
)


def _mk(tmp_path) -> RunCache:
    c = RunCache(tmp_path / "cache.db", logdir=tmp_path / "logs")
    c.start()
    return c


class TestRunCacheCore:
    def test_schema_created_with_meta(self, tmp_path):
        c = _mk(tmp_path)
        try:
            row = c._read_conn().execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            assert row[0] == SCHEMA_VERSION
        finally:
            c.close()

    def test_logdir_recorded_in_meta(self, tmp_path):
        c = _mk(tmp_path)
        try:
            row = c._read_conn().execute(
                "SELECT value FROM meta WHERE key='logdir'"
            ).fetchone()
            assert row[0].endswith("logs")
        finally:
            c.close()

    def test_logdir_mismatch_recreates(self, tmp_path):
        c = _mk(tmp_path)
        c.enqueue(("text_row", "r1", "__global__", "text", 1.0, None, "x"))
        assert c.flush()
        c.close()

        c2 = RunCache(tmp_path / "cache.db", logdir=tmp_path / "other")
        c2.start()
        try:
            row = c2._read_conn().execute(
                "SELECT value FROM meta WHERE key='logdir'"
            ).fetchone()
            assert row[0].endswith("other")
            # Recreated from scratch: the old row is gone.
            n = c2._read_conn().execute("SELECT COUNT(*) FROM texts").fetchone()[0]
            assert n == 0
        finally:
            c2.close()

    def test_reopen_same_logdir_preserves_data(self, tmp_path):
        c = _mk(tmp_path)
        c.enqueue(("text_row", "r1", "__global__", "text", 1.0, None, "x"))
        assert c.flush()
        c.close()

        c2 = _mk(tmp_path)
        try:
            n = c2._read_conn().execute("SELECT COUNT(*) FROM texts").fetchone()[0]
            assert n == 1
        finally:
            c2.close()

    def test_write_behind_flush_barrier(self, tmp_path):
        c = _mk(tmp_path)
        try:
            c.enqueue(("text_row", "r1", "__global__", "text", 1.0, None, "hello"))
            assert c.flush(timeout=5.0)
            n = c._read_conn().execute(
                "SELECT COUNT(*) FROM texts WHERE run_id='r1'"
            ).fetchone()[0]
            assert n == 1
        finally:
            c.close()

    def test_close_flushes_pending(self, tmp_path):
        c = _mk(tmp_path)
        c.enqueue(("text_row", "r1", "__global__", "text", 1.0, None, "bye"))
        c.close()
        import sqlite3

        conn = sqlite3.connect(tmp_path / "cache.db")
        try:
            n = conn.execute("SELECT COUNT(*) FROM texts").fetchone()[0]
            assert n == 1
        finally:
            conn.close()


def _seed_small_run(c: RunCache, run_id: str = "r1") -> None:
    """Enqueue a realistic little run through the op vocabulary."""
    import json

    c.enqueue(("run_upsert", run_id, {
        "script_path": "train.py",
        "run_name": "exp-1",
        "args_json": json.dumps(["--fast"]),
        "started_at": 100.0,
        "source": "network",
        "edges_json": json.dumps([{"source": "a", "target": "b"}]),
        "workflow_description": "desc",
        "run_config_json": json.dumps({"lr": 0.1}),
    }))
    c.enqueue(("loggable_upsert", run_id, "a", {
        "kind": "node", "func_name": "a", "docstring": "da",
        "exec_count": 2, "is_source": 1,
    }))
    c.enqueue(("loggable_upsert", run_id, "b", {
        "kind": "node", "func_name": "b", "exec_count": 1, "is_source": 0,
    }))
    for i in range(3):
        c.enqueue(("metric_row", run_id, "a", "loss", "line",
                   i, 10.0 + i, json.dumps(0.5 - i * 0.1), json.dumps(["train"]), None))
    # Snapshot emitted twice: only the second survives.
    c.enqueue(("metric_snapshot", run_id, "a", "dist", "bar",
               None, 11.0, json.dumps({"x": 1}), json.dumps([]), None))
    c.enqueue(("metric_snapshot", run_id, "a", "dist", "bar",
               None, 12.0, json.dumps({"x": 2}), json.dumps([]), None))
    c.enqueue(("text_row", run_id, "a", "text", 10.5, 1, "hello"))
    c.enqueue(("text_row", run_id, "__global__", "text", 10.6, None, "world"))
    c.enqueue(("alert_row", run_id, 10.8, json.dumps({
        "title": "high loss", "level": 30, "triggered_by": "code",
        "timestamp": 10.8, "loggable_id": "a", "text": "",
    })))
    c.enqueue(("sig_event", run_id, 10.8, "alert", json.dumps({
        "type": "alert", "timestamp": 10.8, "loggable_id": "a",
        "message": "high loss",
    })))
    assert c.flush()


class TestOpsAndAccessors:
    def test_summary_shape(self, tmp_path):
        c = _mk(tmp_path)
        try:
            _seed_small_run(c)
            s = c.get_summary("r1")
            assert s["id"] == "r1"
            assert s["script_path"] == "train.py"
            assert s["run_name"] == "exp-1"
            assert s["args"] == ["--fast"]
            assert s["started_at"] is not None
            assert s["last_event_at"] is None  # not seeded here; no ended_at
            assert s["node_count"] == 2
            assert s["edge_count"] == 1
            assert s["text_count"] == 2
            assert s["metrics_index"] == {"a": ["dist", "loss"]}
            assert s["metric_series_count"] == 2
            assert s["latest_step"] == 2
            assert s["run_config"] == {"lr": 0.1}
            assert c.get_summary("nope") is None
            assert [r["id"] for r in c.list_summaries()] == ["r1"]
            assert c.has_run("r1") and not c.has_run("nope")
        finally:
            c.close()

    def test_graph_shape(self, tmp_path):
        c = _mk(tmp_path)
        try:
            _seed_small_run(c)
            g = c.get_graph("r1")
            assert set(g["nodes"]) == {"a", "b"}
            assert g["nodes"]["a"]["func_name"] == "a"
            assert g["nodes"]["a"]["exec_count"] == 2
            assert g["nodes"]["b"]["is_source"] is False
            assert g["edges"] == [{"source": "a", "target": "b"}]
            assert g["workflow_description"] == "desc"
            assert g["run_config"] == {"lr": 0.1}
        finally:
            c.close()

    def test_texts_and_alerts(self, tmp_path):
        c = _mk(tmp_path)
        try:
            _seed_small_run(c)
            texts = c.get_texts("r1")
            assert [t["message"] for t in texts] == ["hello", "world"]
            assert texts[0]["loggable_id"] == "a"
            assert texts[0]["step"] == 1
            assert "level" not in texts[0]
            only_a = c.get_texts("r1", loggable_id="a")
            assert len(only_a) == 1
            assert c.get_texts("r1", limit=1)[0]["message"] == "world"
            alerts = c.get_alerts("r1")
            assert alerts[0]["title"] == "high loss"
        finally:
            c.close()

    def test_metrics_shapes(self, tmp_path):
        c = _mk(tmp_path)
        try:
            _seed_small_run(c)
            m = c.get_metrics("r1")
            loss = m["a"]["loss"]
            assert loss["type"] == "line"
            assert [e["step"] for e in loss["entries"]] == [0, 1, 2]
            assert loss["entries"][0]["value"] == 0.5
            assert loss["entries"][0]["tags"] == ["train"]
            dist = m["a"]["dist"]
            assert dist["type"] == "bar"
            assert len(dist["entries"]) == 1
            assert dist["entries"][0]["value"] == {"x": 2}
        finally:
            c.close()

    def test_loggable_shape(self, tmp_path):
        c = _mk(tmp_path)
        try:
            _seed_small_run(c)
            lg = c.get_loggable("r1", "a")
            assert lg["loggable_id"] == "a"
            assert lg["kind"] == "node"
            assert lg["exec_count"] == 2
            assert "loss" in lg["metrics"]
            assert [l["message"] for l in lg["recent_texts"]] == ["hello"]
            assert c.get_loggable("r1", "zzz") is None
        finally:
            c.close()

    def test_ingest_state(self, tmp_path):
        c = _mk(tmp_path)
        try:
            _seed_small_run(c)
            st = c.get_run_ingest_state("r1")
            assert st["series_types"] == {"a": {"loss": "line", "dist": "bar"}}
            assert st["loggables"]["a"]["exec_count"] == 2
            assert st["edges"] == [{"source": "a", "target": "b"}]
            assert st["latest_step"] == 2
            assert st["counts"]["texts"] == 2
            assert st["run_row"]["script_path"] == "train.py"
            assert c.get_run_ingest_state("nope") is None
        finally:
            c.close()

    def test_watch_files_roundtrip(self, tmp_path):
        c = _mk(tmp_path)
        try:
            c.enqueue(("watch_file", "/tmp/a.nebo", "r1", 100, 120, 5.0, True))
            c.enqueue(("watch_file", "/tmp/a.nebo", "r1", 200, 220, 6.0, False))
            assert c.flush()
            wf = c.get_watch_files()
            assert wf["/tmp/a.nebo"]["offset"] == 200
            assert wf["/tmp/a.nebo"]["run_id"] == "r1"
            assert wf["/tmp/a.nebo"]["shallow"] is False
        finally:
            c.close()


class TestMediaStore:
    def test_media_id_deterministic(self):
        from nebo.server.cache import media_id_for

        a = media_id_for(b"hello")
        assert a == media_id_for(b"hello")
        assert a != media_id_for(b"world")
        assert len(a) == 16

    def test_lru_evicts_by_bytes(self):
        from nebo.server.cache import MediaLRU

        lru = MediaLRU(budget_bytes=10)
        lru.put("a", b"12345")
        lru.put("b", b"12345")
        assert lru.get("a") == b"12345"
        lru.put("c", b"12345")  # over budget: least-recently-used ("b") goes
        assert lru.get("b") is None
        assert lru.get("a") == b"12345"
        assert lru.get("c") == b"12345"

    def test_blob_roundtrip(self, tmp_path):
        from nebo.server.cache import media_id_for

        c = _mk(tmp_path)
        try:
            data = b"\x89PNG fake bytes"
            mid = media_id_for(data)
            c.enqueue(("media_blob", mid, data))
            c.enqueue(("media_occurrence", "r1", "a", mid, "image",
                       "frame", 0, 1.0, None, None, None, None, None))
            assert c.flush()
            # Cold read (LRU empty): comes from the blob table.
            assert c.get_media(mid) == data
            listing = c.list_media("r1", "image")
            assert listing["a"][0]["media_id"] == mid
            assert listing["a"][0]["name"] == "frame"
        finally:
            c.close()

    def test_ref_roundtrip_via_nebo_file(self, tmp_path):
        import base64

        from nebo.core.fileformat import NeboFileReader, NeboFileWriter
        from nebo.server.cache import media_id_for

        png = b"\x89PNG\r\n\x1a\n" + b"x" * 64
        path = tmp_path / "run.nebo"
        with path.open("wb") as f:
            w = NeboFileWriter(f, run_id="r1", script_path="s.py")
            w.write_header()
            w.write_entry("text", {"type": "text", "message": "before"})
            w.write_entry("image", {
                "type": "image", "loggable_id": "a", "name": "frame",
                "data": base64.b64encode(png).decode("ascii"),
                "step": None, "timestamp": 1.0,
            })

        with path.open("rb") as f:
            reader = NeboFileReader(f)
            reader.read_header()
            frames = list(reader.read_entries_incremental())
        image_frames = [x for x in frames if x[0]["type"] == "image"]
        assert len(image_frames) == 1
        _, start, end = image_frames[0]

        c = _mk(tmp_path)
        try:
            mid = media_id_for(png)
            c.enqueue(("media_occurrence", "r1", "a", mid, "image",
                       "frame", None, 1.0, None, None,
                       str(path), start, end - start))
            assert c.flush()
            assert c.get_media(mid) == png
            # Second read is served from the LRU (delete the file to prove it).
            path.unlink()
            assert c.get_media(mid) == png
        finally:
            c.close()

    def test_get_media_unknown(self, tmp_path):
        c = _mk(tmp_path)
        try:
            assert c.get_media("nope") is None
        finally:
            c.close()


class TestIncrementalReader:
    def _write_file(self, path, n_entries=3):
        from nebo.core.fileformat import NeboFileWriter

        with path.open("wb") as f:
            w = NeboFileWriter(f, run_id="r1", script_path="s.py")
            w.write_header()
            for i in range(n_entries):
                w.write_entry("text", {"type": "text", "message": f"m{i}"})
        return path

    def test_yields_entries_with_offsets(self, tmp_path):
        from nebo.core.fileformat import NeboFileReader

        path = self._write_file(tmp_path / "a.nebo")
        with path.open("rb") as f:
            r = NeboFileReader(f)
            r.read_header()
            header_end = f.tell()
            items = list(r.read_entries_incremental())
        assert len(items) == 3
        assert items[0][1] == header_end
        # Frames tile the file exactly.
        for (_, s, e), (_, s2, _e2) in zip(items, items[1:]):
            assert e == s2
        assert items[-1][2] == path.stat().st_size

    def test_truncated_tail_stops_cleanly(self, tmp_path):
        from nebo.core.fileformat import NeboFileReader

        path = self._write_file(tmp_path / "a.nebo")
        whole = path.read_bytes()
        # Chop the last frame in half.
        with path.open("rb") as f:
            r = NeboFileReader(f)
            r.read_header()
            items = list(r.read_entries_incremental())
        last_start = items[-1][1]
        cut = last_start + (items[-1][2] - last_start) // 2
        path.write_bytes(whole[:cut])

        with path.open("rb") as f:
            r = NeboFileReader(f)
            r.read_header()
            partial = list(r.read_entries_incremental())
            resume_at = f.tell()
        assert len(partial) == 2
        assert resume_at == last_start  # parked at the torn frame

        # Complete the file again: resuming from the parked offset yields
        # exactly the missing entry.
        path.write_bytes(whole)
        with path.open("rb") as f:
            f.seek(resume_at)
            r = NeboFileReader(f)
            rest = list(r.read_entries_incremental())
        assert len(rest) == 1
        assert rest[0][0]["payload"]["message"] == "m2"


def _mk_state(tmp_path):
    from nebo.server.daemon import DaemonState

    c = RunCache(tmp_path / "cache.db", logdir=tmp_path / "logs")
    c.start()
    return DaemonState(cache=c), c


def _events_small_run():
    import base64

    png = b"\x89PNG\r\n\x1a\n" + b"y" * 32
    return png, [
        {"type": "run_start", "data": {"script_path": "train.py", "run_name": "exp"}},
        {"type": "loggable_register", "loggable_id": "a",
         "data": {"loggable_id": "a", "kind": "node", "func_name": "a"}},
        {"type": "metric", "loggable_id": "a", "name": "loss", "metric_type": "line",
         "value": 0.5, "step": 0, "tags": ["train"], "timestamp": 1.0},
        {"type": "metric", "loggable_id": "a", "name": "loss", "metric_type": "line",
         "value": 0.4, "step": 1, "tags": [], "timestamp": 2.0},
        {"type": "metric", "loggable_id": "a", "name": "dist", "metric_type": "bar",
         "value": {"x": 1}, "step": None, "tags": [], "timestamp": 3.0},
        {"type": "text", "loggable_id": "a", "name": "text", "message": "hi",
         "step": None, "timestamp": 4.0},
        {"type": "image", "loggable_id": "a", "name": "frame",
         "data": base64.b64encode(png).decode("ascii"), "step": 1, "timestamp": 5.0},
    ]


class TestDaemonCacheIngest:
    @pytest.mark.asyncio
    async def test_write_through(self, tmp_path):
        from nebo.server.cache import media_id_for

        state, c = _mk_state(tmp_path)
        png, events = _events_small_run()
        try:
            await state.ingest_events(events, run_id="r1")
            assert c.flush()
            m = c.get_metrics("r1")
            assert [e["value"] for e in m["a"]["loss"]["entries"]] == [0.5, 0.4]
            assert m["a"]["dist"]["entries"][0]["value"] == {"x": 1}
            assert c.get_texts("r1")[0]["message"] == "hi"
            s = c.get_summary("r1")
            assert s["script_path"] == "train.py"
            assert s["run_name"] == "exp"
            listing = c.list_media("r1", "image")
            mid = listing["a"][0]["media_id"]
            assert mid == media_id_for(png)
            assert c.get_media(mid) == png
            # Broadcast event carries media_id, not the payload.
            assert events[-1]["media_id"] == mid
            assert "data" not in events[-1]
        finally:
            c.close()

    @pytest.mark.asyncio
    async def test_counters_maintained(self, tmp_path):
        state, c = _mk_state(tmp_path)
        _, events = _events_small_run()
        try:
            await state.ingest_events(events, run_id="r1")
            run = state.runs["r1"]
            assert run.ram_complete is True
            assert run.latest_step == 1
            assert run.resident_points == 4  # 3 metric entries + 1 text
            assert run.last_event_at > 0
            assert run.get_summary()["latest_step"] == 1
        finally:
            c.close()

    @pytest.mark.asyncio
    async def test_rehydration_after_eviction(self, tmp_path):
        state, c = _mk_state(tmp_path)
        _, events = _events_small_run()
        try:
            await state.ingest_events(events, run_id="r1")
            assert c.flush()
            del state.runs["r1"]
            state.active_run_id = None

            await state.ingest_events([
                {"type": "metric", "loggable_id": "a", "name": "loss",
                 "metric_type": "line", "value": 0.3, "step": 2,
                 "tags": [], "timestamp": 6.0},
            ], run_id="r1")
            assert list(state.runs) == ["r1"]
            run = state.runs["r1"]
            assert run.ram_complete is False
            assert run.script_path == "train.py"
            # Series type lock survived rehydration.
            assert run.loggables["a"].metrics["loss"]["type"] == "line"
            assert run.loggables["a"].exec_count == 0
            assert c.flush()
            entries = c.get_metrics("r1")["a"]["loss"]["entries"]
            assert [e["step"] for e in entries] == [0, 1, 2]
            # SQL sees exactly one run.
            assert [s["id"] for s in c.list_summaries()] == ["r1"]
        finally:
            c.close()

    @pytest.mark.asyncio
    async def test_no_cache_fallback_media(self, tmp_path):
        from nebo.server.daemon import DaemonState

        state = DaemonState()
        png, events = _events_small_run()
        await state.ingest_events(events, run_id="r1")
        mid = events[-1]["media_id"]
        assert state.media_bytes("r1", mid) == png


def _norm_summary(s: dict) -> dict:
    """started_at is an ISO string, last_event_at is epoch seconds; compare
    both coarsely so RAM/SQL float roundtrips don't cause spurious diffs."""
    from datetime import datetime

    out = dict(s)
    v = out.pop("started_at", None)
    out["_started_at_s"] = (
        round(datetime.fromisoformat(v).timestamp(), 2) if v else None
    )
    le = out.pop("last_event_at", None)
    out["_last_event_at_s"] = round(le, 2) if le is not None else None
    return out


class TestSqlReadParity:
    ENDPOINTS = [
        "/runs/r1",
        "/runs/r1/graph",
        "/runs/r1/text",
        "/runs/r1/metrics",
        "/runs/r1/images",
        "/runs/r1/audio",
        "/runs/r1/loggables/a",
    ]

    def _client(self, tmp_path):
        from fastapi.testclient import TestClient

        from nebo.server.daemon import create_daemon_app

        state, c = _mk_state(tmp_path)
        app = create_daemon_app(state)
        return state, c, TestClient(app)

    def test_parity_after_eviction(self, tmp_path):
        state, c, client = self._client(tmp_path)
        _, events = _events_small_run()
        events.append({"type": "edge", "data": {"source": "a", "target": "a"}})
        try:
            resp = client.post("/events?run_id=r1", json=events)
            assert resp.status_code == 200
            before = {ep: client.get(ep).json() for ep in self.ENDPOINTS}
            before_runs = client.get("/runs").json()

            assert c.flush()
            # Use the real eviction path (not a bare `del`) so the run's
            # final last_event_at is flushed to SQL and parity holds.
            state._evict_run("r1")

            after = {ep: client.get(ep).json() for ep in self.ENDPOINTS}
            after_runs = client.get("/runs").json()

            for ep in self.ENDPOINTS:
                b, a = before[ep], after[ep]
                if ep == "/runs/r1":
                    assert _norm_summary(a) == _norm_summary(b), ep
                else:
                    assert a == b, ep
            assert [_norm_summary(r) for r in after_runs["runs"]] == [
                _norm_summary(r) for r in before_runs["runs"]
            ]
        finally:
            c.close()

    def test_media_raw_bytes_with_etag(self, tmp_path):
        state, c, client = self._client(tmp_path)
        png, events = _events_small_run()
        try:
            client.post("/events?run_id=r1", json=events)
            mid = client.get("/runs/r1/images").json()["images"]["a"][0]["media_id"]
            resp = client.get(f"/runs/r1/media/{mid}")
            assert resp.status_code == 200
            assert resp.content == png
            assert resp.headers["content-type"].startswith("image/png")
            assert resp.headers["etag"] == mid
            assert "immutable" in resp.headers["cache-control"]

            resp304 = client.get(
                f"/runs/r1/media/{mid}", headers={"If-None-Match": mid}
            )
            assert resp304.status_code == 304

            assert client.get("/runs/r1/media/nope").status_code == 404
        finally:
            c.close()

    def test_media_survives_eviction(self, tmp_path):
        state, c, client = self._client(tmp_path)
        png, events = _events_small_run()
        try:
            client.post("/events?run_id=r1", json=events)
            mid = client.get("/runs/r1/images").json()["images"]["a"][0]["media_id"]
            assert c.flush()
            del state.runs["r1"]
            state.media_lru.__init__(budget_bytes=1)  # wipe the LRU too
            resp = client.get(f"/runs/r1/media/{mid}")
            assert resp.status_code == 200
            assert resp.content == png
        finally:
            c.close()


class TestJanitor:
    @pytest.mark.asyncio
    async def _seed(self, tmp_path, run_id="r1", completed=False):
        state, c = _mk_state(tmp_path)
        _, events = _events_small_run()
        await state.ingest_events(events, run_id=run_id)
        if completed:
            await state.ingest_events(
                [{"type": "run_completed", "data": {}}], run_id=run_id
            )
        return state, c

    @pytest.mark.asyncio
    async def test_idle_run_evicts_after_threshold(self, tmp_path):
        state, c = await self._seed(tmp_path, completed=True)
        try:
            now = state.runs["r1"].last_event_at
            result = state.janitor_pass(now=now + state.EVICT_IDLE_S + 1)
            assert result["evicted"] == ["r1"]
            assert "r1" not in state.runs
            # Reads still served from SQL.
            assert state.run_summary("r1")["script_path"] == "train.py"
        finally:
            c.close()

    @pytest.mark.asyncio
    async def test_idle_run_survives_within_window(self, tmp_path):
        state, c = await self._seed(tmp_path, completed=True)
        try:
            now = state.runs["r1"].last_event_at
            result = state.janitor_pass(now=now + state.EVICT_IDLE_S - 1)
            assert result["evicted"] == []
            assert "r1" in state.runs
        finally:
            c.close()

    @pytest.mark.asyncio
    async def test_idle_eviction_clears_active_run(self, tmp_path):
        # A run without run_completed is treated identically — there is no
        # crashed/completed distinction, just a single idle threshold.
        state, c = await self._seed(tmp_path, completed=False)
        try:
            assert state.active_run_id == "r1"
            now = state.runs["r1"].last_event_at
            assert (
                state.janitor_pass(now=now + state.EVICT_IDLE_S - 1)["evicted"]
                == []
            )
            result = state.janitor_pass(now=now + state.EVICT_IDLE_S + 1)
            assert result["evicted"] == ["r1"]
            assert state.active_run_id is None
        finally:
            c.close()

    @pytest.mark.asyncio
    async def test_budget_evicts_idlest_first(self, tmp_path):
        state, c = _mk_state(tmp_path)
        try:
            for rid in ("r1", "r2", "r3"):
                _, events = _events_small_run()
                await state.ingest_events(events, run_id=rid)
            # All three are recently active (small idle); eviction order is by
            # recency, idlest first. No run alone exceeds budget, so none is
            # demoted.
            state.runs["r1"].last_event_at = 100.0
            state.runs["r2"].last_event_at = 200.0
            state.runs["r3"].last_event_at = 300.0
            state.ram_budget_points = 10
            state.runs["r1"].resident_points = 6
            state.runs["r2"].resident_points = 6
            state.runs["r3"].resident_points = 6
            result = state.janitor_pass(now=400.0)
            # Idlest (r1) evicted brings total 18 -> 12; still over, so r2
            # goes too; r3 is the most recent and stays resident.
            assert result["evicted"] == ["r1", "r2"]
            assert "r3" in state.runs
        finally:
            c.close()

    @pytest.mark.asyncio
    async def test_lone_oversized_active_run_demoted(self, tmp_path):
        state, c = await self._seed(tmp_path, completed=False)
        try:
            state.ram_budget_points = 3
            result = state.janitor_pass(now=state.runs["r1"].last_event_at + 1)
            assert result["demoted"] == ["r1"]
            run = state.runs["r1"]
            assert run.ram_complete is False
            assert run.resident_points == 0
            assert run.loggables["a"].metrics["loss"]["entries"] == []
            # Type lock survives demotion; ingest keeps working.
            await state.ingest_events([
                {"type": "metric", "loggable_id": "a", "name": "loss",
                 "metric_type": "line", "value": 0.2, "step": 5,
                 "tags": [], "timestamp": 7.0},
            ], run_id="r1")
            assert c.flush()
            steps = [e["step"] for e in c.get_metrics("r1")["a"]["loss"]["entries"]]
            assert steps == [0, 1, 5]
            # Reads route to SQL for demoted runs.
            m = state.run_metrics("r1")
            assert [e["step"] for e in m["a"]["loss"]["entries"]] == [0, 1, 5]
        finally:
            c.close()

    @pytest.mark.asyncio
    async def test_no_cache_janitor_noop(self, tmp_path):
        from nebo.server.daemon import DaemonState

        state = DaemonState()
        _, events = _events_small_run()
        await state.ingest_events(events, run_id="r1")
        result = state.janitor_pass(now=time.time() + 10**6)
        assert result == {"evicted": [], "demoted": []}
        assert "r1" in state.runs


class TestAppCacheWiring:
    def test_env_builds_cache(self, tmp_path, monkeypatch):
        from nebo.server.daemon import DaemonState, create_daemon_app

        monkeypatch.setenv("NEBO_CACHE_PATH", str(tmp_path / "c.db"))
        monkeypatch.setenv("NEBO_RAM_BUDGET_MB", "1")
        state = DaemonState()
        create_daemon_app(state)
        try:
            assert state.cache is not None
            assert state.media_lru is state.cache.media_lru
            assert state.ram_budget_points == (1024 * 1024) // 372
        finally:
            state.cache.close()

    def test_no_env_no_cache(self, tmp_path, monkeypatch):
        from nebo.server.daemon import DaemonState, create_daemon_app

        monkeypatch.delenv("NEBO_CACHE_PATH", raising=False)
        state = DaemonState()
        create_daemon_app(state)
        assert state.cache is None

    def test_no_cache_env_wins(self, tmp_path, monkeypatch):
        from nebo.server.daemon import DaemonState, create_daemon_app

        monkeypatch.setenv("NEBO_CACHE_PATH", str(tmp_path / "c.db"))
        monkeypatch.setenv("NEBO_NO_CACHE", "1")
        state = DaemonState()
        create_daemon_app(state)
        assert state.cache is None


class TestCachePathAndSweep:
    def test_resolve_cache_path_stable(self, tmp_path):
        a = resolve_cache_path(tmp_path / "x")
        b = resolve_cache_path(tmp_path / "x")
        assert a == b
        assert a.suffix == ".db"

    def test_resolve_cache_path_distinct_per_logdir(self, tmp_path):
        a = resolve_cache_path(tmp_path / "x")
        b = resolve_cache_path(tmp_path / "y")
        assert a != b

    def test_sweep_cache_dir(self, tmp_path):
        old = tmp_path / "old.db"
        new = tmp_path / "new.db"
        old.write_bytes(b"")
        new.write_bytes(b"")
        stale = time.time() - 40 * 86400
        os.utime(old, (stale, stale))
        deleted = sweep_cache_dir(tmp_path, 30)
        assert old in deleted
        assert not old.exists()
        assert new.exists()

    def test_sweep_missing_dir_is_noop(self, tmp_path):
        assert sweep_cache_dir(tmp_path / "nope", 30) == []

    def test_sweep_removes_lock_side_file(self, tmp_path):
        old = tmp_path / "old.db"
        old.write_bytes(b"")
        (tmp_path / "old.db.lock").write_text("{}")
        stale = time.time() - 40 * 86400
        os.utime(old, (stale, stale))
        sweep_cache_dir(tmp_path, 30)
        assert not old.exists()
        assert not (tmp_path / "old.db.lock").exists()


class TestSingleOwnerLock:
    """Two live processes must never share one cache db — the second opener
    fails fast instead of racing duplicate rows into it."""

    def test_second_opener_fails_fast(self, tmp_path):
        from nebo.server.cache import CacheLockedError

        c = _mk(tmp_path)
        try:
            c2 = RunCache(tmp_path / "cache.db", logdir=tmp_path / "logs")
            with pytest.raises(CacheLockedError) as ei:
                c2.start()
            # The error names the holder recorded in the lockfile.
            assert str(os.getpid()) in str(ei.value)
        finally:
            c.close()

    def test_lock_released_on_close(self, tmp_path):
        c = _mk(tmp_path)
        c.close()
        c2 = _mk(tmp_path)  # must not raise
        c2.close()

    def test_lock_holder_probe(self, tmp_path):
        from nebo.server.cache import cache_lock_holder

        db = tmp_path / "cache.db"
        assert cache_lock_holder(db) is None  # no lockfile yet
        c = _mk(tmp_path)
        try:
            holder = cache_lock_holder(db)
            assert holder is not None
            assert holder.get("pid") == os.getpid()
        finally:
            c.close()
        assert cache_lock_holder(db) is None  # released with close()


class TestReingestIdempotence:
    """Re-applying an identical history op is a no-op (unique index + INSERT
    OR IGNORE) — the layer that keeps a re-scanned file or replayed batch
    from doubling cached rows."""

    @staticmethod
    def _count(c, table):
        return c._read_conn().execute(
            f"SELECT COUNT(*) FROM {table}"
        ).fetchone()[0]

    def test_media_row_deduped_including_null_step(self, tmp_path):
        c = _mk(tmp_path)
        try:
            # step=None exercises the COALESCE sentinel: NULLs are distinct
            # in SQLite unique indexes, so a bare column key wouldn't dedup.
            op = ("media_occurrence", "r1", "a", "f" * 16, "image", "frame",
                  None, 5.0, None, None, None, None, None)
            c.enqueue(op)
            c.enqueue(op)
            assert c.flush()
            assert self._count(c, "media") == 1
        finally:
            c.close()

    def test_metric_row_deduped(self, tmp_path):
        c = _mk(tmp_path)
        try:
            op = ("metric_row", "r1", "a", "loss", "line",
                  0, 1.0, "0.5", "[]", None)
            c.enqueue(op)
            c.enqueue(op)
            # A genuinely new point (different ts) still lands.
            c.enqueue(("metric_row", "r1", "a", "loss", "line",
                       1, 2.0, "0.4", "[]", None))
            assert c.flush()
            assert self._count(c, "metrics") == 2
        finally:
            c.close()

    def test_text_row_deduped(self, tmp_path):
        c = _mk(tmp_path)
        try:
            op = ("text_row", "r1", "a", "text", 1.0, None, "hi")
            c.enqueue(op)
            c.enqueue(op)
            # Same instant, different message: distinct row.
            c.enqueue(("text_row", "r1", "a", "text", 1.0, None, "yo"))
            assert c.flush()
            assert self._count(c, "texts") == 2
        finally:
            c.close()

    def test_alert_and_sig_rows_deduped(self, tmp_path):
        c = _mk(tmp_path)
        try:
            c.enqueue(("alert_row", "r1", 1.0, '{"title": "t"}'))
            c.enqueue(("alert_row", "r1", 1.0, '{"title": "t"}'))
            c.enqueue(("sig_event", "r1", 2.0, "run_completed", "{}"))
            c.enqueue(("sig_event", "r1", 2.0, "run_completed", "{}"))
            assert c.flush()
            assert self._count(c, "alerts") == 1
            assert self._count(c, "significant_events") == 1
        finally:
            c.close()

    @pytest.mark.asyncio
    async def test_full_reingest_adds_no_rows(self, tmp_path):
        """The end-to-end shape of the two-daemons-one-cache incident: the
        same run's events applied twice must leave SQL row counts unchanged."""
        state, c = _mk_state(tmp_path)
        try:
            _, events = _events_small_run()
            await state.ingest_events(events, run_id="r1")
            assert c.flush()
            tables = ("texts", "metrics", "media")
            counts = {t: self._count(c, t) for t in tables}
            assert counts["media"] == 1
            _, again = _events_small_run()
            await state.ingest_events(again, run_id="r1")
            assert c.flush()
            assert {t: self._count(c, t) for t in tables} == counts
        finally:
            c.close()
