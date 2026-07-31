"""Tests for class decoration with @nb.fn()."""

import warnings
import pytest
from nebo.core.state import SessionState


@pytest.fixture
def reset_state():
    SessionState.reset_singleton()
    import nebo as nb
    nb._auto_init_done = False
    nb.init()
    yield
    SessionState.reset_singleton()


def test_class_decoration_wraps_methods(reset_state):
    """Decorating a class wraps all methods with scope tracking."""
    import nebo as nb
    from nebo.core.state import get_state

    @nb.fn()
    class MyProcessor:
        def process(self):
            nb.log_text("text", "processing")

        def finalize(self):
            nb.log_text("text", "finalizing")

    p = MyProcessor()
    p.process()
    p.finalize()

    state = get_state()
    assert "MyProcessor.process" in state.loggables
    assert "MyProcessor.finalize" in state.loggables
    assert state.loggables["MyProcessor.process"].materialized
    assert state.loggables["MyProcessor.finalize"].materialized


def test_class_group_field(reset_state):
    """Methods in a decorated class have the group field set."""
    import nebo as nb
    from nebo.core.state import get_state

    @nb.fn()
    class MyAgent:
        def think(self):
            nb.log_text("text", "thinking")

    agent = MyAgent()
    agent.think()

    state = get_state()
    node = state.loggables["MyAgent.think"]
    assert node.group == "MyAgent"


def test_class_methods_materialize_on_execution(reset_state):
    """All executed methods of a decorated class materialize, even silent ones.

    Silent methods still need to appear in the graph so dependency chains
    aren't broken when a caller method doesn't itself call nb.log_text.
    """
    import nebo as nb
    from nebo.core.state import get_state

    @nb.fn()
    class MyClass:
        def logs(self):
            nb.log_text("text", "visible")

        def silent(self):
            return 42

    obj = MyClass()
    obj.logs()
    obj.silent()

    state = get_state()
    assert state.loggables["MyClass.logs"].materialized
    assert state.loggables["MyClass.silent"].materialized


def test_redundant_decorator_warning(reset_state):
    """@nb.fn() on a method inside a decorated class issues a warning."""
    import nebo as nb

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")

        @nb.fn()
        class MyClass:
            @nb.fn()
            def my_method(self):
                nb.log_text("text", "hello")

        assert len(w) == 1
        assert "redundant" in str(w[0].message).lower()


def test_decorated_method_in_undecorated_class(reset_state):
    """@nb.fn() on a method in an undecorated class is a standalone node."""
    import nebo as nb
    from nebo.core.state import get_state

    class MyClass:
        @nb.fn()
        def my_method(self):
            nb.log_text("text", "standalone")

    obj = MyClass()
    obj.my_method()

    state = get_state()
    node_id = next(nid for nid in state.loggables if "my_method" in nid)
    assert state.loggables[node_id].materialized
    assert state.loggables[node_id].group is None


def test_called_fn_inside_class_group(reset_state):
    """A standalone @nb.fn() function called from a decorated class
    appears inside the class group."""
    import nebo as nb
    from nebo.core.state import get_state

    @nb.fn()
    def helper():
        nb.log_text("text", "helping")

    @nb.fn()
    class MyClass:
        def run(self):
            nb.log_text("text", "running")
            helper()

    obj = MyClass()
    obj.run()

    state = get_state()
    # helper was called within MyClass context, so it should be in the group
    node_id = next(nid for nid in state.loggables if "helper" in nid)
    assert state.loggables[node_id].group == "MyClass"


def test_dunder_methods_are_wrapped(reset_state):
    """Dunder methods (__init__, __call__, etc.) should be wrapped with scope tracking."""
    import nebo as nb
    from nebo.core.state import get_state

    @nb.fn()
    class MyCallable:
        def __init__(self):
            nb.log_text("text", "initializing")

        def __call__(self, x):
            nb.log_text("text", f"called with {x}")
            return x * 2

    obj = MyCallable()
    result = obj(5)

    state = get_state()
    assert "MyCallable.__init__" in state.loggables
    assert state.loggables["MyCallable.__init__"].materialized
    assert "MyCallable.__call__" in state.loggables
    assert state.loggables["MyCallable.__call__"].materialized
    assert result == 10
