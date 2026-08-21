"""Daemon ingest, cache persistence and read paths for the action modality."""

from __future__ import annotations

import asyncio
import json

import pytest

from nebo.server.cache import RunCache
from nebo.server.daemon import (
    DaemonState, create_daemon_app, decimate_frames,
)

GLB = b"glTF\x02\x00\x00\x00body-model-bytes"


def _model_event(loggable_id="__global__", model_id="abc123"):
    return {
        "type": "body_model", "loggable_id": loggable_id, "name": "arm",
        "model_id": model_id, "body_names": ["world", "link1"],
        "source_format": "mjcf", "data": GLB, "timestamp": 1.0,
    }


def _frame_event(step, name="scene", loggable_id="__global__",
                 instances=("policy",)):
    return {
        "type": "body_transform", "loggable_id": loggable_id, "name": name,
        "step": step, "timestamp": 1.0 + step,
        "instances": {
            label: {"model": "abc123", "pos_quat_xyzw": [0.0] * 14}
            for label in instances
        },
    }


def _ingest(state, run_id, events):
    asyncio.new_event_loop().run_until_complete(
        state.ingest_events(events, run_id)
    )


# --- ingest ----------------------------------------------------------------


class TestActionIngest:
    def setup_method(self):
        self.state = DaemonState()
        self.run = self.state.create_run("sim.py", run_id="r1")

    def test_body_model_registers_a_run_level_manifest(self):
        _ingest(self.state, "r1", [_model_event()])
        manifest = self.run.body_models["abc123"]
        assert manifest["body_names"] == ["world", "link1"]
        assert manifest["source_format"] == "mjcf"
        assert manifest["name"] == "arm"

    def test_body_model_bytes_are_content_addressed_media(self):
        import hashlib

        _ingest(self.state, "r1", [_model_event()])
        media_id = self.run.body_models["abc123"]["media_id"]
        assert media_id == hashlib.sha256(GLB).hexdigest()[:16]
        assert self.state.media_bytes("r1", media_id) == GLB

    def test_body_model_payload_is_stripped_after_ingest(self):
        # `data` is popped so WS broadcasts stay light, exactly like images.
        event = _model_event()
        _ingest(self.state, "r1", [event])
        assert "data" not in event

    def test_body_transform_appends_a_frame(self):
        _ingest(self.state, "r1", [_model_event(), _frame_event(0)])
        (frame,) = self.run.loggables["__global__"].actions
        assert frame["name"] == "scene"
        assert frame["step"] == 0
        assert frame["instances"]["policy"]["model"] == "abc123"

    def test_frames_charge_the_ram_budget_by_float_count(self):
        # A scene frame is far heavier than one metric point; charging it
        # as a single point would let a rollout blow past --ram-budget.
        before = self.run.resident_points
        _ingest(self.state, "r1", [
            _frame_event(0, instances=("policy", "reference")),
        ])
        assert self.run.resident_points - before > 1

    def test_frames_advance_latest_step(self):
        _ingest(self.state, "r1", [_frame_event(0), _frame_event(7)])
        assert self.run.latest_step == 7

    def test_run_summary_counts_actions(self):
        _ingest(self.state, "r1", [_frame_event(0), _frame_event(1)])
        assert self.run.get_summary()["action_count"] == 2



def test_gltf_bytes_sniff_as_gltf_binary():
    from nebo.server.daemon import _sniff_mime

    assert _sniff_mime(GLB) == "model/gltf-binary"


# --- decimation ------------------------------------------------------------


def test_decimate_keeps_the_final_frame():
    frames = [{"name": "scene", "step": i} for i in range(5000)]
    out = decimate_frames(frames, 100)
    assert len(out) == 100
    assert out[-1]["step"] == 4999


