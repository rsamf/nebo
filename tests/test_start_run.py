"""Tests for nb.start_run() multi-run support."""

from __future__ import annotations

import pytest

import nebo as nb
from nebo.core.state import NodeInfo, SessionState, get_state, _RunSnapshot
from nebo.core.decorators import fn


def _reset() -> None:
    """Reset nebo state and force local mode."""
    SessionState.reset_singleton()
    nb._auto_init_done = False
    nb.init()


def _node_count(state: SessionState) -> int:
    """Count only node-kind loggables (excludes the seeded __global__)."""
    return sum(1 for l in state.loggables.values() if isinstance(l, NodeInfo))


# ---------------------------------------------------------------------------
# Basic API
# ---------------------------------------------------------------------------


class TestStartRunAPI:
    """Tests for the start_run public API."""

    def setup_method(self) -> None:
        _reset()

    def test_start_run_is_importable(self) -> None:
        """start_run should be accessible on the nebo module."""
        assert hasattr(nb, "start_run")
        assert callable(nb.start_run)

    def test_start_run_in_all(self) -> None:
        """start_run should be listed in __all__."""
        assert "start_run" in nb.__all__

    def test_returns_run_context(self) -> None:
        """start_run() should return a _RunContext with a run_id attribute."""
        ctx = nb.start_run(name="test")
        assert hasattr(ctx, "run_id")
        assert isinstance(ctx.run_id, str)
        assert len(ctx.run_id) == 12

    def test_context_manager_protocol(self) -> None:
        """_RunContext should work as a context manager."""
        with nb.start_run(name="ctx") as run:
            assert hasattr(run, "run_id")
            assert isinstance(run.run_id, str)

    def test_context_exposes_name_and_config(self) -> None:
        """_RunContext should store the name and config passed in."""
        with nb.start_run(name="exp-1", config={"lr": 0.01}) as run:
            assert run.name == "exp-1"
            assert run.config == {"lr": 0.01}


# ---------------------------------------------------------------------------
# Run ID generation
# ---------------------------------------------------------------------------


class TestRunIDGeneration:
    """Tests for run_id uniqueness and format."""

    def setup_method(self) -> None:
        _reset()

    def test_unique_ids_per_run(self) -> None:
        """Each start_run call should produce a unique run_id."""
        ids = []
        for i in range(5):
            with nb.start_run(name=f"run-{i}") as run:
                ids.append(run.run_id)
        assert len(set(ids)) == 5

    def test_run_id_is_12_char_hex(self) -> None:
        """run_id should be a 12-character hex string."""
        with nb.start_run() as run:
            assert len(run.run_id) == 12
            int(run.run_id, 16)  # should not raise


# ---------------------------------------------------------------------------
# State isolation between runs
# ---------------------------------------------------------------------------


class TestStateIsolation:
    """Tests for state reset between separate runs."""

    def setup_method(self) -> None:
        _reset()

    def test_nodes_cleared_for_new_run(self) -> None:
        """A new run should start with empty nodes."""
        state = get_state()

        with nb.start_run(name="first"):
            @fn()
            def step_a():
                nb.log_text("text", "a")
            step_a()
            assert _node_count(state) > 0

        with nb.start_run(name="second"):
            # Fresh run — no nodes carried over
            assert _node_count(state) == 0

    def test_nodes_register_in_subsequent_runs(self) -> None:
        """Decorated functions must re-register nodes in each new start_run()."""
        state = get_state()

        @fn()
        def my_step():
            nb.log_text("text", "working")

        # Run 1: node should appear
        with nb.start_run(name="run-1"):
            my_step()
            assert "my_step" in state.loggables or any(
                isinstance(l, NodeInfo) and l.func_name == "my_step"
                for l in state.loggables.values()
            )

        # Run 2: same function, fresh state — node must re-register
        with nb.start_run(name="run-2"):
            my_step()
            assert _node_count(state) > 0, "Node must re-register in new run"
            node = next(
                l for l in state.loggables.values()
                if isinstance(l, NodeInfo) and l.func_name == "my_step"
            )
            assert node.materialized, "Node must be materialized"

    def test_edges_cleared_for_new_run(self) -> None:
        """A new run should start with empty edges."""
        state = get_state()

        with nb.start_run(name="first"):
            @fn()
            def parent():
                child()
            @fn()
            def child():
                nb.log_text("text", "c")
            parent()
            assert len(state.edges) > 0

        with nb.start_run(name="second"):
            assert len(state.edges) == 0

    def test_description_cleared_for_new_run(self) -> None:
        """Workflow description should reset between runs."""
        state = get_state()

        with nb.start_run(name="first"):
            nb.md("First pipeline")
            assert state.workflow_description is not None

        with nb.start_run(name="second"):
            assert state.workflow_description is None


