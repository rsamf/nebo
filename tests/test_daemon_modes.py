"""Daemon persistence modes + local-only rejection + SDK fail-fast.

Modes:
  * local             — rejects network runs (watcher-only daemon)
  * remote            — accepts network runs, persists them as .nebo files
  * remote-ephemeral  — accepts network runs, persists nothing (RAM + cache)
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from nebo.core.fileformat import NeboFileReader
from nebo.server.daemon import DaemonState, create_daemon_app

RUN_START = {"type": "run_start", "data": {"script_path": "t.py"}}
A_TEXT = {"type": "text", "loggable_id": "__global__", "message": "hi"}


def _app(mode, remote_dir=None):
    state = DaemonState()
    state.mode = mode
    if remote_dir is not None:
        state._remote_dir = remote_dir
    return state, TestClient(create_daemon_app(state))


class TestHealthMode:
    def test_each_mode_reported(self):
        for mode in ("local", "remote", "remote-ephemeral"):
            _, client = _app(mode)
            assert client.get("/health").json()["mode"] == mode


class TestLocalRejection:
    def test_run_start_rejected_naming_both_flags(self):
        _, client = _app("local")
        resp = client.post("/events?run_id=r1", json=[RUN_START])
        assert resp.status_code == 409
        body = resp.json()
        assert body["error"] == "daemon_local_only"
        # The error copy carries the fix — both flags named.
        assert "--remote " in body["detail"] or "--remote [" in body["detail"]
        assert "--remote-ephemeral" in body["detail"]

    def test_unknown_run_event_rejected(self):
        _, client = _app("local")
        resp = client.post("/events?run_id=ghost", json=[A_TEXT])
        assert resp.status_code == 409

    def test_known_run_annotation_accepted(self):
        # A watcher-discovered run already exists → annotating it is fine even
        # on a local-only daemon (only run *creation* is refused).
        state, client = _app("local")
        state.create_run("t.py", run_id="known")
        resp = client.post("/events?run_id=known", json=[A_TEXT])
        assert resp.status_code == 200
        texts = client.get("/runs/known/text").json()["texts"]
        assert texts[-1]["message"] == "hi"


class TestRemoteEphemeral:
    def test_accepts_but_persists_nothing(self, tmp_path):
        _, client = _app("remote-ephemeral")
        resp = client.post("/events?run_id=r1", json=[RUN_START, A_TEXT])
        assert resp.status_code == 200
        assert not list(tmp_path.glob("*.nebo"))
        texts = client.get("/runs/r1/text").json()["texts"]
        assert texts[-1]["message"] == "hi"


class TestRemote:
    def test_writes_readable_file(self, tmp_path):
        _, client = _app("remote", remote_dir=tmp_path)
        client.post("/events?run_id=r1", json=[RUN_START, A_TEXT])
        client.post(
            "/events?run_id=r1", json=[{"type": "run_completed", "data": {}}]
        )
        files = list(tmp_path.glob("*.nebo"))
        assert len(files) == 1
        with files[0].open("rb") as f:
            reader = NeboFileReader(f)
            assert reader.read_header()["run_id"] == "r1"
            msgs = [
                e["payload"].get("message")
                for e in reader.read_entries()
                if e["type"] == "text"
            ]
        assert msgs == ["hi"]


class TestModeResolution:
    def test_both_env_flags_error(self, monkeypatch):
        monkeypatch.setenv("NEBO_REMOTE", "/x")
        monkeypatch.setenv("NEBO_REMOTE_EPHEMERAL", "1")
        with pytest.raises(RuntimeError):
            create_daemon_app(None)

    def test_default_server_is_local(self, monkeypatch):
        for k in (
            "NEBO_REMOTE", "NEBO_REMOTE_EPHEMERAL", "NEBO_LOGDIR", "NEBO_NO_LOCAL",
        ):
            monkeypatch.delenv(k, raising=False)
        app = create_daemon_app(None)
        assert app.state.daemon.mode == "local"

    def test_remote_env_bare_uses_default_dir(self, monkeypatch, tmp_path):
        monkeypatch.delenv("NEBO_REMOTE_EPHEMERAL", raising=False)
        monkeypatch.delenv("NEBO_NO_LOCAL", raising=False)
        monkeypatch.setenv("NEBO_LOGDIR", str(tmp_path))
        monkeypatch.setenv("NEBO_REMOTE", "1")  # bare flag → default dir
        state = create_daemon_app(None).state.daemon
        assert state.mode == "remote"
        assert state._remote_dir == tmp_path / "remote"

    def test_remote_env_equal_logdir_rejected(self, monkeypatch, tmp_path):
        # cli.py validates the flag form; env-only launches (Dockerfile's
        # uvicorn --factory, embedders) must be caught here too — the remote
        # writer feeding the watched logdir re-ingests its own output.
        monkeypatch.delenv("NEBO_REMOTE_EPHEMERAL", raising=False)
        monkeypatch.delenv("NEBO_NO_LOCAL", raising=False)
        monkeypatch.delenv("NEBO_CACHE_PATH", raising=False)
        monkeypatch.setenv("NEBO_LOGDIR", str(tmp_path))
        monkeypatch.setenv("NEBO_REMOTE", str(tmp_path))
        with pytest.raises(RuntimeError, match="remote dir"):
            create_daemon_app(None)

    def test_remote_env_equal_logdir_ok_without_watcher(self, monkeypatch, tmp_path):
        # With the watcher off there is no feedback path — mirrors the CLI,
        # which only rejects the combination when --no-local is absent.
        monkeypatch.delenv("NEBO_REMOTE_EPHEMERAL", raising=False)
        monkeypatch.delenv("NEBO_CACHE_PATH", raising=False)
        monkeypatch.setenv("NEBO_NO_LOCAL", "1")
        monkeypatch.setenv("NEBO_LOGDIR", str(tmp_path))
        monkeypatch.setenv("NEBO_REMOTE", str(tmp_path))
        state = create_daemon_app(None).state.daemon
        assert state.mode == "remote"


class TestWriterSourceGating:
    """Watcher/loaded events never pass through the remote-mode writer —
    they came FROM a file, so re-writing them would duplicate entries (and,
    with a remote dir aliasing the watched logdir, feed the watcher its own
    output in a loop)."""

    @pytest.mark.asyncio
    async def test_watcher_events_not_rewritten_to_file(self, tmp_path):
        state = DaemonState()
        state.mode = "remote"
        state._remote_dir = tmp_path
        await state.ingest_events(
            [RUN_START, A_TEXT], run_id="r1", source="network",
        )
        await state.ingest_events(
            [{"type": "text", "loggable_id": "__global__",
              "message": "from-file"}],
            run_id="r1", source="watcher",
        )
        await state.ingest_events(
            [{"type": "run_completed", "data": {}}],
            run_id="r1", source="network",
        )
        # The alias guard drops the watcher batch before RAM, and the writer
        # gate keeps the file to network entries only.
        assert [l.message for l in state.runs["r1"].texts] == ["hi"]
        files = list(tmp_path.glob("*.nebo"))
        assert len(files) == 1
        with files[0].open("rb") as f:
            reader = NeboFileReader(f)
            reader.read_header()
            msgs = [
                e["payload"].get("message")
                for e in reader.read_entries()
                if e["type"] == "text"
            ]
        assert msgs == ["hi"]

    @pytest.mark.asyncio
    async def test_watcher_batch_for_network_run_dropped(self, tmp_path):
        # A .nebo file in the watched logdir that claims a network-owned
        # run_id is an alias (stray copy) — its events must not append a
        # second copy of everything into RAM.
        state = DaemonState()
        state.mode = "remote"
        state._remote_dir = tmp_path
        await state.ingest_events(
            [RUN_START, A_TEXT], run_id="r1", source="network",
        )
        await state.ingest_events([A_TEXT], run_id="r1", source="watcher")
        assert [l.message for l in state.runs["r1"].texts] == ["hi"]
        # Watcher-owned runs keep accepting watcher events as before.
        await state.ingest_events(
            [RUN_START, A_TEXT], run_id="r2", source="watcher",
        )
        await state.ingest_events([A_TEXT], run_id="r2", source="watcher")
        assert [l.message for l in state.runs["r2"].texts] == ["hi", "hi"]

    @pytest.mark.asyncio
    async def test_watcher_run_start_does_not_reopen_writer(self, tmp_path):
        state = DaemonState()
        state.mode = "remote"
        state._remote_dir = tmp_path
        await state.ingest_events([RUN_START], run_id="r1", source="network")
        await state.ingest_events(
            [{"type": "run_completed", "data": {}}],
            run_id="r1", source="network",
        )
        assert getattr(state.runs["r1"], "_file_writer", None) is None
        # The watcher later re-discovers the daemon's own (closed) file and
        # replays its run_start — that must not open a fresh writer.
        await state.ingest_events([RUN_START], run_id="r1", source="watcher")
        assert getattr(state.runs["r1"], "_file_writer", None) is None


def _fake_urlopen(mode):
    """A urllib.request.urlopen stand-in returning /health with `mode`."""

    class _Resp:
        status = 200

        def read(self):
            return json.dumps({"status": "ok", "mode": mode}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _open(req, timeout=None):
        return _Resp()

    return _open


class TestSdkFailFast:
    def test_connect_local_mode_is_fatal(self, monkeypatch):
        from nebo.core.client import NetworkTransport

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen("local"))
        t = NetworkTransport(base_url="http://localhost:7861")
        assert t.connect() is False
        assert t._fatal is True
        assert t._policy_error and "local-only" in t._policy_error

    def test_connect_ephemeral_mode_ok(self, monkeypatch):
        from nebo.core.client import NetworkTransport

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen("remote-ephemeral"))
        # Don't spin up the background flush thread for a unit check.
        monkeypatch.setattr(NetworkTransport, "_start_flush_thread", lambda self: None)
        t = NetworkTransport(base_url="http://localhost:7861")
        assert t.connect() is True
        assert t._fatal is False
        assert t._policy_error is None

    def test_transient_failure_is_retryable(self):
        from nebo.core.client import NetworkTransport

        # Nothing is listening → connect fails, but not fatally: the flush
        # loop must keep retrying (this is the not-a-policy-error path).
        t = NetworkTransport(base_url="http://localhost:19998")
        assert t.connect() is False
        assert t._fatal is False
        assert t._policy_error is None

    def test_init_raises_daemon_local_only(self, monkeypatch):
        import nebo as nb
        from nebo.core.state import SessionState

        monkeypatch.delenv("NEBO_URI", raising=False)
        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen("local"))
        SessionState.reset_singleton()
        nb._auto_init_done = False
        try:
            with pytest.raises(nb.DaemonLocalOnlyError):
                nb.init(uri="localhost:7861")
        finally:
            SessionState.reset_singleton()
            nb._auto_init_done = False
