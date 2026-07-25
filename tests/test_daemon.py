"""Tests for the daemon server state management."""

from __future__ import annotations

import threading
import time

import pytest

from nebo.server.daemon import DaemonState, Run, LoggableState, LogEntry


class TestDaemonState:
    """Tests for DaemonState run management."""

    def setup_method(self) -> None:
        self.state = DaemonState()

    def test_create_run(self) -> None:
        """Should create a run with correct defaults."""
        run = self.state.create_run("test_script.py", args=["--epochs", "10"])
        assert run.script_path == "test_script.py"
        assert run.args == ["--epochs", "10"]
        assert run.started_at is not None
        assert run.last_event_at > 0  # recency seeded at creation; no ended_at
        assert self.state.active_run_id == run.id

    def test_create_run_custom_id(self) -> None:
        """Should accept a custom run ID."""
        run = self.state.create_run("s.py", run_id="my_run")
        assert run.id == "my_run"
        assert "my_run" in self.state.runs

    def test_get_active_run(self) -> None:
        """Should return the run whose id is active_run_id."""
        run = self.state.create_run("s.py")
        assert self.state.get_active_run() is run

    @pytest.mark.asyncio
    async def test_get_active_run_none_after_completion(self) -> None:
        """run_completed clears active_run_id, so no run is active."""
        self.state.create_run("s.py", run_id="r1")
        await self.state.ingest_events(
            [{"type": "run_completed", "data": {}}], run_id="r1",
        )
        assert self.state.get_active_run() is None

    def test_get_latest_run(self) -> None:
        """Should return the most recent run."""
        self.state.create_run("a.py", run_id="r1")
        self.state.create_run("b.py", run_id="r2")
        latest = self.state.get_latest_run()
        assert latest is not None
        assert latest.id == "r2"