# ---------------------------------------------------------------------------
# Resume / interleave
# ---------------------------------------------------------------------------


class TestResume:
    """Tests for run resume via run_id."""

    def setup_method(self) -> None:
        _reset()

    def test_resume_preserves_run_id(self) -> None:
        """Resuming with run_id should return the same run_id."""
        with nb.start_run(name="A") as run_a:
            original_id = run_a.run_id

        with nb.start_run(run_id=original_id) as resumed:
            assert resumed.run_id == original_id

    def test_resume_restores_nodes(self) -> None:
        """Resuming a run should restore its nodes."""
        state = get_state()
        run_a_id = None

        with nb.start_run(name="A") as run:
            run_a_id = run.run_id
            @fn()
            def step_a():
                nb.log_text("text", "hello from A")
            step_a()
            node_count_a = _node_count(state)
            assert node_count_a > 0

        # Start a different run
        with nb.start_run(name="B"):
            @fn()
            def step_b():
                nb.log_text("text", "hello from B")
            step_b()
            assert _node_count(state) > 0

        # Resume run A
        with nb.start_run(run_id=run_a_id):
            assert _node_count(state) == node_count_a

    def test_resume_restores_description(self) -> None:
        """Resuming should restore the workflow description."""
        state = get_state()

        with nb.start_run(name="A") as run:
            nb.md("Pipeline A description")
            run_a_id = run.run_id

        with nb.start_run(name="B"):
            nb.md("Pipeline B description")

        with nb.start_run(run_id=run_a_id):
            assert state.workflow_description == "Pipeline A description"

    def test_interleave_pattern(self) -> None:
        """The interleave pattern (A, B, A, B) should work correctly."""
        state = get_state()
        id_a = id_b = None

        for i in range(3):
            with nb.start_run(name="A", run_id=id_a) as run:
                id_a = run.run_id
                @fn()
                def iter_a():
                    nb.log_text("text", f"A iteration {i}")
                iter_a()

            with nb.start_run(name="B", run_id=id_b) as run:
                id_b = run.run_id
                @fn()
                def iter_b():
                    nb.log_text("text", f"B iteration {i}")
                iter_b()

        assert id_a is not None
        assert id_b is not None
        assert id_a != id_b

    def test_unknown_run_id_creates_new_run(self) -> None:
        """Passing an unknown run_id should create a new run with that id."""
        state = get_state()
        custom_id = "customid1234"

        with nb.start_run(run_id=custom_id) as run:
            assert run.run_id == custom_id
            assert _node_count(state) == 0  # fresh state


# ---------------------------------------------------------------------------
# Context manager exception handling
# ---------------------------------------------------------------------------


class TestExceptionHandling:
    """Tests for context manager behavior on exceptions."""

    def setup_method(self) -> None:
        _reset()

    def test_exception_propagates(self) -> None:
        """Exceptions inside the context should propagate normally."""
        with pytest.raises(ValueError, match="boom"):
            with nb.start_run(name="crasher"):
                raise ValueError("boom")

    def test_new_run_after_exception(self) -> None:
        """Should be able to start a new run after a crash."""
        try:
            with nb.start_run(name="crasher"):
                raise ValueError("boom")
        except ValueError:
            pass

        with nb.start_run(name="recovery") as run:
            assert run.run_id is not None
            assert len(run.run_id) == 12

    def test_resume_after_exception(self) -> None:
        """Should be able to resume a run that exited via exception."""
        run_id = None
        try:
            with nb.start_run(name="crasher") as run:
                run_id = run.run_id
                nb.md("before crash")
                raise ValueError("boom")
        except ValueError:
            pass

        state = get_state()
        with nb.start_run(run_id=run_id):
            assert state.workflow_description == "before crash"