def test_decimate_is_per_scene():
    frames = (
        [{"name": "a", "step": i} for i in range(300)]
        + [{"name": "b", "step": 0}]
    )
    out = decimate_frames(frames, 100)
    assert sum(1 for f in out if f["name"] == "a") == 100
    assert sum(1 for f in out if f["name"] == "b") == 1


def test_decimate_zero_means_full_fidelity():
    frames = [{"name": "a", "step": i} for i in range(5000)]
    assert decimate_frames(frames, 0) is frames


# --- endpoint --------------------------------------------------------------


def _client(events, run_id="r1"):
    from fastapi.testclient import TestClient

    state = DaemonState()
    state.create_run("sim.py", run_id=run_id)
    _ingest(state, run_id, events)
    return TestClient(create_daemon_app(state=state))


class TestActionsEndpoint:
    def test_returns_frames_and_models(self):
        client = _client([_model_event(), _frame_event(0)])
        body = client.get("/runs/r1/actions").json()
        assert body["body_models"]["abc123"]["source_format"] == "mjcf"
        assert body["actions"]["__global__"][0]["name"] == "scene"

    def test_unknown_run_is_404(self):
        client = _client([_frame_event(0)])
        assert client.get("/runs/nope/actions").status_code == 404

    def test_decimates_by_default_and_limit_zero_is_full(self):
        client = _client([_frame_event(i) for i in range(5000)])
        assert len(client.get("/runs/r1/actions").json()
                   ["actions"]["__global__"]) == 2000
        assert len(client.get("/runs/r1/actions?limit=0").json()
                   ["actions"]["__global__"]) == 5000

    def test_filters_by_scene_name(self):
        client = _client([_frame_event(0, name="a"), _frame_event(0, name="b")])
        body = client.get("/runs/r1/actions?name=a").json()
        names = {f["name"] for f in body["actions"]["__global__"]}
        assert names == {"a"}

    def test_glb_is_served_raw_from_the_media_endpoint(self):
        client = _client([_model_event()])
        model = client.get("/runs/r1/actions").json()["body_models"]["abc123"]
        resp = client.get(f"/runs/r1/media/{model['media_id']}")
        assert resp.status_code == 200
        assert resp.content == GLB
        assert resp.headers["content-type"].startswith("model/gltf-binary")

    def test_resolve_reports_an_action_stream_as_existing(self):
        client = _client([_model_event(), _frame_event(0, name="rollout")])
        out = client.get(
            "/resolve?ref=nebo://run/r1/__global__/rollout"
        ).json()
        assert out["exists"] is True
        assert out["resource"] == "stream"


# --- cache -----------------------------------------------------------------


