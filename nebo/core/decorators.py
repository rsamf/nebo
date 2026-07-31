"""The @fn decorator for nebo."""

from __future__ import annotations

import functools
import inspect
import warnings
from typing import Any, Callable, Optional, TypeVar, overload

from nebo.core.state import NodeInfo, _current_node, _current_group, get_state

F = TypeVar("F", bound=Callable[..., Any])

# Decoration-time identity per node_id: (module, qualname). Loggable ids
# must be unique — two *different* functions decorating to the same id
# (e.g. top-level `step()` in two modules both qualname "step") would
# silently merge into one node with interleaved streams and conflated
# edges. The newcomer gets module-qualified instead; pathological
# same-module duplicates fall back to #N suffixes. Process-level and
# deliberately not reset by nb.reset(): re-decorating the same function
# is idempotent, and ids must stay stable within a process.
_node_identities: dict[str, tuple[str, str]] = {}


def _resolve_node_id(f: Callable[..., Any], base_id: str) -> str:
    """Return a unique node_id for ``f``, module-qualifying on collision."""
    ident = (getattr(f, "__module__", "") or "", f.__qualname__)
    claimed = _node_identities.get(base_id)
    if claimed is None or claimed == ident:
        _node_identities[base_id] = ident
        return base_id
    qualified = f"{ident[0]}.{base_id}" if ident[0] else f"{base_id}#2"
    candidate = qualified
    n = 2
    while True:
        claimed = _node_identities.get(candidate)
        if claimed is None or claimed == ident:
            break
        n += 1
        candidate = f"{qualified}#{n}"
    already_qualified = _node_identities.get(candidate) == ident
    _node_identities[candidate] = ident
    if not already_qualified:
        warnings.warn(
            f"@nb.fn: node id '{base_id}' is already used by a different "
            f"function; registering this one as '{candidate}'. Rename one "
            "of the functions to silence this.",
            stacklevel=4,
        )
    return candidate


@overload
def fn(func: F) -> F: ...


@overload
def fn(
    depends_on: Optional[list[Any]] = None,
    ui: Optional[dict] = None,
) -> Callable[[F], F]: ...


def fn(
    func: Optional[Any] = None,
    depends_on: Optional[list[Any]] = None,
    ui: Optional[dict] = None,
) -> Any:
    """Decorator that registers a function or class as a DAG node.

    The node materializes (appears in the graph) as soon as the
    decorated function is executed for the first time — a call to
    ``nb.log*`` is **not** required. This keeps dependency chains
    intact when an intermediate function only orchestrates calls to
    other nodes without logging anything itself.

    Can be used as::

        @nb.fn
        @nb.fn()
        @nb.fn(depends_on=[other_fn])

    When applied to a class, all methods are wrapped with scope tracking
    and the class name becomes a visual group container.

    DAG edges are inferred automatically from data flow between
    **sibling** nodes (nodes sharing the same parent/caller). An object
    creates one edge per hop—if node X produces a value that flows
    through node Y to node A, the edges are X->Y and Y->A, never X->A.
    When no sibling data-flow is detected, a parent edge is used.

    For dependencies that cannot be detected automatically (shared mutable
    state, class attributes, globals), use ``depends_on``::

        @nb.fn(depends_on=[foo])
        def bar(self):
            # bar depends on foo via shared state, not via arguments
            ...

    Args:
        func: The function or class to decorate (when used without parentheses).
        depends_on: Optional list of decorated functions or node ID strings
            that this node depends on. Creates explicit edges.
        ui: Optional per-node display hints surfaced to the web UI as
            ``ui_hints``. Supported keys:

            - ``collapsed`` (bool): if True, the node's card is collapsed
              in the flat view on first render.
            - ``color`` (str): badge / border accent color for the node.
            - ``default_tab`` (str): which tab opens by default when the
              card is expanded. One of ``"info"`` (default), ``"logs"``,
              ``"metrics"``, ``"images"``, ``"audio"``. User clicks on a
              different tab always override this preference.

            Unknown keys are forwarded verbatim for forward-compatibility
            with future UI features.

            Example::

                @nb.fn(ui={"default_tab": "metrics", "collapsed": False})
                def train(...): ...

    Returns:
        The decorated function or class.
    """
    # Reject non-dict `ui` at decoration time. The most common form of
    # this typo — `ui={"default_tab", "metrics"}` (a set, no colon) —
    # otherwise reaches the wire as an un-encodable `loggable_register`
    # event and silently drops every later event in the run.
    if ui is not None and not isinstance(ui, dict):
        raise TypeError(
            f"@nb.fn(ui=...) expects a dict, got {type(ui).__name__}. "
            f'Did you mean ui={{"default_tab": "metrics"}} (note the colon)?'
        )

    def decorator(f):
        if inspect.isclass(f):
            return _decorate_class(f, depends_on)
        return _decorate_function(f, depends_on, ui_hints=ui)

    # Handle @fn, @fn()
    if func is None:
        return decorator
    if callable(func):
        return decorator(func)
    raise TypeError(
        f"fn() got an unexpected positional argument {func!r}. "
        "Use @nb.fn or @nb.fn() instead."
    )


