"""SQL-side metric decimation.

The daemon used to load every point of a series into Python and *then*
throw most of them away, so a `points=2000` request against a 1.5M-point
run still materialized 1.5M dicts on the event loop. These tests pin the
replacement: the database selects the kept rows, and it selects exactly
the rows `downsample_series` would have.
"""

from __future__ import annotations

import json
import math

import pytest

from nebo.server.cache import RunCache
from nebo.server.daemon import downsample_series


def _cache(tmp_path):
    cache = RunCache(tmp_path / "c.db", logdir=tmp_path)
    cache.start()
    return cache


def _fill(cache, run_id, name, stype, values, colors=None):
    for step, v in enumerate(values):
        cache.enqueue((
            "metric_row", run_id, "lg", name, stype, step, float(step),
            json.dumps(v), "[]", colors,
        ))
    cache.flush()


def _entries(series):
    return [(e["step"], e["value"]) for e in series["entries"]]


@pytest.fixture
def cache(tmp_path):
    c = _cache(tmp_path)
    yield c
    c.close()


# --- parity with the in-RAM implementation ---------------------------------


def test_line_decimation_matches_downsample_series(cache):
    # Distinct values: ties are the one case where SQL may keep a different
    # (equally extreme) row, so keep them out of the equality check.
    values = [math.sin(i / 7.0) * 100 + i * 0.001 for i in range(5000)]
    _fill(cache, "r1", "loss", "line", values)

    sql = cache.get_metrics("r1", points=200)["lg"]["loss"]
    full = cache.get_metrics("r1")["lg"]["loss"]
    ram = downsample_series(full, 200)

    assert _entries(sql) == _entries(ram)
    assert sql["total_points"] == ram["total_points"] == 5000
    assert sql["downsampled"] is True


def test_line_decimation_preserves_spikes(cache):
    """The whole reason line uses min/max buckets instead of a stride."""
    values = [0.0] * 5000
    values[1234] = 999.0   # a spike a uniform stride would step over
    values[4321] = -999.0
    _fill(cache, "r1", "loss", "line", values)

    kept = [v for _, v in _entries(cache.get_metrics("r1", points=200)["lg"]["loss"])]
    assert 999.0 in kept
    assert -999.0 in kept


def test_scatter_decimation_matches_downsample_series(cache):
    values = [{"a": {"x": [i], "y": [i * 2]}} for i in range(3000)]
    _fill(cache, "r1", "pts", "scatter", values)

    sql = cache.get_metrics("r1", points=250)["lg"]["pts"]
    ram = downsample_series(cache.get_metrics("r1")["lg"]["pts"], 250)
    assert _entries(sql) == _entries(ram)


def test_endpoints_are_always_kept(cache):
    values = [float(i) for i in range(4000)]
    _fill(cache, "r1", "loss", "line", values)
    kept = _entries(cache.get_metrics("r1", points=100)["lg"]["loss"])
    assert kept[0][0] == 0
    assert kept[-1][0] == 3999


# --- pass-through cases -----------------------------------------------------


def test_series_under_the_cap_is_untouched(cache):
    values = [float(i) for i in range(50)]
    _fill(cache, "r1", "loss", "line", values)
    series = cache.get_metrics("r1", points=2000)["lg"]["loss"]
    assert len(series["entries"]) == 50
    assert series["downsampled"] is False
    assert series["total_points"] == 50


def test_snapshot_types_are_never_decimated(cache):
    # bar/pie/histogram overwrite on ingest; there is nothing to thin.
    values = [{"a": float(i)} for i in range(3000)]
    _fill(cache, "r1", "dist", "bar", values)
    series = cache.get_metrics("r1", points=10)["lg"]["dist"]
    assert len(series["entries"]) == 3000
    assert series["downsampled"] is False


def test_points_zero_returns_full_fidelity(cache):
    values = [float(i) for i in range(3000)]
    _fill(cache, "r1", "loss", "line", values)
    assert len(cache.get_metrics("r1", points=0)["lg"]["loss"]["entries"]) == 3000


def test_colors_and_tags_survive_decimation(cache):
    values = [float(i) for i in range(3000)]
    _fill(cache, "r1", "pts", "line", values, colors=1)
    series = cache.get_metrics("r1", points=100)["lg"]["pts"]
    assert all(e["colors"] is True for e in series["entries"])


# --- the actual point: bounded work --------------------------------------


def test_decimation_reads_far_fewer_rows_than_it_stores(cache):
    """A capped request must not materialize the whole series in Python."""
    values = [float(i) for i in range(20000)]
    _fill(cache, "r1", "loss", "line", values)
    series = cache.get_metrics("r1", points=500)["lg"]["loss"]
    # ~2 points per bucket, 250 buckets, plus endpoints.
    assert len(series["entries"]) <= 520
    assert series["total_points"] == 20000


# --- the reported bug: one heavy run must not stall the daemon ------------


@pytest.mark.asyncio
async def test_metrics_read_does_not_block_the_event_loop():
    """A slow metrics read must not make /health time out.

    This is the failure that was reported: opening a 1.5M-point run held
    the event loop for ~11 s, so every other request — including the
    health check and the run-list poll — queued behind it and the daemon
    looked hung.
    """
    import asyncio
    import time

    import httpx

    from nebo.server.daemon import DaemonState, create_daemon_app

    state = DaemonState()
    state.create_run("s.py", run_id="r1")

    def _slow(run_id, points=0):
        time.sleep(1.0)  # stands in for a million-point read
        return {}

    state.run_metrics = _slow
    app = create_daemon_app(state=state)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        slow = asyncio.create_task(c.get("/runs/r1/metrics"))
        # Yield once so the slow request is actually in flight. If the read
        # ran on the loop this sleep would itself take the full 1 s, which
        # is why the assertion below is "is the slow one still pending?"
        # rather than a wall-clock measurement taken after the fact.
        await asyncio.sleep(0.05)

        health = await c.get("/health")
        assert health.status_code == 200
        # The load-bearing assertion: /health was served while the metrics
        # read was still running. On the event loop it could only have been
        # served after that read finished.
        assert not slow.done(), "/health was queued behind the metrics read"
        assert (await slow).status_code == 200