class TestActionCache:
    def _cache(self, tmp_path):
        cache = RunCache(tmp_path / "c.db", logdir=tmp_path)
        cache.start()
        return cache

    def test_actions_and_models_persist(self, tmp_path):
        cache = self._cache(tmp_path)
        try:
            cache.enqueue((
                "body_model_upsert", "r1", "abc123", "arm", "m1",
                json.dumps(["world", "link1"]), "mjcf",
            ))
            cache.enqueue((
                "action_frame", "r1", "__global__", "scene", 0, 1.0,
                json.dumps({"policy": {"model": "abc123",
                                       "pos_quat_xyzw": [0.0] * 14}}),
            ))
            cache.flush()
            models = cache.list_body_models("r1")
            assert models["abc123"]["body_names"] == ["world", "link1"]
            frames = cache.list_actions("r1")["__global__"]
            assert frames[0]["instances"]["policy"]["model"] == "abc123"
        finally:
            cache.close()

    def test_reingesting_a_frame_is_idempotent(self, tmp_path):
        cache = self._cache(tmp_path)
        try:
            op = (
                "action_frame", "r1", "__global__", "scene", 0, 1.0, "{}",
            )
            cache.enqueue(op)
            cache.enqueue(op)
            cache.flush()
            assert len(cache.list_actions("r1")["__global__"]) == 1
        finally:
            cache.close()

    def test_republishing_a_model_updates_in_place(self, tmp_path):
        cache = self._cache(tmp_path)
        try:
            cache.enqueue((
                "body_model_upsert", "r1", "abc123", "arm", "m1", "[]", "mjcf",
            ))
            cache.enqueue((
                "body_model_upsert", "r1", "abc123", "renamed", "m1", "[]",
                "mjcf",
            ))
            cache.flush()
            models = cache.list_body_models("r1")
            assert len(models) == 1
            assert models["abc123"]["name"] == "renamed"
        finally:
            cache.close()

    def test_demotion_drops_read_state_but_keeps_serving(self, tmp_path):
        """A demoted run drops its RAM frames and reads them back from SQL."""
        state = DaemonState(cache=self._cache(tmp_path))
        try:
            state.create_run("sim.py", run_id="r1")
            _ingest(state, "r1", [_model_event(), _frame_event(0)])
            state._demote_run("r1")
            assert state.runs["r1"].loggables["__global__"].actions == []
            assert state.runs["r1"].ram_complete is False
            # Ingest state survives, so reads fall through to the cache.
            assert len(state.run_actions("r1")["actions"]["__global__"]) == 1
        finally:
            state.cache.close()

    def test_frames_read_back_after_eviction(self, tmp_path):
        """A run served from SQL still lists its scenes and models."""
        state = DaemonState(cache=self._cache(tmp_path))
        try:
            state.create_run("sim.py", run_id="r1")
            _ingest(state, "r1", [_model_event(), _frame_event(0)])
            state.cache.flush()
            state._evict_run("r1")
            payload = state.run_actions("r1")
            assert payload["body_models"]["abc123"]["name"] == "arm"
            assert len(payload["actions"]["__global__"]) == 1
        finally:
            state.cache.close()


def test_summary_parity_across_eviction(tmp_path):
    """A run's summary must not change when it moves from RAM to SQL."""
    cache = RunCache(tmp_path / "c.db", logdir=tmp_path)
    cache.start()
    state = DaemonState(cache=cache)
    try:
        state.create_run("sim.py", run_id="r1")
        _ingest(state, "r1", [_model_event()] + [
            _frame_event(i) for i in range(4)
        ])
        cache.flush()
        before = state.run_summary("r1")
        state._evict_run("r1")
        after = state.run_summary("r1")
        assert after["action_count"] == before["action_count"] == 4
        assert after["latest_step"] == before["latest_step"] == 3
    finally:
        cache.close()


def test_ingest_decodes_float32_pose_bytes():
    """The daemon normalizes poses to lists so RAM/cache/HTTP/UI agree."""
    from nebo.logging.bodies import normalize_instances

    state = DaemonState()
    state.create_run("sim.py", run_id="r1")
    poses = [[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]] * 2
    packed = normalize_instances(poses, n_bodies=2)["default"]
    _ingest(state, "r1", [{
        "type": "body_transform", "loggable_id": "__global__",
        "name": "scene", "step": 0, "timestamp": 1.0,
        "instances": {"a": {"model": "abc123", "pos_quat_xyzw": packed}},
    }])
    frame = state.run_actions("r1")["actions"]["__global__"][0]
    values = frame["instances"]["a"]["pos_quat_xyzw"]
    assert isinstance(values, list)
    assert len(values) == 14
    assert values[6] == 1.0


def test_ingest_still_accepts_plain_lists():
    """Pre-float32 .nebo files replay unchanged."""
    state = DaemonState()
    state.create_run("sim.py", run_id="r1")
    _ingest(state, "r1", [{
        "type": "body_transform", "loggable_id": "__global__",
        "name": "scene", "step": 0, "timestamp": 1.0,
        "instances": {"a": {"model": "abc123", "pos_quat_xyzw": [0.0] * 14}},
    }])
    frame = state.run_actions("r1")["actions"]["__global__"][0]
    assert len(frame["instances"]["a"]["pos_quat_xyzw"]) == 14