class TestDaemonEventIngestion:
    """Tests for event processing into run state."""

    def setup_method(self) -> None:
        self.state = DaemonState()

    @pytest.mark.asyncio
    async def test_ingest_loggable_register(self) -> None:
        """Should register nodes from events."""
        run = self.state.create_run("s.py", run_id="r1")
        await self.state.ingest_events([
            {"type": "loggable_register", "data": {"loggable_id": "my_func", "func_name": "my_func", "docstring": "Does stuff"}},
        ], "r1")
        assert "my_func" in run.loggables
        assert run.loggables["my_func"].docstring == "Does stuff"

    @pytest.mark.asyncio
    async def test_ingest_loggable_register_preserves_group(self) -> None:
        """loggable_register events carrying 'group' must land on LoggableState.group."""
        run = self.state.create_run("s.py", run_id="r1")
        await self.state.ingest_events([
            {
                "type": "loggable_register",
                "data": {
                    "loggable_id": "Agent.think",
                    "func_name": "think",
                    "docstring": None,
                    "group": "Agent",
                },
            },
        ], "r1")
        node = run.loggables["Agent.think"]
        assert node.group == "Agent"
        # And it must be exposed by the graph API payload the UI consumes.
        graph = run.get_graph()
        assert graph["nodes"]["Agent.think"]["group"] == "Agent"

    @pytest.mark.asyncio
    async def test_ingest_loggable_register_preserves_ui_hints(self) -> None:
        """loggable_register events carrying 'ui_hints' must reach the graph payload."""
        run = self.state.create_run("s.py", run_id="r1")
        await self.state.ingest_events([
            {
                "type": "loggable_register",
                "data": {
                    "loggable_id": "train",
                    "func_name": "train",
                    "ui_hints": {"collapsed": True, "color": "blue"},
                },
            },
        ], "r1")
        node = run.loggables["train"]
        assert node.ui_hints == {"collapsed": True, "color": "blue"}
        graph = run.get_graph()
        assert graph["nodes"]["train"]["ui_hints"] == {"collapsed": True, "color": "blue"}

    @pytest.mark.asyncio
    async def test_ingest_loggable_register_without_group_defaults_to_none(self) -> None:
        """A plain @nb.fn node without a group should still register, with group=None."""
        run = self.state.create_run("s.py", run_id="r1")
        await self.state.ingest_events([
            {
                "type": "loggable_register",
                "data": {"loggable_id": "plain", "func_name": "plain"},
            },
        ], "r1")
        node = run.loggables["plain"]
        assert node.group is None
        assert node.ui_hints is None
        graph = run.get_graph()
        assert graph["nodes"]["plain"]["group"] is None
        assert graph["nodes"]["plain"]["ui_hints"] is None

    @pytest.mark.asyncio
    async def test_ingest_log(self) -> None:
        """Should append log entries."""
        self.state.create_run("s.py", run_id="r1")
        await self.state.ingest_events([
            {"type": "loggable_register", "data": {"loggable_id": "n1", "func_name": "n1"}},
            {"type": "log", "loggable_id": "n1", "message": "hello world"},
        ], "r1")
        run = self.state.runs["r1"]
        assert len(run.logs) == 1
        assert run.logs[0].message == "hello world"

    @pytest.mark.asyncio
    async def test_log_event_name_round_trips(self) -> None:
        """Text-log events carry a stream name; absent name defaults to 'text'."""
        self.state.create_run("s.py", run_id="r1")
        await self.state.ingest_events([
            {"type": "log", "loggable_id": "__global__", "name": "status", "message": "hi"},
            {"type": "log", "loggable_id": "__global__", "message": "no-name"},
        ], "r1")
        run = self.state.runs["r1"]
        assert run.logs[0].name == "status"
        assert run.logs[1].name == "text"

    @pytest.mark.asyncio
    async def test_metric_before_register_is_not_dropped(self) -> None:
        """A metric whose loggable_register hasn't arrived yet (e.g. after a
        daemon restart mid-run) must be kept, not silently dropped — logs
        already always append, so the two should be symmetric."""
        self.state.create_run("s.py", run_id="r1")
        await self.state.ingest_events([
            {"type": "metric", "loggable_id": "train", "name": "loss",
             "metric_type": "line", "value": 0.5, "step": 0},
        ], "r1")
        run = self.state.runs["r1"]
        assert "train" in run.loggables
        assert run.loggables["train"].auto_seeded is True
        assert run.loggables["train"].metrics["loss"]["entries"][0]["value"] == 0.5

    @pytest.mark.asyncio
    async def test_late_register_upgrades_auto_seeded_loggable(self) -> None:
        """A real loggable_register arriving after an auto-seeded placeholder
        fills in metadata in place, without dropping it as a duplicate or
        discarding the metrics already attached."""
        self.state.create_run("s.py", run_id="r1")
        await self.state.ingest_events([
            {"type": "metric", "loggable_id": "train", "name": "loss",
             "metric_type": "line", "value": 0.5, "step": 0},
            {"type": "loggable_register", "data": {
                "loggable_id": "train", "func_name": "train_fn",
                "kind": "node", "docstring": "trains",
            }},
        ], "r1")
        lg = self.state.runs["r1"].loggables["train"]
        assert lg.func_name == "train_fn"
        assert lg.docstring == "trains"
        assert lg.auto_seeded is False
        assert len(lg.metrics["loss"]["entries"]) == 1

    @pytest.mark.asyncio
    async def test_ingest_edge(self) -> None:
        """Should track DAG edges and mark targets as non-source."""
        self.state.create_run("s.py", run_id="r1")
        await self.state.ingest_events([
            {"type": "loggable_register", "data": {"loggable_id": "a", "func_name": "a"}},
            {"type": "loggable_register", "data": {"loggable_id": "b", "func_name": "b"}},
            {"type": "edge", "data": {"source": "a", "target": "b"}},
        ], "r1")
        run = self.state.runs["r1"]
        assert len(run.edges) == 1
        assert run.loggables["a"].is_source is True
        assert run.loggables["b"].is_source is False

    @pytest.mark.asyncio
    async def test_ingest_metric(self) -> None:
        """Should store metrics on nodes."""
        self.state.create_run("s.py", run_id="r1")
        await self.state.ingest_events([
            {"type": "loggable_register", "data": {"loggable_id": "train", "func_name": "train"}},
            {"type": "metric", "loggable_id": "train", "name": "loss", "value": 0.5, "step": 0},
            {"type": "metric", "loggable_id": "train", "name": "loss", "value": 0.3, "step": 1},
        ], "r1")
        run = self.state.runs["r1"]
        assert "loss" in run.loggables["train"].metrics
        series = run.loggables["train"].metrics["loss"]
        assert series["type"] == "line"
        assert len(series["entries"]) == 2
        assert series["entries"][0]["value"] == 0.5
        assert series["entries"][1]["value"] == 0.3

    @pytest.mark.asyncio
    async def test_ingest_metric_with_type_and_tags(self) -> None:
        """metric events carrying metric_type + tags must land on the new typed series."""
        run = self.state.create_run("s.py", run_id="r1")
        await self.state.ingest_events([
            {"type": "loggable_register", "data": {"loggable_id": "n", "func_name": "n"}},
            {"type": "metric", "loggable_id": "n", "name": "loss",
             "metric_type": "line", "value": 0.5, "step": 0, "tags": ["warmup"]},
            {"type": "metric", "loggable_id": "n", "name": "counts",
             "metric_type": "bar", "value": {"a": 1, "b": 2}, "step": 0, "tags": []},
        ], "r1")
        loss = run.loggables["n"].metrics["loss"]
        assert loss["type"] == "line"
        assert loss["entries"][-1]["tags"] == ["warmup"]
        assert loss["entries"][-1]["value"] == 0.5
        counts = run.loggables["n"].metrics["counts"]
        assert counts["type"] == "bar"
        assert counts["entries"][-1]["value"] == {"a": 1, "b": 2}

    @pytest.mark.asyncio
    async def test_ingest_metric_scatter_accumulates(self) -> None:
        """Scatter is accumulating like line: repeated emissions append
        rather than overwrite."""
        run = self.state.create_run("s.py", run_id="r1")
        await self.state.ingest_events([
            {"type": "loggable_register", "data": {"loggable_id": "n", "func_name": "n"}},
            {"type": "metric", "loggable_id": "n", "name": "embed",
             "metric_type": "scatter",
             "value": {"dog": {"x": [0], "y": [8]}}, "step": 0, "tags": []},
            {"type": "metric", "loggable_id": "n", "name": "embed",
             "metric_type": "scatter",
             "value": {"cat": {"x": [1], "y": [4]}}, "step": 1, "tags": []},
        ], "r1")
        embed = run.loggables["n"].metrics["embed"]
        assert embed["type"] == "scatter"
        assert len(embed["entries"]) == 2
        assert embed["entries"][0]["step"] == 0
        assert embed["entries"][0]["value"] == {"dog": {"x": [0], "y": [8]}}
        assert embed["entries"][1]["step"] == 1
        assert embed["entries"][1]["value"] == {"cat": {"x": [1], "y": [4]}}

    @pytest.mark.asyncio
    async def test_ingest_metric_snapshots_overwrite(self) -> None:
        """Bar/pie/histogram remain snapshots: re-emitting overwrites the
        prior entry rather than appending."""
        run = self.state.create_run("s.py", run_id="r1")
        await self.state.ingest_events([
            {"type": "loggable_register", "data": {"loggable_id": "n", "func_name": "n"}},
            {"type": "metric", "loggable_id": "n", "name": "counts",
             "metric_type": "bar", "value": {"a": 1}, "step": None, "tags": []},
            {"type": "metric", "loggable_id": "n", "name": "counts",
             "metric_type": "bar", "value": {"a": 2, "b": 3}, "step": None, "tags": []},
        ], "r1")
        counts = run.loggables["n"].metrics["counts"]
        assert counts["type"] == "bar"
        assert len(counts["entries"]) == 1
        assert counts["entries"][0]["value"] == {"a": 2, "b": 3}

    @pytest.mark.asyncio
    async def test_ingest_creates_implicit_run(self) -> None:
        """Should create an implicit run if none exists."""
        await self.state.ingest_events([
            {"type": "log", "message": "orphan log"},
        ])
        assert len(self.state.runs) == 1

    @pytest.mark.asyncio
    async def test_ingest_ui_config_exposed_via_get_graph(self) -> None:
        """ui_config events must reach the graph payload the UI consumes."""
        run = self.state.create_run("s.py", run_id="r1")
        await self.state.ingest_events([
            {
                "type": "ui_config",
                "data": {
                    "layout": "horizontal",
                    "view": "flat",
                    "collapsed": True,
                    "minimap": False,
                    "theme": "dark",
                },
            },
        ], "r1")
        assert run.ui_config == {
            "layout": "horizontal",
            "view": "flat",
            "collapsed": True,
            "minimap": False,
            "theme": "dark",
        }
        graph = run.get_graph()
        assert graph["ui_config"] == {
            "layout": "horizontal",
            "view": "flat",
            "collapsed": True,
            "minimap": False,
            "theme": "dark",
        }

    @pytest.mark.asyncio
    async def test_get_graph_ui_config_defaults_to_none(self) -> None:
        """When no ui_config event is sent, get_graph() exposes None."""
        run = self.state.create_run("s.py", run_id="r1")
        await self.state.ingest_events([
            {"type": "loggable_register", "data": {"loggable_id": "n1", "func_name": "n1"}},
        ], "r1")
        graph = run.get_graph()
        assert graph["ui_config"] is None

    @pytest.mark.asyncio
    async def test_ingest_image_with_labels_preserves_labels(self) -> None:
        """Image events carrying labels must land on LoggableState.images entries."""
        run = self.state.create_run("s.py", run_id="r1")
        await self.state.ingest_events([
            {"type": "loggable_register", "data": {"loggable_id": "seg", "func_name": "seg"}},
            {
                "type": "image",
                "loggable_id": "seg",
                "name": "pred",
                "data": "AA==",
                "step": 0,
                "labels": {"boxes": [[1, 2, 3, 4]], "points": [[5, 6]]},
            },
        ], "r1")
        images = run.loggables["seg"].images
        assert len(images) == 1
        assert images[0]["labels"] == {"boxes": [[1, 2, 3, 4]], "points": [[5, 6]]}