# ---------------------------------------------------------------------------
# Plain function usage (no context manager)
# ---------------------------------------------------------------------------


class TestPlainFunction:
    """Tests for using start_run without context manager."""

    def setup_method(self) -> None:
        _reset()

    def test_plain_function_sets_active_run(self) -> None:
        """Calling start_run as a plain function should set the active run."""
        state = get_state()
        ctx = nb.start_run(name="plain")
        assert state._active_run_id == ctx.run_id

    def test_next_start_run_completes_previous(self) -> None:
        """Calling start_run again should implicitly complete the previous run."""
        state = get_state()
        ctx1 = nb.start_run(name="first")
        id1 = ctx1.run_id

        ctx2 = nb.start_run(name="second")
        # The first run should have been saved
        assert id1 in state._run_snapshots


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


class TestConfigResolution:
    """Tests for _resolve_config helper."""

    def test_dict_passthrough(self) -> None:
        """Plain dicts should pass through unchanged."""
        from nebo import _resolve_config
        result = _resolve_config({"lr": 0.01, "epochs": 10})
        assert result == {"lr": 0.01, "epochs": 10}

    def test_none_returns_empty(self) -> None:
        """None should return empty dict."""
        from nebo import _resolve_config
        assert _resolve_config(None) == {}

    def test_nested_dict(self) -> None:
        """Nested dicts should pass through."""
        from nebo import _resolve_config
        cfg = {"model": {"name": "resnet", "layers": 50}, "lr": 0.01}
        result = _resolve_config(cfg)
        assert result == cfg


# ---------------------------------------------------------------------------
# SessionState snapshot infrastructure
# ---------------------------------------------------------------------------


class TestSessionStateSnapshots:
    """Tests for save/restore/clear on SessionState."""

    def setup_method(self) -> None:
        _reset()

    def test_save_and_restore(self) -> None:
        """save_run_state + restore_run_state should round-trip."""
        state = get_state()

        @fn()
        def my_node():
            nb.log_text("text", "hello")
        my_node()

        state.save_run_state("test-run")
        saved_node_count = _node_count(state)
        assert saved_node_count > 0

        state.clear_run_state()
        assert _node_count(state) == 0

        state.restore_run_state("test-run")
        assert _node_count(state) == saved_node_count

    def test_restore_unknown_id_clears(self) -> None:
        """Restoring an unknown run_id should clear state."""
        state = get_state()

        @fn()
        def my_node():
            nb.log_text("text", "hello")
        my_node()
        assert _node_count(state) > 0

        state.restore_run_state("nonexistent")
        assert _node_count(state) == 0

    def test_clear_run_state(self) -> None:
        """clear_run_state should reset all per-run fields."""
        state = get_state()
        nb.log_text("text", "materialize")  # md outside a live run is declarative
        nb.md("test description")
        assert state.workflow_description is not None

        state.clear_run_state()
        assert state.workflow_description is None
        assert _node_count(state) == 0
        assert len(state.edges) == 0

    def test_snapshots_are_independent(self) -> None:
        """Modifying state after save should not affect the snapshot."""
        state = get_state()

        @fn()
        def node_a():
            nb.log_text("text", "a")
        node_a()
        state.save_run_state("snap-1")
        count_1 = _node_count(state)

        @fn()
        def node_b():
            nb.log_text("text", "b")
        node_b()
        assert _node_count(state) > count_1

        state.restore_run_state("snap-1")
        assert _node_count(state) == count_1

    def test_reset_clears_snapshots(self) -> None:
        """SessionState.reset() should clear all snapshots."""
        state = get_state()
        state.save_run_state("test")
        assert len(state._run_snapshots) > 0

        state.reset()
        assert len(state._run_snapshots) == 0
        assert state._active_run_id is None


