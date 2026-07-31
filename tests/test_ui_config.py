"""Tests for nb.ui() and @nb.fn(ui={...})."""

import pytest
from nebo.core.state import SessionState
import nebo as nb


@pytest.fixture
def reset_state():
    SessionState.reset_singleton()
    nb._auto_init_done = False
    nb.init()
    yield
    SessionState.reset_singleton()


def test_ui_outside_run_writes_script_template(reset_state):
    """nb.ui() with no live run is declarative: script-level template,
    no run materialized."""
    from nebo.core.state import get_state

    nb.ui(layout="horizontal", view="dag", minimap=True, theme="dark")

    state = get_state()
    assert state.ui_config is None
    assert state._run_materialized is False
    assert state._script_ui_config == {
        "layout": "horizontal", "view": "dag", "minimap": True,
        "theme": "dark",
    }


def test_ui_outside_run_overwrites_template(reset_state):
    """Repeat nb.ui() overwrites the template, matching live-run semantics."""
    from nebo.core.state import get_state

    nb.ui(layout="horizontal")
    nb.ui(layout="vertical")

    assert get_state()._script_ui_config == {"layout": "vertical"}


def test_ui_inside_live_run_stores_and_emits(capturing_client):
    """nb.ui() with a live run keeps today's behavior: state + wire event."""
    from nebo.core.state import get_state

    nb.log_text("text", "materialize")
    nb.ui(layout="horizontal", theme="dark")

    state = get_state()
    assert state.ui_config == {"layout": "horizontal", "theme": "dark"}
    ui_events = [e for e in capturing_client.events if e.get("type") == "ui_config"]
    assert len(ui_events) == 1
    assert ui_events[0]["data"] == {"layout": "horizontal", "theme": "dark"}


def test_fn_ui_parameter(reset_state):
    """@nb.fn(ui={...}) stores per-node UI hints."""
    from nebo.core.state import get_state

    @nb.fn(ui={"color": "#34d399"})
    def my_func():
        nb.log_text("text", "hello")

    my_func()

    state = get_state()
    node_id = next(nid for nid in state.loggables if "my_func" in nid)
    assert state.loggables[node_id].ui_hints == {"color": "#34d399"}


class TestFnUiTypeValidation:
    """`@nb.fn(ui=...)` must reject non-dict values at decoration time.

    Regression: a literal-set typo (`ui={"default_tab", "metrics"}`)
    silently produced a `loggable_register` event whose `ui_hints` was
    a Python set — un-encodable by `json.dumps` and, before the
    poison-batch fix, enough to drop every later event in the run.
    """

    def test_set_literal_raises(self, reset_state):
        with pytest.raises(TypeError, match="dict"):
            @nb.fn(ui={"default_tab", "metrics"})  # type: ignore[arg-type]
            def step():
                pass

    def test_set_literal_message_mentions_colon_hint(self, reset_state):
        with pytest.raises(TypeError) as excinfo:
            @nb.fn(ui={"default_tab", "metrics"})  # type: ignore[arg-type]
            def step():
                pass
        assert "colon" in str(excinfo.value)

    def test_list_raises(self, reset_state):
        with pytest.raises(TypeError, match="dict"):
            @nb.fn(ui=["default_tab"])  # type: ignore[arg-type]
            def step():
                pass

    def test_string_raises(self, reset_state):
        with pytest.raises(TypeError, match="dict"):
            @nb.fn(ui="default_tab")  # type: ignore[arg-type]
            def step():
                pass

    def test_none_is_accepted(self, reset_state):
        @nb.fn(ui=None)
        def step():
            return 1
        assert step() == 1

    def test_dict_is_accepted(self, reset_state):
        @nb.fn(ui={"default_tab": "metrics"})
        def step():
            return 2
        assert step() == 2

    def test_empty_dict_is_accepted(self, reset_state):
        @nb.fn(ui={})
        def step():
            return 3
        assert step() == 3