class TestRunSummary:
    """Tests for Run serialization methods."""

    def test_get_summary(self) -> None:
        """Should return a concise summary dict."""
        run = Run(id="r1", script_path="train.py", args=["--lr", "0.01"])
        summary = run.get_summary()
        assert summary["id"] == "r1"
        assert summary["script_path"] == "train.py"
        assert summary["args"] == ["--lr", "0.01"]

    def test_get_graph(self) -> None:
        """Should return serializable graph dict."""
        run = Run(id="r1", script_path="s.py")
        run.loggables["a"] = LoggableState(loggable_id="a", func_name="a", docstring="Step A")
        run.edges.append({"source": "a", "target": "b"})
        graph = run.get_graph()
        assert "a" in graph["nodes"]
        assert graph["nodes"]["a"]["docstring"] == "Step A"
        assert len(graph["edges"]) == 1

    def test_get_graph_excludes_global_loggable(self) -> None:
        """Global-kind loggables must not appear under the graph's nodes key."""
        run = Run(id="r1", script_path="s.py")
        run.loggables["__global__"] = LoggableState(
            loggable_id="__global__", kind="global"
        )
        run.loggables["a"] = LoggableState(loggable_id="a", func_name="a")
        graph = run.get_graph()
        assert "__global__" not in graph["nodes"]
        assert "a" in graph["nodes"]

    def test_get_graph_filters_edges_touching_global(self) -> None:
        """An edge whose endpoint is a known global loggable is dropped."""
        run = Run(id="r1", script_path="s.py")
        run.loggables["__global__"] = LoggableState(
            loggable_id="__global__", kind="global"
        )
        run.loggables["a"] = LoggableState(loggable_id="a", func_name="a")
        run.loggables["b"] = LoggableState(loggable_id="b", func_name="b")
        run.edges.append({"source": "a", "target": "b"})
        run.edges.append({"source": "__global__", "target": "a"})
        run.edges.append({"source": "a", "target": "__global__"})
        graph = run.get_graph()
        assert graph["edges"] == [{"source": "a", "target": "b"}]

    @pytest.mark.asyncio
    async def test_run_start_seeds_global_loggable(self) -> None:
        """A run_start event must seed the __global__ loggable with kind=global."""
        state = DaemonState()
        run = state.create_run("s.py", run_id="r1")
        await state.ingest_events(
            [{"type": "run_start", "data": {"script_path": "s.py"}}],
            run_id="r1",
        )
        assert "__global__" in run.loggables
        assert run.loggables["__global__"].kind == "global"

    @pytest.mark.asyncio
    async def test_run_start_global_seed_is_idempotent(self) -> None:
        """Re-firing run_start must not overwrite an already-seeded global."""
        state = DaemonState()
        run = state.create_run("s.py", run_id="r1")
        await state.ingest_events(
            [{"type": "run_start", "data": {"script_path": "s.py"}}],
            run_id="r1",
        )
        run.loggables["__global__"].logs.append({"message": "marker"})
        await state.ingest_events(
            [{"type": "run_start", "data": {"script_path": "s.py"}}],
            run_id="r1",
        )
        assert any(
            entry.get("message") == "marker"
            for entry in run.loggables["__global__"].logs
        )