# ---------------------------------------------------------------------------
# DaemonClient guard
# ---------------------------------------------------------------------------


class TestClientRunCompletedGuard:
    """Tests for the _run_completed guard on DaemonClient."""

    def test_guard_field_exists(self) -> None:
        """DaemonClient should have a _run_completed field."""
        from nebo.core.client import NetworkTransport as DaemonClient
        client = DaemonClient()
        assert hasattr(client, "_run_completed")
        assert client._run_completed is False


# ---------------------------------------------------------------------------
# Daemon-side: Run fields and event processing
# ---------------------------------------------------------------------------


class TestDaemonRunFields:
    """Tests for run_name and run_config on daemon Run."""

    def test_run_has_new_fields(self) -> None:
        """Run dataclass should have run_name and run_config."""
        from nebo.server.daemon import DaemonState
        state = DaemonState()
        run = state.create_run("test.py", [], "test-run")
        assert hasattr(run, "run_name")
        assert hasattr(run, "run_config")
        assert run.run_name is None
        assert run.run_config == {}

    def test_get_summary_includes_run_name(self) -> None:
        """get_summary() should include run_name."""
        from nebo.server.daemon import DaemonState
        state = DaemonState()
        run = state.create_run("test.py", [], "test-run")
        run.run_name = "my-experiment"
        summary = run.get_summary()
        assert summary["run_name"] == "my-experiment"

    def test_get_graph_includes_run_config(self) -> None:
        """get_graph() should include run_config."""
        from nebo.server.daemon import DaemonState
        state = DaemonState()
        run = state.create_run("test.py", [], "test-run")
        run.run_config = {"lr": 0.01, "batch_size": 32}
        graph = run.get_graph()
        assert graph["run_config"] == {"lr": 0.01, "batch_size": 32}


class TestDaemonEventProcessing:
    """Tests for run_config and run_name event processing."""

    def test_run_config_event(self) -> None:
        """run_config event should store config on the run."""
        import asyncio
        from nebo.server.daemon import DaemonState
        state = DaemonState()
        state.create_run("test.py", [], "test-run")
        asyncio.run(state.ingest_events([
            {"type": "run_config", "data": {"lr": 0.01, "epochs": 100}},
        ], run_id="test-run"))
        assert state.runs["test-run"].run_config == {"lr": 0.01, "epochs": 100}

    def test_run_name_in_run_start(self) -> None:
        """run_start event with run_name should store it on the run."""
        import asyncio
        from nebo.server.daemon import DaemonState
        state = DaemonState()
        state.create_run("test.py", [], "test-run")
        asyncio.run(state.ingest_events([
            {"type": "run_start", "data": {"script_path": "test.py", "run_name": "experiment-1"}},
        ], run_id="test-run"))
        assert state.runs["test-run"].run_name == "experiment-1"

    def test_run_start_reactivates_completed_run(self) -> None:
        """run_start on a completed run should make it active again (resume)."""
        import asyncio
        from nebo.server.daemon import DaemonState
        state = DaemonState()
        state.create_run("test.py", [], "test-run")
        # Complete the run
        asyncio.run(state.ingest_events([
            {"type": "run_completed", "data": {}},
        ], run_id="test-run"))
        assert state.active_run_id is None

        # Resume via run_start
        asyncio.run(state.ingest_events([
            {"type": "run_start", "data": {"script_path": "test.py"}},
        ], run_id="test-run"))
        assert state.active_run_id == "test-run"


# ---------------------------------------------------------------------------
# File format
# ---------------------------------------------------------------------------


class TestFileFormat:
    """Tests for the run_config entry type in the file format."""

    def test_run_config_entry_type_exists(self) -> None:
        """ENTRY_TYPES should include run_config."""
        from nebo.core.fileformat import ENTRY_TYPES
        assert "run_config" in ENTRY_TYPES
        assert isinstance(ENTRY_TYPES["run_config"], int)
        assert ENTRY_TYPES["run_config"] == 18
