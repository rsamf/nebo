"""Tests for the Loggable abstraction in state.py."""
from collections import deque

from nebo.core.state import (
    LoggableInfo,
    NodeInfo,
    GlobalInfo,
    AgentInfo,
    SessionState,
    get_state,
)


def test_loggable_info_has_shared_fields():
    loggable = LoggableInfo(loggable_id="x", kind="node")
    # `texts` is a bounded ring buffer used by the terminal "Recent
    # texts" panel; it is the only payload-bearing field the SDK keeps
    # on disk-volume buckets after the v3 redesign.
    assert isinstance(loggable.texts, deque)
    assert len(loggable.texts) == 0
    assert loggable.progress is None
    # Metric values, image metadata, and audio metadata are no longer
    # mirrored on the SDK — they go straight to the daemon.
    assert not hasattr(loggable, "metrics")
    assert not hasattr(loggable, "images")
    assert not hasattr(loggable, "audio")


def test_node_info_inherits_loggable_info():
    node = NodeInfo(loggable_id="my_node", name="my_node", func_name="f")
    assert isinstance(node, LoggableInfo)
    assert node.kind == "node"
    assert node.exec_count == 0
    assert node.is_source is True
    assert isinstance(node.texts, deque)  # inherited
    assert len(node.texts) == 0


def test_global_info_has_fixed_kind():
    g = GlobalInfo(loggable_id="__global__")
    assert isinstance(g, LoggableInfo)
    assert g.kind == "global"
    assert g.loggable_id == "__global__"


def test_agent_info_has_fixed_kind():
    a = AgentInfo(loggable_id="__agent__")
    assert isinstance(a, LoggableInfo)
    assert a.kind == "agent"
    assert a.loggable_id == "__agent__"


def test_session_state_seeds_global_on_clear():
    SessionState.reset_singleton()
    state = get_state()
    state.clear_run_state()
    assert "__global__" in state.loggables
    assert state.loggables["__global__"].kind == "global"


def test_session_state_seeds_agent_on_clear():
    SessionState.reset_singleton()
    state = get_state()
    state.clear_run_state()
    assert "__agent__" in state.loggables
    assert state.loggables["__agent__"].kind == "agent"


def test_session_state_reset_seeds_global():
    SessionState.reset_singleton()
    state = get_state()
    assert "__global__" in state.loggables


def test_session_state_reset_seeds_agent():
    SessionState.reset_singleton()
    state = get_state()
    assert "__agent__" in state.loggables
    assert state.loggables["__agent__"].kind == "agent"


def test_emit_after_reset_stays_in_file_mode(monkeypatch):
    """SessionState.reset() clears _pending_mode; the next auto-run must
    re-resolve to file mode, not fall into the network branch and probe
    localhost:7861 (2s stall when another process squats on the port)."""
    monkeypatch.setenv("NEBO_NO_STORE", "1")
    monkeypatch.delenv("NEBO_URI", raising=False)
    import nebo as nb
    from nebo.core.client import NetworkTransport

    nb.get_state().reset()
    nb.log_text("text", "after reset")
    state = nb.get_state()
    assert state._mode != "network" or state._pending_mode is not None
    assert not isinstance(state._transport, NetworkTransport)


class TestReturnOriginMemory:
    def test_weakrefable_returns_not_pinned(self):
        import gc
        import weakref

        import numpy as np

        from nebo.core.state import SessionState

        SessionState.reset_singleton()
        state = SessionState()
        value = np.zeros(1000)
        ref = weakref.ref(value)
        state.track_return("producer", value)
        del value
        gc.collect()
        # The tracker must not keep the array alive.
        assert ref() is None
        # And a dead entry is a safe miss, not a crash.
        probe = np.ones(3)
        assert state.find_producers((probe,), {}, parent=None) == set()

    def test_weakrefable_flow_still_infers(self):
        import numpy as np

        from nebo.core.state import SessionState

        SessionState.reset_singleton()
        state = SessionState()
        state._node_parents["producer"] = None
        value = np.zeros(10)
        state.track_return("producer", value)
        assert state.find_producers((value,), {}, parent=None) == {"producer"}

    def test_non_weakrefable_returns_still_work(self):
        from nebo.core.state import SessionState

        SessionState.reset_singleton()
        state = SessionState()
        state._node_parents["producer"] = None
        value = [1, 2, 3]  # lists don't support weakrefs
        state.track_return("producer", value)
        assert state.find_producers((value,), {}, parent=None) == {"producer"}

    def test_strong_fallback_is_bounded(self):
        from nebo.core.state import RETURN_ORIGINS_MAX, SessionState

        SessionState.reset_singleton()
        state = SessionState()
        state._node_parents["p"] = None
        keep = []
        for i in range(RETURN_ORIGINS_MAX + 100):
            v = [i]
            keep.append(v)
            state.track_return("p", v)
        # Oldest strong entries were evicted; newest still resolve.
        assert state.find_producers((keep[0],), {}, parent=None) == set()
        assert state.find_producers((keep[-1],), {}, parent=None) == {"p"}