class TestGetNodeEndpoint:
    """HTTP-level tests for the GET /loggables/{loggable_id} endpoint.

    Regression: the global loggable endpoint historically omitted
    `metrics` and `progress` from its response, which broke the
    `nebo_get_metrics` MCP tool (it reads from this endpoint and always
    saw an empty metrics dict).
    """

    def _client_with_metric(self):
        from fastapi.testclient import TestClient
        from nebo.server.daemon import DaemonState, create_daemon_app

        state = DaemonState()
        run = state.create_run("s.py", run_id="r1")
        run.status = "running"
        import asyncio
        asyncio.get_event_loop().run_until_complete(
            state.ingest_events([
                {"type": "loggable_register", "data": {"loggable_id": "train", "func_name": "train"}},
                {"type": "metric", "loggable_id": "train", "name": "loss", "value": 0.5, "step": 0},
                {"type": "metric", "loggable_id": "train", "name": "loss", "value": 0.3, "step": 1},
                {"type": "progress", "loggable_id": "train", "data": {"current": 1, "total": 2, "name": "epoch"}},
            ], "r1")
        )
        app = create_daemon_app(state=state)
        return TestClient(app)

    def test_get_node_returns_metrics(self) -> None:
        """GET /loggables/{loggable_id} must include the loggable's metrics dict."""
        client = self._client_with_metric()
        resp = client.get("/loggables/train")
        assert resp.status_code == 200
        body = resp.json()
        assert "metrics" in body, f"missing 'metrics' field in response: {body}"
        assert "loss" in body["metrics"]
        series = body["metrics"]["loss"]
        assert series["type"] == "line"
        assert len(series["entries"]) == 2

    def test_get_node_returns_progress(self) -> None:
        """GET /loggables/{loggable_id} must include the loggable's progress state."""
        client = self._client_with_metric()
        resp = client.get("/loggables/train")
        assert resp.status_code == 200
        body = resp.json()
        assert "progress" in body, f"missing 'progress' field in response: {body}"

    def test_get_node_returns_kind_node(self) -> None:
        """GET /loggables/{loggable_id} must expose kind so the UI can distinguish nodes from the global."""
        client = self._client_with_metric()
        body = client.get("/loggables/train").json()
        assert body["kind"] == "node"

    def test_get_global_loggable_returns_kind_global(self) -> None:
        """The pre-seeded __global__ loggable must be fetchable with kind=global."""
        from fastapi.testclient import TestClient
        from nebo.server.daemon import DaemonState, create_daemon_app
        import asyncio

        state = DaemonState()
        state.create_run("s.py", run_id="r1")
        asyncio.get_event_loop().run_until_complete(
            state.ingest_events(
                [{"type": "run_start", "data": {"script_path": "s.py"}}], "r1"
            )
        )
        client = TestClient(create_daemon_app(state=state))
        resp = client.get("/runs/r1/loggables/__global__")
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "global"
        assert body["loggable_id"] == "__global__"


class TestRunCompletedEventClearsActiveRun:
    """Regression test for Bug 10 via the `/events` ingest path.

    Pipelines finalize by POSTing a `run_completed` event to `/events`,
    which is handled inline in `ingest_events`. Previously that branch
    updated `run.status` but never cleared `self.active_run_id`, so
    `/health` and `nebo status` kept reporting a finished run as "active"
    indefinitely.
    """

    def test_run_completed_event_clears_active_run_id(self) -> None:
        from fastapi.testclient import TestClient
        from nebo.server.daemon import DaemonState, create_daemon_app

        state = DaemonState()
        state.create_run("s.py", run_id="bug10_repro")
        assert state.active_run_id == "bug10_repro"

        app = create_daemon_app(state=state)
        client = TestClient(app)

        resp = client.post(
            "/events?run_id=bug10_repro",
            json=[{"type": "run_completed", "data": {"run_id": "bug10_repro"}}],
        )
        assert resp.status_code == 200

        assert state.active_run_id is None, (
            f"active_run_id should be None after run_completed event, "
            f"got {state.active_run_id!r}"
        )
        # run_completed carries no ended_at (no lifecycle state) — it is
        # recorded only as a significant event, which /events/wait fires on.
        assert any(
            e["type"] == "run_completed"
            for e in state.runs["bug10_repro"].significant_events
        )

    def test_run_completed_event_preserves_other_active_run(self) -> None:
        """A run_completed event for a non-active run must not clear active_run_id."""
        from fastapi.testclient import TestClient
        from nebo.server.daemon import DaemonState, create_daemon_app

        state = DaemonState()
        state.create_run("a.py", run_id="r1")
        state.create_run("b.py", run_id="r2")
        assert state.active_run_id == "r2"

        app = create_daemon_app(state=state)
        client = TestClient(app)

        resp = client.post(
            "/events?run_id=r1",
            json=[{"type": "run_completed", "data": {"run_id": "r1"}}],
        )
        assert resp.status_code == 200
        assert state.active_run_id == "r2"


