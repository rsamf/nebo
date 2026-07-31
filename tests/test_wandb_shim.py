"""Tests for the nebo.wandb drop-in shim."""

from __future__ import annotations

import nebo as nb
import nebo.wandb as wandb
from nebo.core.state import SessionState, NodeInfo, get_state


def _reset_state() -> None:
    SessionState.reset_singleton()
    # Reset the wandb shim's module-level proxy state so tests don't leak.
    wandb.run.id = None
    wandb.run.name = None
    wandb.run.project = None
    wandb.config._data.clear()


class TestWandbInit:
    def setup_method(self) -> None:
        _reset_state()

    def test_init_starts_run_and_populates_run_proxy(self) -> None:
        wandb.init(project="myproj", name="run-1")
        assert wandb.run.id is not None
        assert wandb.run.name == "run-1"
        assert wandb.run.project == "myproj"

    def test_init_populates_config_proxy(self) -> None:
        wandb.init(name="run", config={"lr": 0.001, "batch_size": 32})
        assert wandb.config.lr == 0.001
        assert wandb.config["batch_size"] == 32
        assert wandb.config.get("missing", 99) == 99


class TestWandbLog:
    """The wandb shim dispatches to nb.log_line under the hood, so its
    asserts read off the SDK's wire output via a CapturingClient
    rather than the now-removed local metrics mirror."""

    def setup_method(self) -> None:
        _reset_state()

    def _attach_client(self):
        from tests.conftest import CapturingClient
        wandb.init(name="t")
        client = CapturingClient()
        get_state()._transport = client
        return client

    def test_log_numeric_scalars_become_metrics(self) -> None:
        client = self._attach_client()
        wandb.log({"loss": 0.5, "epoch": 1})
        loss_events = client.metrics_named("loss")
        epoch_events = client.metrics_named("epoch")
        assert loss_events and loss_events[-1]["value"] == 0.5
        assert epoch_events and epoch_events[-1]["value"] == 1

    def test_log_bool_becomes_int_metric(self) -> None:
        client = self._attach_client()
        wandb.log({"converged": True})
        events = client.metrics_named("converged")
        assert events[-1]["value"] == 1

    def test_log_step_threads_through(self) -> None:
        client = self._attach_client()
        wandb.log({"loss": 0.4}, step=7)
        events = client.metrics_named("loss")
        assert events[-1]["step"] == 7

    def test_log_string_value_falls_back_to_log_text(self) -> None:
        client = self._attach_client()
        wandb.log({"note": "hello world"}, step=2)
        # Non-numeric, non-image values fall through to nb.log_text(k, str(v)),
        # so the key becomes the stream name and the value the message. The
        # entry also lands on the bounded `texts` deque on the global loggable.
        (event,) = client.by_type("text")
        assert event["name"] == "note"
        assert event["message"] == "hello world"
        assert event["step"] == 2
        global_loggable = get_state().loggables["__global__"]
        assert any(
            entry.get("name") == "note" and entry.get("message") == "hello world"
            for entry in list(global_loggable.texts)
        )


class TestWandbConfig:
    def setup_method(self) -> None:
        _reset_state()
        wandb.init(name="t")

    def test_setattr_writes_through_to_nebo(self) -> None:
        wandb.config.lr = 0.05
        # The proxy stores the value locally — log_cfg also dispatches
        # outwards but only Node loggables receive params, so we just verify
        # the proxy round-trips the value.
        assert wandb.config.lr == 0.05

    def test_setattr_inside_fn_writes_to_node_params(self) -> None:
        @nb.fn()
        def setup():
            wandb.config.lr = 0.001
            return None

        setup()
        # Locate the node and check its params got the config write.
        node = next(
            l for l in get_state().loggables.values()
            if isinstance(l, NodeInfo) and l.func_name == "setup"
        )
        assert node.params.get("lr") == 0.001

    def test_update_method(self) -> None:
        wandb.config.update({"a": 1, "b": 2})
        assert wandb.config.a == 1
        assert wandb.config["b"] == 2


class TestWandbFinish:
    def setup_method(self) -> None:
        _reset_state()

    def test_finish_no_op(self) -> None:
        wandb.init(name="t")
        wandb.finish()
        # Nothing to assert; just verify no exception.