def _decorate_class(cls, depends_on):
    """Wrap all methods of a class with scope tracking."""
    class_name = cls.__name__

    for attr_name in list(vars(cls)):
        attr = getattr(cls, attr_name)
        if not callable(attr) or isinstance(attr, type):
            continue

        # Check for redundant @nb.fn() on methods
        if hasattr(attr, "_nb_decorated"):
            warnings.warn(
                f"@nb.fn() on method '{class_name}.{attr_name}' is redundant — "
                f"the class '{class_name}' is already decorated.",
                stacklevel=2,
            )
            original = attr._nb_original
            wrapped = _decorate_function(
                original, depends_on=None, group=class_name,
            )
            setattr(cls, attr_name, wrapped)
        else:
            wrapped = _decorate_function(
                attr, depends_on=None, group=class_name,
            )
            setattr(cls, attr_name, wrapped)

    if depends_on:
        cls._nb_depends_on = depends_on

    return cls


def _decorate_function(f, depends_on, group=None, ui_hints=None):
    """Wrap a single function with scope tracking."""
    # Use ClassName.method_name for methods in decorated classes
    if group:
        base_id = f"{group}.{f.__name__}"
    else:
        base_id = f.__qualname__
    node_id = _resolve_node_id(f, base_id)
    registered = False

    # Resolve depends_on to node ID strings at decoration time
    depends_on_ids: list[str] = []
    if depends_on:
        for dep in depends_on:
            if callable(dep):
                depends_on_ids.append(dep.__qualname__)
            else:
                depends_on_ids.append(str(dep))

    @functools.wraps(f)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        nonlocal registered
        # Auto-init on first execution. Routes through `_ensure_run`
        # so the first `@nb.fn`-decorated call also materializes the
        # implicit run (open transport + emit run_start).
        try:
            from nebo import _ensure_run
            _ensure_run()
        except ImportError:
            pass
        state = get_state()

        # Determine group: use explicit group, or inherit from _current_group
        effective_group = group or _current_group.get()

        # Register node on first execution (not at import time)
        existing_loggable = state.loggables.get(node_id)
        if not registered or not isinstance(existing_loggable, NodeInfo):
            state.register_node(
                node_id=node_id,
                func_name=f.__name__,
                docstring=f.__doc__,
                group=effective_group,
                ui_hints=ui_hints,
            )
            registered = True
        elif effective_group:
            # Update group if this function is being called within a class context
            node = state.loggables.get(node_id)
            if isinstance(node, NodeInfo) and node.group is None:
                node.group = effective_group

        # Materialize the node as soon as the wrapper starts executing, so
        # decorated functions that never call nb.log* still appear in the
        # graph and act as real links in dependency chains.
        state.ensure_loggable(node_id)

        parent = _current_node.get()
        token = _current_node.set(node_id)

        # Set group context if this function defines a group
        group_token = None
        if group:
            group_token = _current_group.set(group)

        try:
            # Record DAG edges (data-flow-aware)
            # 1. Explicit depends_on edges (always added)
            if depends_on_ids:
                for dep in depends_on_ids:
                    state.add_edge(dep, node_id)

            # Record this node's parent for sibling filtering
            state._node_parents[node_id] = parent
            strategy = state.dag_strategy

            # 2. Strategy-dependent edge inference
            if strategy == "none":
                pass
            elif strategy == "stack":
                if parent is not None:
                    state.add_edge(parent, node_id)
            elif strategy == "both":
                if parent is not None:
                    state.add_edge(parent, node_id)
                producers = state.find_producers(args, kwargs, parent)
                for producer in producers:
                    state.add_edge(producer, node_id)
            elif strategy == "linear":
                # Chain nodes in first-execution order. Each unique node gets
                # exactly one incoming edge — from whichever node ran first
                # immediately before it. Subsequent calls to the same node
                # add no new edges.
                node_info = state.loggables.get(node_id)
                is_first_run = (
                    isinstance(node_info, NodeInfo) and node_info.exec_count == 0
                )
                if is_first_run:
                    if state._linear_last is not None and state._linear_last != node_id:
                        state.add_edge(state._linear_last, node_id)
                    state._linear_last = node_id
            else:  # "object" (default)
                producers = state.find_producers(args, kwargs, parent)
                if producers:
                    for producer in producers:
                        state.add_edge(producer, node_id)
                elif parent is not None and not depends_on_ids:
                    state.add_edge(parent, node_id)

            state.increment_count(node_id)
            result = f(*args, **kwargs)

            # Track return value for data-flow edge inference
            state.track_return(node_id, result)

            return result
        finally:
            _current_node.reset(token)
            if group_token is not None:
                _current_group.reset(group_token)

    wrapper._nb_decorated = True
    wrapper._nb_original = f
    return wrapper  # type: ignore