class TestApiTokenAuth:
    """Bearer-token auth for the daemon. With NEBO_API_TOKEN set, the
    NEBO_READ_MODE / NEBO_WRITE_MODE env vars decide which sides of
    the API require the token. /health and the static UI bundle stay
    open in every mode."""

    def _app(self, monkeypatch, *, token=None, read=None, write=None):
        from fastapi.testclient import TestClient
        from nebo.server.daemon import DaemonState, create_daemon_app

        if token is not None:
            monkeypatch.setenv("NEBO_API_TOKEN", token)
        else:
            monkeypatch.delenv("NEBO_API_TOKEN", raising=False)
        if read is not None:
            monkeypatch.setenv("NEBO_READ_MODE", read)
        else:
            monkeypatch.delenv("NEBO_READ_MODE", raising=False)
        if write is not None:
            monkeypatch.setenv("NEBO_WRITE_MODE", write)
        else:
            monkeypatch.delenv("NEBO_WRITE_MODE", raising=False)

        state = DaemonState()
        return TestClient(create_daemon_app(state=state)), state

    # ── /health stays open in every config ──

    def test_health_open_without_token(self, monkeypatch) -> None:
        client, _ = self._app(monkeypatch, token="s3cret")
        assert client.get("/health").status_code == 200

    # ── Default mode: read=public, write=private ──

    def test_default_read_public(self, monkeypatch) -> None:
        # GET passes without a token because the default is public read.
        client, _ = self._app(monkeypatch, token="s3cret")
        assert client.get("/runs").status_code == 200

    def test_default_write_private_rejects_without_token(self, monkeypatch) -> None:
        # POST without a token fails because the default is private write.
        client, _ = self._app(monkeypatch, token="s3cret")
        resp = client.post("/events", json=[{"type": "log", "message": "x"}])
        assert resp.status_code == 401

    def test_default_write_accepts_token_header(self, monkeypatch) -> None:
        client, _ = self._app(monkeypatch, token="s3cret")
        resp = client.post(
            "/events",
            json=[{"type": "log", "message": "x"}],
            headers={"X-Nebo-Token": "s3cret"},
        )
        assert resp.status_code == 200

    def test_default_write_accepts_token_query(self, monkeypatch) -> None:
        # Browser/iframe flows can't set custom headers so the daemon
        # also accepts the token via `?token=`.
        client, _ = self._app(monkeypatch, token="s3cret")
        resp = client.post(
            "/events?token=s3cret",
            json=[{"type": "log", "message": "x"}],
        )
        assert resp.status_code == 200

    # ── Explicit read=private (fully private dashboard) ──

    def test_read_private_rejects_get_without_token(self, monkeypatch) -> None:
        client, _ = self._app(monkeypatch, token="s3cret", read="private")
        assert client.get("/runs").status_code == 401

    def test_read_private_accepts_get_with_token(self, monkeypatch) -> None:
        client, _ = self._app(monkeypatch, token="s3cret", read="private")
        resp = client.get("/runs", headers={"X-Nebo-Token": "s3cret"})
        assert resp.status_code == 200

    # ── Explicit write=public (read-only dashboard mistakenly opens writes) ──

    def test_write_public_accepts_post_without_token(self, monkeypatch) -> None:
        client, _ = self._app(monkeypatch, token="s3cret", write="public")
        resp = client.post("/events", json=[{"type": "log", "message": "x"}])
        assert resp.status_code == 200

    # ── No token at all → both gates open (preserves local dev) ──

    def test_unset_token_leaves_routes_open(self, monkeypatch) -> None:
        client, _ = self._app(monkeypatch)
        assert client.get("/runs").status_code == 200
        resp = client.post("/events", json=[{"type": "log", "message": "x"}])
        assert resp.status_code == 200


class TestMetricsQueryFilters:
    """Tests for ?tag= and ?step= query-string filters on the per-loggable endpoint.

    These filters only apply to accumulating series (line/scatter) that carry
    tags and step fields; snapshot types (bar/pie/histogram) are unaffected
    because their entries don't carry meaningful tags/steps.
    """

    def _make_client(self, run_id: str, metrics: dict):
        """Set up a TestClient with a single run + loggable pre-populated."""
        from fastapi.testclient import TestClient
        from nebo.server.daemon import DaemonState, LoggableState, create_daemon_app

        state = DaemonState()
        run = state.create_run("test.py", run_id=run_id)
        run.status = "running"
        lg = LoggableState(loggable_id="node_a", kind="node")
        run.loggables["node_a"] = lg
        lg.metrics.update(metrics)
        app = create_daemon_app(state=state)
        return TestClient(app), state

    def test_metrics_filter_by_tag(self) -> None:
        """?tag=train must return only line entries whose tags list includes 'train'."""
        client, _ = self._make_client(
            run_id="r_filter_tag",
            metrics={
                "loss": {
                    "type": "line",
                    "entries": [
                        {"step": 0, "value": 1.0, "tags": ["train"], "timestamp": 0},
                        {"step": 1, "value": 0.5, "tags": ["val"], "timestamp": 1},
                        {"step": 2, "value": 0.4, "tags": ["train"], "timestamp": 2},
                    ],
                }
            },
        )
        resp = client.get("/runs/r_filter_tag/loggables/node_a?name=loss&tag=train")
        assert resp.status_code == 200
        entries = resp.json()["metrics"]["loss"]["entries"]
        assert [e["step"] for e in entries] == [0, 2]

    def test_metrics_filter_by_step(self) -> None:
        """?step=1 must return only entries whose step equals 1."""
        client, _ = self._make_client(
            run_id="r_filter_step",
            metrics={
                "loss": {
                    "type": "line",
                    "entries": [
                        {"step": 0, "value": 1.0, "tags": [], "timestamp": 0},
                        {"step": 1, "value": 0.5, "tags": [], "timestamp": 1},
                        {"step": 2, "value": 0.4, "tags": [], "timestamp": 2},
                    ],
                }
            },
        )
        resp = client.get("/runs/r_filter_step/loggables/node_a?name=loss&step=1")
        assert resp.status_code == 200
        entries = resp.json()["metrics"]["loss"]["entries"]
        assert [e["step"] for e in entries] == [1]

    def test_metrics_filter_by_name(self) -> None:
        """?name=loss must return only the 'loss' series, not 'acc'."""
        client, _ = self._make_client(
            run_id="r_filter_name",
            metrics={
                "loss": {
                    "type": "line",
                    "entries": [{"step": 0, "value": 1.0, "tags": [], "timestamp": 0}],
                },
                "acc": {
                    "type": "line",
                    "entries": [{"step": 0, "value": 0.9, "tags": [], "timestamp": 0}],
                },
            },
        )
        resp = client.get("/runs/r_filter_name/loggables/node_a?name=loss")
        assert resp.status_code == 200
        metrics = resp.json()["metrics"]
        assert "loss" in metrics
        assert "acc" not in metrics

    def test_metrics_filter_tag_and_step_compose(self) -> None:
        """?tag= and ?step= filters compose (both must match)."""
        client, _ = self._make_client(
            run_id="r_filter_both",
            metrics={
                "loss": {
                    "type": "line",
                    "entries": [
                        {"step": 1, "value": 0.9, "tags": ["train"], "timestamp": 0},
                        {"step": 1, "value": 0.8, "tags": ["val"], "timestamp": 1},
                        {"step": 2, "value": 0.7, "tags": ["train"], "timestamp": 2},
                    ],
                }
            },
        )
        resp = client.get("/runs/r_filter_both/loggables/node_a?name=loss&tag=train&step=1")
        assert resp.status_code == 200
        entries = resp.json()["metrics"]["loss"]["entries"]
        assert len(entries) == 1
        assert entries[0]["value"] == 0.9

    def test_metrics_no_filter_returns_all(self) -> None:
        """Without filters, all entries are returned (no regression)."""
        client, _ = self._make_client(
            run_id="r_no_filter",
            metrics={
                "loss": {
                    "type": "line",
                    "entries": [
                        {"step": 0, "value": 1.0, "tags": ["train"], "timestamp": 0},
                        {"step": 1, "value": 0.5, "tags": ["val"], "timestamp": 1},
                    ],
                }
            },
        )
        resp = client.get("/runs/r_no_filter/loggables/node_a")
        assert resp.status_code == 200
        entries = resp.json()["metrics"]["loss"]["entries"]
        assert len(entries) == 2


