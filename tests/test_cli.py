"""Tests for the CLI module."""

from __future__ import annotations

import argparse
import json

import pytest

from nebo.cli import _get_version, cmd_mcp, main


class TestVersion:
    """Tests for the -v/--version flag."""

    def test_get_version_returns_string(self) -> None:
        """The installed package version resolves to a non-empty string."""
        v = _get_version()
        assert isinstance(v, str) and v

    @pytest.mark.parametrize("flag", ["--version", "-v"])
    def test_version_flag_prints_and_exits(
        self, flag: str, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`nebo --version` / `-v` prints `nebo <version>` and exits 0."""
        monkeypatch.setattr("sys.argv", ["nebo", flag])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert out.strip() == f"nebo {_get_version()}"


class TestCLI:
    """Tests for CLI commands."""

    def _parse_mcp_config(self, captured: str) -> dict:
        lines = captured.strip().split("\n")
        assert lines[0].startswith("#")
        return json.loads("\n".join(lines[1:]))

    def test_mcp_outputs_valid_json(self, capsys: pytest.CaptureFixture) -> None:
        """nebo mcp should output valid JSON config."""
        args = argparse.Namespace(port=7861)
        cmd_mcp(args)
        config = self._parse_mcp_config(capsys.readouterr().out)
        assert "mcpServers" in config
        assert "nebo" in config["mcpServers"]
        assert config["mcpServers"]["nebo"]["command"] == "nebo"
        assert "mcp-stdio" in config["mcpServers"]["nebo"]["args"]

    def test_mcp_default_port_omits_port_flag(self, capsys: pytest.CaptureFixture) -> None:
        """At the default port, --port should NOT appear in args (keeps output minimal)."""
        args = argparse.Namespace(port=7861)
        cmd_mcp(args)
        config = self._parse_mcp_config(capsys.readouterr().out)
        nebo_args = config["mcpServers"]["nebo"]["args"]
        assert "--port" not in nebo_args

    def test_mcp_custom_port_forwarded_to_args(self, capsys: pytest.CaptureFixture) -> None:
        """`nebo mcp --port 9000` must embed --port 9000 in the printed MCP config.

        Without this, a daemon on a non-default port is unreachable from
        the MCP server the printed config instantiates.
        """
        args = argparse.Namespace(port=9000)
        cmd_mcp(args)
        config = self._parse_mcp_config(capsys.readouterr().out)
        nebo_args = config["mcpServers"]["nebo"]["args"]
        assert nebo_args == ["mcp-stdio", "--port", "9000"]


class TestLoadRemote:
    """`nebo load --url ...` reads the file locally and replays its
    events through /events on the remote daemon — covers the case
    where the daemon (e.g. a Hugging Face Space) can't see the user's
    filesystem so the legacy POST /load with a server-side path won't
    work."""

    def _proxy_urllib(self, monkeypatch, client) -> None:
        """Route the replay helper's urllib traffic into a TestClient so
        the replay path doesn't actually hit the network."""
        import urllib.request

        class FakeResp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return b""

        def fake_urlopen(req, timeout=None):
            # TestClient understands relative paths only; strip the
            # base URL we pass to the replay helper.
            assert req.full_url.startswith("http://daemon")
            path = req.full_url[len("http://daemon"):]
            resp = client.post(
                path, content=req.data,
                headers=dict(req.headers),
            )
            assert resp.status_code == 200, resp.text
            return FakeResp()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    def test_replay_pushes_events_to_remote(self, tmp_path, monkeypatch) -> None:
        from fastapi.testclient import TestClient
        from nebo.server.daemon import DaemonState, create_daemon_app
        from nebo.core.fileformat import NeboFileWriter
        from nebo.cli import _replay_nebo_file_to_remote

        # Write a small .nebo file with one node + two metric points.
        run_id = "replay_test_run"
        nebo_path = tmp_path / "sample.nebo"
        with nebo_path.open("wb") as f:
            writer = NeboFileWriter(f, run_id=run_id, script_path="x.py", args=[])
            writer.write_header()
            writer.write_entry("loggable_register", {
                "loggable_id": "train", "kind": "node", "func_name": "train",
            })
            writer.write_entry("metric", {
                "loggable_id": "train", "name": "loss",
                "metric_type": "line", "value": 0.9, "step": 0, "tags": [],
            })
            writer.write_entry("metric", {
                "loggable_id": "train", "name": "loss",
                "metric_type": "line", "value": 0.4, "step": 1, "tags": [],
            })
            writer.close()

        state = DaemonState()
        client = TestClient(create_daemon_app(state=state))
        self._proxy_urllib(monkeypatch, client)

        _replay_nebo_file_to_remote(str(nebo_path), "http://daemon", api_token=None)

        # Confirm the run + metric landed in the daemon's state.
        assert run_id in state.runs
        run = state.runs[run_id]
        assert "train" in run.loggables
        loss = run.loggables["train"].metrics["loss"]
        assert [e["value"] for e in loss["entries"]] == [0.9, 0.4]

    def test_replay_run_with_media(self, tmp_path, monkeypatch) -> None:
        """Format v4 media entries carry raw PNG/WAV bytes in `data`.
        The replay wire must move them intact — a JSON body can't
        (`Object of type bytes is not JSON serializable`), which is
        exactly how every media-bearing demo run broke CI uploads."""
        from fastapi.testclient import TestClient
        from nebo.server.daemon import DaemonState, create_daemon_app
        from nebo.core.fileformat import NeboFileWriter
        from nebo.server.cache import media_id_for
        from nebo.cli import _replay_nebo_file_to_remote

        png = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
        wav = b"RIFF$\x00\x00\x00WAVE" + bytes(64)

        run_id = "replay_media_run"
        nebo_path = tmp_path / "media.nebo"
        with nebo_path.open("wb") as f:
            writer = NeboFileWriter(f, run_id=run_id, script_path="x.py", args=[])
            writer.write_header()
            writer.write_entry("image", {
                "type": "image", "loggable_id": "__global__",
                "name": "sample", "data": png, "step": None,
                "timestamp": 123.0,
            })
            writer.write_entry("audio", {
                "type": "audio", "loggable_id": "__global__",
                "name": "clip", "data": wav, "sr": 16000, "step": None,
                "timestamp": 124.0,
            })
            writer.close()

        state = DaemonState()
        client = TestClient(create_daemon_app(state=state))
        self._proxy_urllib(monkeypatch, client)

        _replay_nebo_file_to_remote(str(nebo_path), "http://daemon", api_token=None)

        run = state.runs[run_id]
        images = run.loggables["__global__"].images
        assert [i["media_id"] for i in images] == [media_id_for(png)]
        audio = run.loggables["__global__"].audio
        assert [a["media_id"] for a in audio] == [media_id_for(wav)]
        # The bytes must survive the wire byte-for-byte.
        resp = client.get(f"/runs/{run_id}/media/{media_id_for(png)}")
        assert resp.status_code == 200 and resp.content == png
        resp = client.get(f"/runs/{run_id}/media/{media_id_for(wav)}")
        assert resp.status_code == 200 and resp.content == wav

    def test_replay_forwards_header_metadata(self, tmp_path, monkeypatch) -> None:
        """The file body has no run_start entry — run metadata lives in
        the header. The synthesized run_start must forward it (like the
        watcher's shallow registration) or the uploaded run lists with
        the upload time and loses its name/group/args."""
        from fastapi.testclient import TestClient
        from nebo.server.daemon import DaemonState, create_daemon_app
        from nebo.server.tree import TreeStore
        from nebo.core.fileformat import NeboFileWriter
        from nebo.cli import _replay_nebo_file_to_remote

        run_id = "replay_meta_run"
        nebo_path = tmp_path / "meta.nebo"
        with nebo_path.open("wb") as f:
            writer = NeboFileWriter(
                f, run_id=run_id, script_path="train.py",
                args=["--epochs", "2"], run_name="exp-1",
                group="demos/vision",
            )
            writer._started_at = 1_600_000_000.0
            writer.write_header()
            writer.close()

        state = DaemonState()
        state.tree = TreeStore(tmp_path / "meta_dir")
        client = TestClient(create_daemon_app(state=state))
        self._proxy_urllib(monkeypatch, client)

        _replay_nebo_file_to_remote(str(nebo_path), "http://daemon", api_token=None)

        run = state.runs[run_id]
        assert run.script_path == "train.py"
        assert run.run_name == "exp-1"
        assert run.args == ["--epochs", "2"]
        assert run.started_at.timestamp() == 1_600_000_000.0
        assert state.tree.to_payload({run_id})["runs"][run_id] == "demos/vision"


# ---------------------------------------------------------------------------
# nebo runs list|show|wait
# ---------------------------------------------------------------------------

import io
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch


def _run_cli(argv: list[str]) -> str:
    """Run nebo.cli.main with argv (excluding the program name) and return stdout."""
    buf = io.StringIO()
    from nebo.cli import main
    with redirect_stdout(buf), patch("sys.argv", ["nebo"] + argv):
        main()
    return buf.getvalue()


def test_runs_list_json(monkeypatch):
    monkeypatch.setattr(
        "nebo.client.get_run_history",
        lambda **c: {"runs": [{"id": "abc", "run_name": "exp1"}]},
    )
    out = _run_cli(["runs", "list", "--json"])
    assert json.loads(out)["runs"][0]["id"] == "abc"


def test_runs_show_json(monkeypatch):
    monkeypatch.setattr(
        "nebo.client.get_run_status",
        lambda rid, **c: {"id": rid, "node_count": 3},
    )
    out = _run_cli(["runs", "show", "abc", "--json"])
    assert json.loads(out) == {"id": "abc", "node_count": 3}


def test_runs_wait_passes_args(monkeypatch):
    received: dict = {}
    def fake_wait(run_id, **kwargs):
        received["run_id"] = run_id
        received.update(kwargs)
        return {"status": "alert", "alert": {"title": "x"}}
    monkeypatch.setattr("nebo.client.wait_for_alert", fake_wait)
    out = _run_cli(["runs", "wait", "abc", "--timeout", "10", "--min-level", "30", "--json"])
    assert json.loads(out)["status"] == "alert"
    assert received["run_id"] == "abc"
    assert received["timeout"] == 10.0
    assert received["min_level"] == 30


# ---------------------------------------------------------------------------
# nebo graph show | loggables show | describe
# ---------------------------------------------------------------------------


def test_graph_show_json(monkeypatch):
    monkeypatch.setattr(
        "nebo.client.get_graph",
        lambda **c: {"nodes": {"a": {}}, "edges": []},
    )
    out = _run_cli(["graph", "show", "--run", "abc", "--json"])
    parsed = json.loads(out)
    assert "nodes" in parsed
    assert "edges" in parsed


def test_loggables_show_json(monkeypatch):
    monkeypatch.setattr(
        "nebo.client.get_loggable_status",
        lambda lid, **c: {"loggable_id": lid, "kind": "node"},
    )
    out = _run_cli(["loggables", "show", "node_a", "--run", "abc", "--json"])
    assert json.loads(out)["loggable_id"] == "node_a"


def test_describe_json(monkeypatch):
    monkeypatch.setattr(
        "nebo.client.get_description",
        lambda **c: {"workflow_description": "hello"},
    )
    out = _run_cli(["describe", "--json"])
    assert json.loads(out)["workflow_description"] == "hello"


# ---------------------------------------------------------------------------
# nebo metrics list|get|log
# ---------------------------------------------------------------------------


def test_metrics_get_passes_filters(monkeypatch):
    received: dict = {}
    def fake(lid, **kw):
        received["loggable_id"] = lid
        received.update(kw)
        return {"loggable_id": lid, "metrics": {"loss": {"type": "line", "entries": []}}}
    monkeypatch.setattr("nebo.client.get_metrics", fake)
    _run_cli([
        "metrics", "get", "node_a",
        "--name", "loss",
        "--tag", "train",
        "--step", "5",
        "--run", "abc",
        "--json",
    ])
    assert received["loggable_id"] == "node_a"
    assert received["name"] == "loss"
    assert received["tag"] == "train"
    assert received["step"] == 5
    assert received["run_id"] == "abc"


def test_metrics_get_values_only_emits_entries(monkeypatch):
    entries = [
        {"step": 0, "value": 1.0, "tags": [], "timestamp": 1.0},
        {"step": 1, "value": 0.5, "tags": [], "timestamp": 2.0},
    ]
    monkeypatch.setattr(
        "nebo.client.get_metrics",
        lambda lid, **kw: {"metrics": {"loss": {"type": "line", "entries": entries}}},
    )
    out = _run_cli([
        "metrics", "get", "node_a", "--name", "loss", "--values-only", "--json",
    ])
    assert json.loads(out) == entries


def test_metrics_get_values_only_requires_name(monkeypatch):
    monkeypatch.setattr("nebo.client.get_metrics", lambda lid, **kw: {"metrics": {}})
    with pytest.raises(SystemExit):
        _run_cli(["metrics", "get", "node_a", "--values-only", "--json"])


def test_metrics_get_cross_run_fanout(monkeypatch):
    calls: list = []
    def fake(lid, **kw):
        calls.append(kw.get("run_id"))
        return {"metrics": {"loss": {"type": "line", "entries": [
            {"step": 0, "value": float(len(calls)), "tags": [], "timestamp": 0.0},
        ]}}}
    monkeypatch.setattr("nebo.client.get_metrics", fake)
    out = _run_cli([
        "metrics", "get", "node_a", "--name", "loss",
        "--runs", "r1,r2,r3", "--values-only", "--json",
    ])
    data = json.loads(out)
    assert calls == ["r1", "r2", "r3"]
    assert set(data["runs"]) == {"r1", "r2", "r3"}
    assert data["runs"]["r1"][0]["value"] == 1.0
    assert data["name"] == "loss"


# ---------------------------------------------------------------------------
# nebo alerts ls|get|set|rm
# ---------------------------------------------------------------------------


def test_alerts_set_parses_condition(monkeypatch):
    received: dict = {}
    def fake(title, condition, **kw):
        received["title"] = title
        received["condition"] = condition
        received.update(kw)
        return {"id": "abc12345", "title": title}
    monkeypatch.setattr("nebo.client.set_alert", fake)
    _run_cli([
        "alerts", "set",
        "--title", "loss diverged",
        "--condition", "train/loss > 5",
        "--level", "WARN",
        "--loggable", "__global__",
        "--run", "r1",
        "--json",
    ])
    assert received["title"] == "loss diverged"
    assert received["condition"] == {"metric": "train/loss", "op": ">", "value": 5.0}
    assert received["level"] == 30
    assert received["loggable_id"] == "__global__"
    assert received["run_id"] == "r1"


def test_alerts_set_rejects_bad_condition(monkeypatch):
    monkeypatch.setattr("nebo.client.set_alert", lambda *a, **k: {})
    with pytest.raises(SystemExit):
        _run_cli(["alerts", "set", "--title", "t", "--condition", "loss soars"])


def test_alerts_ls_json(monkeypatch):
    monkeypatch.setattr(
        "nebo.client.list_alerts",
        lambda **kw: {"alerts": [{"id": "a1", "triggered_by": "cli", "title": "t"}]},
    )
    out = _run_cli(["alerts", "ls", "--json"])
    assert json.loads(out)["alerts"][0]["id"] == "a1"


def test_alerts_rm(monkeypatch):
    received: dict = {}
    def fake(rule_id, **kw):
        received["rule_id"] = rule_id
        return {"status": "deleted", "id": rule_id}
    monkeypatch.setattr("nebo.client.delete_alert", fake)
    out = _run_cli(["alerts", "rm", "a1", "--json"])
    assert received["rule_id"] == "a1"
    assert json.loads(out)["status"] == "deleted"


def test_metrics_log_passes_entries(monkeypatch):
    received: dict = {}
    def fake(entries, **kw):
        received["entries"] = entries
        received.update(kw)
        return {"status": "ok"}
    monkeypatch.setattr("nebo.client.log_metric", fake)
    payload = '[{"name":"x","value":0.1,"type":"line"}]'
    _run_cli(["metrics", "log", "--entries-json", payload, "--run", "abc", "--json"])
    assert received["entries"] == [{"name": "x", "value": 0.1, "type": "line"}]
    assert received["run_id"] == "abc"


def test_metrics_list_derives_from_run_status(monkeypatch):
    monkeypatch.setattr(
        "nebo.client.get_run_status",
        lambda rid, **c: {"metrics_index": {"node_a": ["loss", "accuracy"]}},
    )
    out = _run_cli(["metrics", "list", "--run", "abc", "--json"])
    parsed = json.loads(out)
    assert parsed["node_a"] == ["loss", "accuracy"]


# ---------------------------------------------------------------------------
# nebo text log | images log | audio log
# ---------------------------------------------------------------------------


def test_text_log_passes_entries(monkeypatch):
    received: dict = {}

    def fake_log_text(entries, **kw):
        received["entries"] = entries
        received.update(kw)
        return {"status": "ok"}

    monkeypatch.setattr("nebo.client.log_text", fake_log_text)
    _run_cli([
        "text", "log",
        "--entries-json", '[{"message":"hello"}]',
        "--run", "abc",
        "--json",
    ])
    assert received["entries"] == [{"message": "hello"}]
    assert received["run_id"] == "abc"


def test_images_log_passes_entries(monkeypatch, tmp_path):
    f = tmp_path / "x.png"
    f.write_bytes(b"PNGfake")
    received: dict = {}
    monkeypatch.setattr(
        "nebo.client.log_image",
        lambda entries, **kw: received.setdefault("entries", entries) or {"status": "ok"},
    )
    _run_cli([
        "images", "log",
        "--entries-json", json.dumps([{"name": "x", "path": str(f)}]),
        "--run", "abc",
        "--json",
    ])
    assert received["entries"][0]["path"] == str(f)


def test_audio_log_passes_entries(monkeypatch, tmp_path):
    f = tmp_path / "x.wav"
    f.write_bytes(b"RIFFfake")
    received: dict = {}
    monkeypatch.setattr(
        "nebo.client.log_audio",
        lambda entries, **kw: received.setdefault("entries", entries) or {"status": "ok"},
    )
    _run_cli([
        "audio", "log",
        "--entries-json", json.dumps([{"name": "snd", "path": str(f)}]),
        "--run", "abc",
        "--json",
    ])
    assert received["entries"][0]["name"] == "snd"


# ---------------------------------------------------------------------------
# nebo text ls | load | status  (Task 20 — --json + nebo.client routing)
# ---------------------------------------------------------------------------


def test_text_ls_json(monkeypatch):
    monkeypatch.setattr(
        "nebo.client.get_text",
        lambda **c: {"texts": [{"timestamp": 1, "loggable_id": "n", "message": "m"}]},
    )
    out = _run_cli(["text", "ls", "--json"])
    assert json.loads(out)["texts"][0]["message"] == "m"


def test_text_ls_human(monkeypatch, capsys):
    monkeypatch.setattr(
        "nebo.client.get_text",
        lambda **c: {"texts": [
            {"loggable_id": "node_a", "name": "status", "message": "hello", "step": 3},
            {"loggable_id": "node_a", "name": "text", "message": "plain", "step": None},
        ]},
    )
    buf = io.StringIO()
    with redirect_stdout(buf), patch("sys.argv", ["nebo", "text", "ls"]):
        from nebo.cli import main
        main()
    out = buf.getvalue()
    # Per-entry format:   [loggable_id] name@step: message  (step tag omitted
    # when step is None).
    assert "  [node_a] status@3: hello" in out
    assert "  [node_a] text: plain" in out


def test_text_ls_no_entries(monkeypatch):
    monkeypatch.setattr(
        "nebo.client.get_text",
        lambda **c: {"texts": []},
    )
    out = _run_cli(["text", "ls"])
    assert "No text entries found" in out




def test_load_json(monkeypatch, tmp_path):
    f = tmp_path / "x.nebo"
    f.write_bytes(b"fake")
    monkeypatch.setattr(
        "nebo.client.load_file",
        lambda fp, **c: {"status": "loaded", "filepath": fp},
    )
    out = _run_cli(["load", str(f), "--json"])
    parsed = json.loads(out)
    assert parsed["status"] == "loaded"


def test_load_human(monkeypatch, tmp_path):
    f = tmp_path / "y.nebo"
    f.write_bytes(b"fake")
    monkeypatch.setattr(
        "nebo.client.load_file",
        lambda fp, **c: {"run_id": "abc"},
    )
    out = _run_cli(["load", str(f)])
    assert "Loaded" in out


def test_status_json(monkeypatch):
    monkeypatch.setattr(
        "nebo.client.get_run_history",
        lambda **c: {"runs": [{"id": "r1", "status": "completed", "script_path": "x.py"}]},
    )
    out = _run_cli(["status", "--json"])
    parsed = json.loads(out)
    assert parsed["daemon"] == "running"
    assert parsed["runs"][0]["id"] == "r1"


def test_status_daemon_down(monkeypatch):
    def boom(**c):
        raise ConnectionError("refused")

    monkeypatch.setattr("nebo.client.get_run_history", boom)
    out = _run_cli(["status"])
    assert "not running" in out


def test_text_ls_passes_run_and_node(monkeypatch):
    received: dict = {}

    def fake_get_text(**c):
        received.update(c)
        return {"texts": []}

    monkeypatch.setattr("nebo.client.get_text", fake_get_text)
    _run_cli(["text", "ls", "--run", "r1", "--node", "train", "--limit", "5"])
    assert received["run_id"] == "r1"
    assert received["loggable_id"] == "train"
    assert received["limit"] == 5



def _run_cli_with_stderr(argv: list[str]) -> tuple[int, str]:
    """Invoke nebo.cli.main with argv (no program name); return (exit_code, stderr)."""
    from nebo.cli import main
    err = io.StringIO()
    code = 0
    with redirect_stderr(err), patch("sys.argv", ["nebo"] + argv):
        try:
            main()
        except SystemExit as e:
            code = int(e.code or 0)
    return code, err.getvalue()


def test_serve_refuses_remote_equal_to_logdir(tmp_path, monkeypatch):
    # Stub out the "is a daemon already running" probe so this test isn't
    # affected by a developer who happens to have `nebo serve` running on
    # the default port — cmd_serve short-circuits with "already running"
    # before reaching the conflict check otherwise.
    monkeypatch.setattr("nebo.cli._is_alive", lambda port: False)
    code, err = _run_cli_with_stderr([
        "serve",
        "--logdir", str(tmp_path),
        "--remote", str(tmp_path),
    ])
    assert code == 2
    assert "cannot be the watched --logdir" in err


def test_serve_no_local_without_remote_errors(tmp_path, monkeypatch):
    monkeypatch.setattr("nebo.cli._is_alive", lambda port: False)
    code, err = _run_cli_with_stderr([
        "serve", "--logdir", str(tmp_path), "--no-local",
    ])
    assert code == 2
    assert "needs --remote" in err


def test_serve_remote_and_ephemeral_mutually_exclusive(tmp_path, monkeypatch):
    monkeypatch.setattr("nebo.cli._is_alive", lambda port: False)
    code, err = _run_cli_with_stderr([
        "serve", "--logdir", str(tmp_path),
        "--remote", str(tmp_path / "r"), "--remote-ephemeral",
    ])
    assert code == 2  # argparse mutually-exclusive group


def test_serve_rejects_removed_no_store():
    code, err = _run_cli_with_stderr(["serve", "--no-store"])
    # argparse rejects unknown --no-store. Either exit 2 with argparse
    # error, or exit 2 with our explicit removal-error message.
    assert code == 2


def _make_cache_db(cache_dir, logdir):
    from nebo.server.cache import RunCache, resolve_cache_path

    path = cache_dir / resolve_cache_path(logdir).name
    c = RunCache(path, logdir=logdir)
    c.start()
    c.close()
    return path


class TestCacheCommands:
    def test_cache_ls_json(self, tmp_path):
        cache_dir = tmp_path / "cachedir"
        cache_dir.mkdir()
        _make_cache_db(cache_dir, tmp_path / "logs_a")
        _make_cache_db(cache_dir, tmp_path / "logs_b")
        out = _run_cli(["cache", "ls", "--json", "--cache-dir", str(cache_dir)])
        caches = json.loads(out)["caches"]
        assert len(caches) == 2
        logdirs = {c["logdir"] for c in caches}
        assert str((tmp_path / "logs_a").resolve()) in logdirs
        assert all(c["size_bytes"] > 0 for c in caches)

    def test_cache_ls_empty(self, tmp_path):
        out = _run_cli(["cache", "ls", "--cache-dir", str(tmp_path / "none")])
        assert "no cache databases" in out

    def test_cache_clear_by_logdir(self, tmp_path):
        cache_dir = tmp_path / "cachedir"
        cache_dir.mkdir()
        keep = _make_cache_db(cache_dir, tmp_path / "logs_a")
        gone = _make_cache_db(cache_dir, tmp_path / "logs_b")
        _run_cli([
            "cache", "clear", str(tmp_path / "logs_b"),
            "--cache-dir", str(cache_dir),
        ])
        assert keep.exists()
        assert not gone.exists()

    def test_cache_clear_all(self, tmp_path):
        cache_dir = tmp_path / "cachedir"
        cache_dir.mkdir()
        a = _make_cache_db(cache_dir, tmp_path / "logs_a")
        b = _make_cache_db(cache_dir, tmp_path / "logs_b")
        _run_cli(["cache", "clear", "--all", "--cache-dir", str(cache_dir)])
        assert not a.exists() and not b.exists()

    def test_cache_clear_requires_target(self, tmp_path):
        code, err = _run_cli_with_stderr(
            ["cache", "clear", "--cache-dir", str(tmp_path)]
        )
        assert code == 2
        assert "--all" in err


class TestIsAlive:
    """The daemon liveness probe must work without httpx installed."""

    @staticmethod
    def _health_server():
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/health":
                    body = b'{"status": "ok"}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404)

            def log_message(self, *args):  # keep pytest output pristine
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def test_is_alive_without_httpx(self, block_import):
        from nebo.cli import _is_alive

        server = self._health_server()
        try:
            with block_import("httpx"):
                assert _is_alive(server.server_address[1]) is True
        finally:
            server.shutdown()

    def test_is_alive_false_on_dead_port(self):
        import socket

        from nebo.cli import _is_alive

        with socket.socket() as s:  # grab a port that is definitely closed
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        assert _is_alive(port) is False


class TestMediaCli:
    """`images ls` / `audio ls` list a run's media; `images get` /
    `audio get` download one object so an agent can Read/attach it."""

    def test_images_ls_json(self, monkeypatch):
        calls = {}

        def fake(run_id, **conn):
            calls["run_id"] = run_id
            return {"images": {"train": [
                {"node": "train", "media_id": "m1", "name": "sample",
                 "step": 3, "timestamp": 1.0, "labels": None},
            ]}}

        monkeypatch.setattr("nebo.client.list_images", fake)
        out = _run_cli(["images", "ls", "--run", "r1", "--json"])
        assert calls["run_id"] == "r1"
        assert json.loads(out)["images"]["train"][0]["media_id"] == "m1"

    def test_images_ls_human_lists_media_ids(self, monkeypatch):
        monkeypatch.setattr(
            "nebo.client.list_images",
            lambda run_id, **conn: {"images": {"train": [
                {"node": "train", "media_id": "m1", "name": "sample",
                 "step": 3, "timestamp": 1.0, "labels": None},
            ]}},
        )
        out = _run_cli(["images", "ls", "--run", "r1"])
        assert "m1" in out and "sample" in out and "train" in out

    def test_audio_ls_json(self, monkeypatch):
        monkeypatch.setattr(
            "nebo.client.list_audio",
            lambda run_id, **conn: {"audio": {"__global__": [
                {"node": "__global__", "media_id": "a1", "name": "clip",
                 "sr": 16000, "step": None, "timestamp": 2.0},
            ]}},
        )
        out = _run_cli(["audio", "ls", "--run", "r1", "--json"])
        assert json.loads(out)["audio"]["__global__"][0]["media_id"] == "a1"

    def test_images_get_writes_file_and_prints_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "nebo.client.get_media",
            lambda run_id, media_id, **conn: (b"\x89PNGdata", "image/png"),
        )
        target = tmp_path / "x.png"
        out = _run_cli(["images", "get", "m1", "--run", "r1", "-o", str(target)])
        assert target.read_bytes() == b"\x89PNGdata"
        assert str(target) in out

    def test_audio_get_default_name_uses_content_type(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "nebo.client.get_media",
            lambda run_id, media_id, **conn: (b"RIFFdata", "audio/wav"),
        )
        monkeypatch.chdir(tmp_path)
        _run_cli(["audio", "get", "a1", "--run", "r1"])
        assert (tmp_path / "a1.wav").read_bytes() == b"RIFFdata"

    def test_images_get_json_reports_path_and_type(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "nebo.client.get_media",
            lambda run_id, media_id, **conn: (b"\x89PNGdata", "image/png"),
        )
        target = tmp_path / "y.png"
        out = _run_cli(["images", "get", "m1", "--run", "r1", "-o", str(target), "--json"])
        info = json.loads(out)
        assert info["path"] == str(target)
        assert info["content_type"] == "image/png"
        assert info["bytes"] == len(b"\x89PNGdata")

    def test_images_get_rejects_audio_media(self, tmp_path, monkeypatch):
        """The kind-specific command refuses the other kind's media and
        points at the right command instead of writing a mislabeled file."""
        monkeypatch.setattr(
            "nebo.client.get_media",
            lambda run_id, media_id, **conn: (b"RIFFdata", "audio/wav"),
        )
        monkeypatch.chdir(tmp_path)
        code, err = _run_cli_with_stderr(["images", "get", "a1", "--run", "r1"])
        assert code == 1
        assert "audio/wav" in err and "nebo audio get" in err
        assert not list(tmp_path.iterdir())  # nothing written

    def test_no_top_level_media_command(self):
        code, _err = _run_cli_with_stderr(["media", "get", "m1", "--run", "r1"])
        assert code == 2  # argparse: unknown command