def test_alert_event_is_appended_to_run():
    """Alert events must be persisted on Run.alerts and added to significant_events."""
    from fastapi.testclient import TestClient
    from nebo.server.daemon import DaemonState, create_daemon_app

    state = DaemonState()
    state.create_run("test.py", run_id="r_alert_1")

    app = create_daemon_app(state=state)
    client = TestClient(app)

    resp = client.post(
        "/events?run_id=r_alert_1",
        json=[{
            "type": "alert",
            "loggable_id": "node_a",
            "data": {
                "title": "Loss went up",
                "text": "epoch 12",
                "level": 30,
                "level_name": "WARN",
                "timestamp": 1700000000.0,
            },
        }],
    )
    assert resp.status_code == 200
    run = state.runs["r_alert_1"]
    assert len(run.alerts) == 1
    assert run.alerts[0]["title"] == "Loss went up"
    assert run.alerts[0]["level"] == 30
    assert run.alerts[0]["level_name"] == "WARN"
    assert run.alerts[0]["loggable_id"] == "node_a"

    # The per-run listing endpoint returns the same fired alerts.
    resp = client.get("/runs/r_alert_1/alerts")
    assert resp.status_code == 200
    alerts = resp.json()["alerts"]
    assert len(alerts) == 1
    assert alerts[0]["title"] == "Loss went up"

    resp = client.get("/runs/no_such_run/alerts")
    assert resp.status_code == 404


def test_alerts_wait_returns_alert():
    """Wait endpoint should unblock and return alert when one arrives at or above min_level."""
    from fastapi.testclient import TestClient
    from nebo.server.daemon import DaemonState, create_daemon_app

    state = DaemonState()
    state.create_run("test.py", run_id="r_wait_1")

    app = create_daemon_app(state=state)
    client = TestClient(app)

    result: dict = {}

    def waiter():
        resp = client.get("/runs/r_wait_1/alerts/wait?timeout=5")
        result["body"] = resp.json()

    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    time.sleep(0.2)
    client.post(
        "/events?run_id=r_wait_1",
        json=[{
            "type": "alert",
            "data": {"title": "x", "text": "y", "level": 20, "level_name": "INFO", "timestamp": 1.0},
        }],
    )
    t.join(timeout=5)
    assert result["body"]["status"] == "alert"
    assert result["body"]["alert"]["title"] == "x"


def test_alerts_wait_respects_min_level():
    """Wait endpoint should timeout when only alerts below min_level arrive."""
    from fastapi.testclient import TestClient
    from nebo.server.daemon import DaemonState, create_daemon_app

    state = DaemonState()
    state.create_run("test.py", run_id="r_wait_2")

    app = create_daemon_app(state=state)
    client = TestClient(app)

    result: dict = {}

    def waiter():
        resp = client.get("/runs/r_wait_2/alerts/wait?timeout=1&min_level=30")
        result["body"] = resp.json()

    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    time.sleep(0.1)
    # Submit an alert BELOW the min level — should not wake the waiter.
    client.post(
        "/events?run_id=r_wait_2",
        json=[{
            "type": "alert",
            "data": {"title": "i", "text": "", "level": 20, "level_name": "INFO", "timestamp": 1.0},
        }],
    )
    t.join(timeout=5)
    assert result["body"]["status"] == "timeout"


class TestOfflineTextLogsReachUI:
    """Text logs must survive into the projection and the REST snapshot the
    UI hydrates from on a cold page open — i.e. when no WebSocket client was
    connected while the run was emitting.

    Regression guard for "text logs don't appear in the UI unless I was
    actively viewing the page while the run was ongoing." The live UI path
    streams logs over the WebSocket; the offline path relies entirely on
    GET /runs/{id}/logs. These assert the offline path is complete for both
    node-scoped and __global__ logs, including logs interleaved with metrics
    and ingested incrementally (the network-mode wire shape).
    """

    def _offline_client(self):
        from fastapi.testclient import TestClient
        from nebo.server.daemon import DaemonState, create_daemon_app

        state = DaemonState()
        client = TestClient(create_daemon_app(state=state))
        rid = "offline1"
        # No WebSocket is ever connected — events arrive only via POST /events,
        # incrementally, exactly as the SDK pushes them in network mode.
        client.post(f"/events?run_id={rid}", json=[
            {"type": "run_start", "run_id": rid, "script_path": "t.py", "args": []},
        ])
        client.post(f"/events?run_id={rid}", json=[
            {"type": "loggable_register",
             "data": {"loggable_id": "step", "func_name": "step", "kind": "node"}},
        ])
        client.post(f"/events?run_id={rid}", json=[
            {"type": "log", "loggable_id": "step", "message": "node log A", "step": 0},
        ])
        client.post(f"/events?run_id={rid}", json=[
            {"type": "metric", "loggable_id": "step", "name": "loss",
             "metric_type": "line", "value": 1.0, "step": 0},
        ])
        client.post(f"/events?run_id={rid}", json=[
            {"type": "log", "loggable_id": "__global__", "message": "global log B"},
        ])
        client.post(f"/events?run_id={rid}", json=[
            {"type": "log", "loggable_id": "step", "message": "node log C", "step": 1},
        ])
        return client, rid

    def test_offline_logs_in_rest_snapshot(self) -> None:
        client, rid = self._offline_client()
        body = client.get(f"/runs/{rid}/logs?limit=500").json()
        messages = [l["message"] for l in body["logs"]]
        assert messages == ["node log A", "global log B", "node log C"], messages

    def test_offline_global_logs_present(self) -> None:
        """Logs emitted outside any @nb.fn (the __global__ loggable) must be
        retrievable — this is the common shape for metric-only ML runs."""
        client, rid = self._offline_client()
        body = client.get(f"/runs/{rid}/logs?limit=500").json()
        global_logs = [l for l in body["logs"] if l["loggable_id"] == "__global__"]
        assert [l["message"] for l in global_logs] == ["global log B"]

    def test_offline_log_count_in_summary(self) -> None:
        client, rid = self._offline_client()
        summary = client.get(f"/runs/{rid}").json()
        assert summary["log_count"] == 3

    def test_offline_logs_keep_step_for_timeline_filter(self) -> None:
        """`nb.log(msg, step=i)` must round-trip its step through the offline
        REST path so the UI's step-filter (entry.step === clicked metric step)
        can match logs to a clicked chart datapoint after the run finishes.
        """
        from fastapi.testclient import TestClient
        from nebo.server.daemon import DaemonState, create_daemon_app

        state = DaemonState()
        client = TestClient(create_daemon_app(state=state))
        rid = "stepfilter"
        client.post(f"/events?run_id={rid}", json=[
            {"type": "run_start", "run_id": rid, "script_path": "t.py", "args": []},
            {"type": "loggable_register",
             "data": {"loggable_id": "step", "func_name": "step", "kind": "node"}},
        ])
        for i in range(4):
            client.post(f"/events?run_id={rid}", json=[
                {"type": "log", "loggable_id": "step", "message": f"log {i}", "step": i},
                {"type": "metric", "loggable_id": "step", "name": "loss",
                 "metric_type": "line", "value": 1.0 / (i + 1), "step": i},
            ])

        logs = client.get(f"/runs/{rid}/logs?limit=500").json()["logs"]
        assert [(l["message"], l["step"]) for l in logs] == [
            ("log 0", 0), ("log 1", 1), ("log 2", 2), ("log 3", 3),
        ]
        # The metric entries carry the same steps the chart exposes on click.
        series = client.get(f"/runs/{rid}/metrics").json()["metrics"]["step"]["loss"]
        assert [e["step"] for e in series["entries"]] == [0, 1, 2, 3]
        # Simulate the UI step filter: clicking step 2 keeps exactly that log.
        clicked = 2
        assert [l["message"] for l in logs if l["step"] == clicked] == ["log 2"]


class TestMetricBatchIngest:
    """v4 metric_batch frames fan out per the equivalence rule."""

    @staticmethod
    def _batch(n=4, name="loss", lid="a"):
        return {
            "type": "metric_batch",
            "loggable_id": lid,
            "name": name,
            "metric_type": "line",
            "steps": list(range(n)),
            "timestamps": [100.0 + i for i in range(n)],
            "values": [0.5 - 0.1 * i for i in range(n)],
            "tags": ["train"],
        }

    @pytest.mark.asyncio
    async def test_batch_equals_per_point_ingest(self) -> None:
        from nebo.core.coalesce import expand_metric_batch

        batch = self._batch()
        state_a = DaemonState()
        await state_a.ingest_events([batch], run_id="r1")
        state_b = DaemonState()
        await state_b.ingest_events(expand_metric_batch(batch), run_id="r1")

        ma = state_a.run_metrics("r1")["a"]["loss"]
        mb = state_b.run_metrics("r1")["a"]["loss"]
        assert ma == mb
        assert len(ma["entries"]) == 4
        assert state_a.runs["r1"].latest_step == 3
        assert state_a.runs["r1"].resident_points == 4

    @pytest.mark.asyncio
    async def test_batch_respects_snapshot_type_lock(self) -> None:
        state = DaemonState()
        await state.ingest_events([self._batch(2)], run_id="r1")
        series = state.runs["r1"].loggables["a"].metrics["loss"]
        assert series["type"] == "line"

    @pytest.mark.asyncio
    async def test_mismatched_arrays_dropped(self) -> None:
        bad = self._batch()
        bad["values"] = bad["values"][:-1]
        state = DaemonState()
        await state.ingest_events([bad], run_id="r1")
        assert "loss" not in state.runs["r1"].loggables.get(
            "a", LoggableState(loggable_id="a")
        ).metrics

    @pytest.mark.asyncio
    async def test_alert_rule_fires_on_in_batch_point(self) -> None:
        state = DaemonState()
        state.alert_rules["rule1"] = {
            "id": "rule1", "title": "loss spiked", "text": "",
            "level": 30, "triggered_by": "cli",
            "condition": {"metric": "loss", "op": ">", "value": 0.42,
                          "loggable_id": None},
            "run_id": None, "created_at": 0.0, "fired": [],
        }
        await state.ingest_events([self._batch(4)], run_id="r1")
        rule = state.alert_rules["rule1"]
        # values are 0.5, 0.4, 0.3, 0.2 -> only the FIRST point qualifies,
        # and the rule fires exactly once.
        assert len(rule["fired"]) == 1
        assert rule["fired"][0]["step"] == 0
        assert rule["fired"][0]["value"] == 0.5
        assert len(state.runs["r1"].alerts) == 1


class TestNodeExecutedCount:
    @pytest.mark.asyncio
    async def test_count_delta_applied(self) -> None:
        state = DaemonState()
        await state.ingest_events([
            {"type": "loggable_register", "loggable_id": "a",
             "data": {"loggable_id": "a", "kind": "node", "func_name": "a"}},
            {"type": "loggable_register", "loggable_id": "p",
             "data": {"loggable_id": "p", "kind": "node", "func_name": "p"}},
            {"type": "node_executed", "loggable_id": "a",
             "data": {"loggable_id": "a", "caller": "p", "count": 42}},
        ], run_id="r1")
        run = state.runs["r1"]
        assert run.loggables["a"].exec_count == 42
        # The caller edge is still created exactly once.
        assert run.edges == [{"source": "p", "target": "a"}]

    @pytest.mark.asyncio
    async def test_countless_event_still_increments_by_one(self) -> None:
        state = DaemonState()
        await state.ingest_events([
            {"type": "loggable_register", "loggable_id": "a",
             "data": {"loggable_id": "a", "kind": "node", "func_name": "a"}},
            {"type": "node_executed", "loggable_id": "a",
             "data": {"loggable_id": "a"}},
        ], run_id="r1")
        assert state.runs["r1"].loggables["a"].exec_count == 1


class TestMsgpackIngestEndpoint:
    def _client(self):
        from fastapi.testclient import TestClient

        state = DaemonState()
        from nebo.server.daemon import create_daemon_app

        return state, TestClient(create_daemon_app(state))

    def test_msgpack_body_ingests_like_json(self) -> None:
        import msgpack as _msgpack

        raw = b"\x89PNG\r\n\x1a\n" + b"m" * 24
        events = [
            {"type": "metric", "loggable_id": "a", "name": "loss",
             "metric_type": "line", "value": 0.5, "step": 0,
             "tags": [], "timestamp": 1.0},
            {"type": "image", "loggable_id": "a", "name": "f",
             "data": raw, "step": None, "timestamp": 2.0},
        ]
        state, client = self._client()
        body = b"".join(_msgpack.packb(e, use_bin_type=True) for e in events)
        resp = client.post(
            "/events?run_id=r1", content=body,
            headers={"Content-Type": "application/msgpack"},
        )
        assert resp.status_code == 200
        assert resp.json()["count"] == 2
        run = state.runs["r1"]
        assert run.loggables["a"].metrics["loss"]["entries"][0]["value"] == 0.5
        (img,) = run.loggables["a"].images
        assert state.media_bytes("r1", img["media_id"]) == raw

    def test_json_body_still_works(self) -> None:
        state, client = self._client()
        resp = client.post("/events?run_id=r1", json=[
            {"type": "log", "loggable_id": "__global__", "message": "hi",
             "timestamp": 1.0},
        ])
        assert resp.status_code == 200
        assert len(state.runs["r1"].logs) == 1


class TestWsBroadcastQueues:
    @pytest.mark.asyncio
    async def test_slow_client_does_not_block_ingest(self) -> None:
        import asyncio as _asyncio

        from nebo.server.daemon import _WsClient

        class NeverSends:
            async def send_text(self, message: str) -> None:
                await _asyncio.sleep(3600)

        state = DaemonState()
        client = _WsClient(NeverSends())
        client.task = _asyncio.get_event_loop().create_task(client.sender())
        state._ws_clients.append(client)
        try:
            t0 = time.monotonic()
            await state.ingest_events([
                {"type": "log", "loggable_id": "__global__", "message": "hi",
                 "timestamp": 1.0},
            ], run_id="r1")
            elapsed = time.monotonic() - t0
            # Broadcast must be enqueue-only — never awaiting the browser.
            assert elapsed < 1.0
            assert len(state.runs["r1"].logs) == 1
        finally:
            client.task.cancel()

    @pytest.mark.asyncio
    async def test_drop_oldest_at_capacity(self) -> None:
        from nebo.server.daemon import _WsClient

        client = _WsClient(ws=None, maxsize=3)
        for i in range(5):
            client.enqueue(f"m{i}")
        assert client.dropped == 2
        remaining = []
        while not client.queue.empty():
            remaining.append(client.queue.get_nowait())
        assert remaining == ["m2", "m3", "m4"]

    def test_normal_client_receives_batches_in_order(self) -> None:
        import json as _json

        from fastapi.testclient import TestClient

        from nebo.server.daemon import create_daemon_app

        state = DaemonState()
        app = create_daemon_app(state)
        client = TestClient(app)
        with client.websocket_connect("/stream") as ws:
            client.post("/events?run_id=r1", json=[
                {"type": "log", "loggable_id": "__global__", "message": "one",
                 "timestamp": 1.0},
            ])
            client.post("/events?run_id=r1", json=[
                {"type": "log", "loggable_id": "__global__", "message": "two",
                 "timestamp": 2.0},
            ])
            first = _json.loads(ws.receive_text())
            second = _json.loads(ws.receive_text())
        assert first["type"] == "batch" and first["run_id"] == "r1"
        assert first["events"][0]["message"] == "one"
        assert second["events"][0]["message"] == "two"
        # Client fully cleaned up after disconnect.
        assert state._ws_clients == []


class TestLoadNeboFile:
    """`POST /load` — daemon-side load of a server-visible .nebo file."""

    @pytest.mark.asyncio
    async def test_load_forwards_header_metadata(self, tmp_path) -> None:
        """The file body carries no run_start entry — run_name, group and
        started_at live in the header and must survive the load."""
        from nebo.core.fileformat import NeboFileWriter
        from nebo.server.tree import TreeStore

        run_id = "loaded_meta_run"
        nebo_path = tmp_path / "meta.nebo"
        with nebo_path.open("wb") as f:
            writer = NeboFileWriter(
                f, run_id=run_id, script_path="train.py",
                args=["--epochs", "2"], run_name="exp-1",
                group="demos/vision",
            )
            writer._started_at = 1_600_000_000.0
            writer.write_header()
            writer.write_entry("metric", {
                "loggable_id": "__global__", "name": "loss",
                "metric_type": "line", "value": 0.5, "step": 0, "tags": [],
            })
            writer.close()

        state = DaemonState()
        state.tree = TreeStore(tmp_path / "meta_dir")
        await state.load_nebo_file(str(nebo_path))

        run = state.runs[run_id]
        assert run.script_path == "train.py"
        assert run.run_name == "exp-1"
        assert run.args == ["--epochs", "2"]
        assert run.started_at.timestamp() == 1_600_000_000.0
        assert state.tree.to_payload({run_id})["runs"][run_id] == "demos/vision"
        # The body still ingests normally after the synthesized run_start.
        entries = run.loggables["__global__"].metrics["loss"]["entries"]
        assert [e["value"] for e in entries] == [0.5]
