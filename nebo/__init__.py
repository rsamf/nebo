"""Nebo — A modern logging SDK for multi-modal data.

Usage:
    import nebo as nb

    @nb.fn()
    def my_function():
        nb.log_text("status", "hello")
        nb.log_line("loss", 0.5)
"""

from __future__ import annotations

import os
import sys
import time
import uuid
import logging as _stdlib_logging
from typing import Any, Literal, Optional, TypeVar

from nebo.core.decorators import fn
from nebo.core.client import DaemonLocalOnlyError
from nebo.core.tracker import track
from nebo.core.config import log_cfg
from nebo.core.state import get_state
from nebo.alerts import AlertLevel, alert
from nebo.notebook import show
from nebo import labels
from nebo.core import groups
from nebo.logging.logger import (
    log,  # deprecated shim over log_text; warns once, removed in a later release
    log_text,
    log_line,
    log_bar,
    log_pie,
    log_scatter,
    log_histogram,
    log_image,
    log_audio,
    md,
)

T = TypeVar("T")

_auto_init_done = False
logger = _stdlib_logging.getLogger(__name__)


def _ensure_init() -> None:
    """Lazily run init()'s plumbing phase. Does NOT materialize a run.

    Plumbing = parsing config, installing the text logger,
    stashing the resolved (mode, dest) on state. A run/transport is
    only created on the first emit (`_ensure_run`) or explicit
    `nb.start_run()`.
    """
    global _auto_init_done
    if _auto_init_done:
        return
    init(_internal=True)


def _ensure_run() -> None:
    """Materialize a run on first nb.* emit. Idempotent.

    Called by every data-emitting `nb.*` surface (logger, decorator,
    `nb.ui`, `nb.alert`). NOT called by `nb.start_run` — that path
    manages the transport itself so its run_id is used directly.
    """
    _ensure_init()
    state = get_state()
    # Fast lock-free path: the common case after first emit.
    if state._run_materialized:
        return
    with state._lock_state:
        if state._run_materialized:
            return
        # Tests that pre-attach a fake transport (e.g. the
        # `capturing_client` fixture in conftest.py) treat that
        # attachment as "the run is already live". Don't second-guess
        # them — skip materialization, but record it as an implicit run
        # with an id so start_run's adoption/roll logic sees it.
        if state._transport is not None:
            state._run_materialized = True
            state._run_origin = "implicit"
            if state._active_run_id is None:
                state._active_run_id = state._pending_run_id or uuid.uuid4().hex[:12]
                state._pending_run_id = None
            return
        state._run_materialized = True
        run_id = state._pending_run_id or uuid.uuid4().hex[:12]
        state._pending_run_id = None  # consume once
    # _create_run_transport does its own send_event/print work; we
    # release the lock first so a slow file-open doesn't serialise
    # later callers that just need the materialized-True fast-path.
    _create_run_transport(run_id, name=None, config=None)


def init(
    uri: Optional[str] = None,
    *,
    dag_strategy: Literal["object", "stack", "both", "linear", "none"] = "object",
    flush_interval: float = 0.1,
    api_token: Optional[str] = None,
    group: Optional[str] = None,
    webhook_url: Optional[str] = None,
    webhook_min_level: Optional[int] = None,
    _internal: bool = False,
) -> None:
    """Initialize nebo.

    The ``uri`` argument selects the transport:

      * Omitted or a path-like string -> **file mode**. Events are written
        to ``<uri>/<timestamp>_<run_id>.nebo`` via the local SDK.
        Default is ``.nebo/``.
      * ``http://``/``https://`` URL or a bare ``host:port`` -> **network mode**.
        Events are POSTed to the daemon at that address.

    Env overrides: ``NEBO_URI`` overrides ``uri``; ``NEBO_RUN_ID``,
    ``NEBO_FLUSH_INTERVAL``, ``NEBO_API_TOKEN`` work as before.
    ``NEBO_QUIET=1`` suppresses the one-line startup banner.
    ``NEBO_NO_STORE=1`` makes file mode a no-op (no file is opened) —
    used by the test suite.

    Args:
        uri: Destination. See above. Defaults to ``.nebo/``.
        dag_strategy: How DAG edges are inferred between steps.
            'object' (default) uses sibling data-flow edges with parent
            fallback. 'stack' uses caller-to-callee edges only. 'both'
            is the union of object and stack edges. 'linear' chains nodes
            in first-execution order. 'none' disables automatic inference.
        flush_interval: Seconds between event flushes (default 0.1).
        api_token: Token for daemons that require auth. Sent as
            ``X-Nebo-Token``. Defaults to env var NEBO_API_TOKEN.
            Network mode only; ignored in file mode.
        group: Run-tree group path this run is born into (e.g.
            ``"vision/detr/lr-sweep"``), organizing it in the UI/CLI tree.
            ``NEBO_GROUP`` overrides this; ``nb.start_run(group=)`` overrides
            per run. Invalid paths raise ``ValueError``. See ``nb.groups``.
        webhook_url: Slack-compatible webhook URL for ``nb.alert()``.
        webhook_min_level: Minimum ``AlertLevel`` that fires the webhook.
    """
    global _auto_init_done

    if _auto_init_done and not _internal:
        import warnings
        warnings.warn(
            "nebo was already implicitly initialized by a prior nb.* call. "
            "Call nb.init() before any @nb.fn() execution, nb.log_text(), nb.md(), etc. "
            "This nb.init() call will be ignored.",
            stacklevel=2,
        )
        return

    _auto_init_done = True

    from nebo.core.uri import Mode, resolve_uri
    from nebo.core.client import NetworkTransport

    state = get_state()
    state.dag_strategy = dag_strategy
    if webhook_url is not None:
        state.webhook_url = webhook_url
    if webhook_min_level is not None:
        state.webhook_min_level = int(webhook_min_level)

    env_uri = os.environ.get("NEBO_URI")
    env_flush = os.environ.get("NEBO_FLUSH_INTERVAL")

    if env_uri:
        uri = env_uri
    if env_flush:
        flush_interval = float(env_flush)
    api_token = api_token or os.environ.get("NEBO_API_TOKEN")

    # Resolve URI eagerly so configuration errors surface at nb.init()
    # time, not deep inside the first emit.
    mode, dest = resolve_uri(uri)

    # Stash the resolved config; _create_run_transport consumes it
    # when the run is actually materialized.
    state._pending_mode = mode
    state._pending_dest = dest
    state._pending_flush_interval = flush_interval
    state._pending_api_token = api_token
    state._pending_run_id = os.environ.get("NEBO_RUN_ID")
    # Validate the group at the call site so a bad path fails fast (env and
    # start_run overrides are resolved + re-validated at run materialization).
    from nebo.core.groups import validate_group_path
    state._pending_group = validate_group_path(group)
    state._mode = "file" if mode is Mode.FILE else "network"

    _install_text_logger()

    # Network mode: eagerly construct + connect + warmup the transport
    # so the (potentially multi-minute) daemon-cold-start wait doesn't
    # shift to the first emit. We stash it on _pending_transport — it
    # becomes state._transport only when the run is materialized so
    # auto-generated init events can't precede a real `run_start`.
    if mode is not Mode.FILE:
        transport = NetworkTransport(
            base_url=dest,
            api_token=api_token,
            run_id=None,  # rebound to the real run_id in _create_run_transport
            flush_interval=flush_interval,
        )
        if not transport.connect():
            # A local-only daemon is a hard misconfiguration — surface it at
            # init() rather than silently buffering events that never land.
            if transport._policy_error:
                raise DaemonLocalOnlyError(transport._policy_error)
            print(
                f"warning: could not reach nebo daemon at {dest}. "
                "events will be buffered locally and dropped on exit.",
                file=sys.stderr,
            )
        else:
            if api_token:
                transport.warmup(timeout=180.0)
        state._pending_transport = transport


def _create_run_transport(
    run_id: str,
    *,
    name: Optional[str] = None,
    config: Optional[dict] = None,
    group: Optional[str] = None,
    origin: str = "implicit",
    apply_template: bool = True,
) -> None:
    """Open the transport for a run, emit register + run_start, print banner.

    Shared between the auto path (`_ensure_run`, name=None, config=None)
    and the explicit path (`start_run`). The caller is responsible for
    closing any prior transport BEFORE calling this — both call sites
    do (start_run via the close-old-run block, _ensure_run because
    `state._transport is None` is the precondition).

    `apply_template` copies the script-level nb.md()/nb.ui() template
    onto the new run (state + wire); start_run passes False on resume so
    the appended description isn't doubled.
    """
    from nebo.core.uri import Mode, resolve_uri
    from nebo.core.transport import FileTransport

    state = get_state()
    mode = state._pending_mode
    dest = state._pending_dest
    if mode is None:
        # A SessionState.reset() clears _pending_mode but leaves the
        # module-global _auto_init_done set, so init() never re-stashes
        # the config. Re-resolve defaults here — otherwise `None` falls
        # into the network branch with an empty dest and the next emit
        # probes localhost:7861 (2 s stall when something else squats on
        # the port, silently-dropped events either way).
        mode, dest = resolve_uri(os.environ.get("NEBO_URI"))
    no_store = bool(os.environ.get("NEBO_NO_STORE"))
    quiet = bool(os.environ.get("NEBO_QUIET"))
    script_name = os.path.abspath(sys.argv[0]) if sys.argv else "script"

    # Resolve the run-tree group: NEBO_GROUP > start_run(group=) > init(group=).
    from nebo.core.groups import validate_group_path
    env_group = os.environ.get("NEBO_GROUP")
    if env_group is not None:
        group = env_group
    elif group is None:
        group = state._pending_group
    group = validate_group_path(group)

    transport: Any = None
    banner_endpoint = ""

    if mode is Mode.FILE:
        if not no_store:
            transport = FileTransport(
                logdir=dest,
                run_id=run_id,
                script_path=script_name,
                flush_interval=state._pending_flush_interval,
                run_name=name,
                group=group,
            )
            banner_endpoint = str(transport.filepath)
        else:
            banner_endpoint = f"{dest} (NEBO_NO_STORE=1; events dropped)"
    else:
        # Promote the eagerly-built network transport. After this point
        # _pending_transport is None — a second materialization in the
        # same process (via start_run's transport roll) would need to
        # construct a fresh NetworkTransport. That path is rare but
        # handled below.
        transport = state._pending_transport
        state._pending_transport = None
        if transport is None:
            from nebo.core.client import NetworkTransport
            transport = NetworkTransport(
                base_url=dest,
                api_token=state._pending_api_token,
                run_id=run_id,
                flush_interval=state._pending_flush_interval,
            )
            if not transport.connect():
                if transport._policy_error:
                    raise DaemonLocalOnlyError(transport._policy_error)
                print(
                    f"warning: could not reach nebo daemon at {dest}. "
                    "events will be buffered locally and dropped on exit.",
                    file=sys.stderr,
                )
            elif state._pending_api_token:
                transport.warmup(timeout=180.0)
        else:
            transport._run_id = run_id  # rebind to this run's id
        banner_endpoint = dest

    state._transport = transport
    state._active_run_id = run_id
    state._run_origin = origin
    state._run_has_real_events = False

    if transport is not None:
        state._send_to_client({
            "type": "loggable_register",
            "loggable_id": "__global__",
            "data": {"loggable_id": "__global__", "kind": "global"},
        })
        run_start_data: dict[str, Any] = {
            "script_path": script_name,
            "timestamp": time.time(),
        }
        if name is not None:
            run_start_data["run_name"] = name
        if group:
            run_start_data["group"] = group
        state._send_to_client({
            "type": "run_start",
            "data": run_start_data,
        })
        if config is not None:
            state._send_to_client({
                "type": "run_config",
                "data": config,
            })

    # Apply the script-level metadata template (outside the transport
    # guard: state must update under NEBO_NO_STORE too — _send_to_client
    # no-ops safely). The template is never cleared; every new run gets it.
    if apply_template:
        if state._script_description is not None:
            state.workflow_description = state._script_description
            state._send_to_client({
                "type": "description",
                "data": {"description": state._script_description},
            })
        if state._script_ui_config is not None:
            state.ui_config = dict(state._script_ui_config)
            state._send_to_client({
                "type": "ui_config",
                "data": state.ui_config,
            })

    if not quiet:
        if mode is Mode.FILE:
            print(f"nebo: writing run (run_id={run_id}) to {banner_endpoint}")
        else:
            print(f"nebo: connected run (run_id={run_id}) to {banner_endpoint}")


def _install_text_logger() -> None:
    """Route nb.log_text()'s messages to stdout via the 'nebo' stdlib logger.

    Idempotent — repeated calls don't stack handlers.
    """
    nebo_logger = _stdlib_logging.getLogger("nebo")
    nebo_logger.handlers = [
        h for h in nebo_logger.handlers
        if not getattr(h, "_nebo_managed", False)
    ]
    handler = _stdlib_logging.StreamHandler(sys.stdout)
    handler.setFormatter(_stdlib_logging.Formatter("%(message)s"))
    handler._nebo_managed = True  # type: ignore[attr-defined]
    nebo_logger.addHandler(handler)
    if nebo_logger.level == _stdlib_logging.NOTSET:
        nebo_logger.setLevel(_stdlib_logging.INFO)


def flush(timeout: float = 5.0) -> bool:
    """Block until queued events are sent to the daemon.

    Useful for fencing a logging-heavy section before something
    irreversible (saving artifacts, saving videos etc.) so the
    UI shows everything that was logged before that point.

    Args:
        timeout: Max seconds to wait. Default 5.0.

    Returns:
        True if all queued events were flushed (or there was no
        client to flush). False if the timeout elapsed with events
        still unsent — those events remain in the client's internal
        buffer and may be retried by the next periodic flush, but
        will be lost if the process exits before they go out.

    No-op (returns True) in local mode, before init, or after the
    daemon has been disconnected.
    """
    state = get_state()
    client = state._transport
    if client is None:
        return True
    return client.flush(timeout=timeout)


def ui(
    layout: Optional[Literal["horizontal", "vertical"]] = None,
    view: Optional[Literal["dag", "flat"]] = None,
    minimap: Optional[bool] = None,
    theme: Optional[Literal["dark", "light"]] = None,
    tracker: Optional[Literal["time", "step"]] = None,
) -> None:
    """Set run-level UI defaults.

    These are sent to the daemon and UI as defaults.
    The user can still override them in the UI.

    Declarative scoping rule: called outside a run, the config is
    script-level and applies to every run this process opens (it does
    not materialize a run); called inside a run, it applies to that
    run only. Repeat calls overwrite either way.

    Args:
        layout: DAG layout direction ("horizontal" or "vertical").
        view: Default view mode ("dag" or "flat").
        minimap: Show minimap.
        theme: Color theme ("dark" or "light").
        tracker: Default timeline scrubber mode ("time" or "step").
    """
    _ensure_init()
    state = get_state()
    config: dict[str, Any] = {}
    if layout is not None:
        config["layout"] = layout
    if view is not None:
        config["view"] = view
    if minimap is not None:
        config["minimap"] = minimap
    if theme is not None:
        config["theme"] = theme
    if tracker is not None:
        config["tracker"] = tracker

    if not state.run_is_live():
        # Declarative: script-level template, applied at every new-run
        # materialization. No run, no file, no event.
        state._script_ui_config = config
        return

    state.ui_config = config
    state._send_to_client({
        "type": "ui_config",
        "data": config,
    })


def _resolve_config(config: Any) -> dict:
    """Convert config to a plain dict, handling OmegaConf DictConfig."""
    if config is None:
        return {}
    try:
        from omegaconf import DictConfig, OmegaConf
        if isinstance(config, DictConfig):
            return OmegaConf.to_container(config, resolve=True)  # type: ignore
    except ImportError:
        pass
    if isinstance(config, dict):
        return config
    return dict(config)


class _RunContext:
    """Context manager returned by start_run()."""

    def __init__(self, run_id: str, name: Optional[str], config: Optional[dict]) -> None:
        self.run_id = run_id
        self.name = name
        self.config = config

    def __enter__(self) -> _RunContext:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        state = get_state()
        client = state._transport
        # Save state snapshot before completing
        state.save_run_state(self.run_id)
        if client is not None:
            client.send_event({
                "type": "run_completed",
                "data": {"timestamp": time.time()},
            })
            client.flush()
            client._run_completed = True
            from nebo.core.transport import FileTransport
            if isinstance(client, FileTransport):
                client._run_completed_sent = True
        state._active_run_id = None


def start_run(
    name: Optional[str] = None,
    config: Optional[dict] = None,
    run_id: Optional[str] = None,
    group: Optional[str] = None,
) -> _RunContext:
    """Start a new run or resume an existing one.

    Can be used as a context manager or plain function call.

    Args:
        name: Optional display name for the run.
        config: Optional config dict (or OmegaConf DictConfig).
        run_id: Optional run_id to resume a previous run.
        group: Run-tree group path for this run (overrides ``nb.init(group=)``;
            ``NEBO_GROUP`` overrides both). Invalid paths raise ``ValueError``.

    Returns:
        A _RunContext with a .run_id attribute.
    """
    _ensure_init()
    state = get_state()

    if group is not None:
        from nebo.core.groups import validate_group_path
        validate_group_path(group)  # fail fast at the call site

    resolved_config = _resolve_config(config) if config is not None else None
    resuming = run_id is not None and run_id in state._run_snapshots

    # Adopt a virgin implicit run instead of opening a sibling: if
    # something materialized a run before us but it never carried a real
    # event (nothing beyond run identity/metadata), this start_run is
    # unambiguously describing THAT run — upgrade it in place, keep its
    # id and transport. With declarative md/ui this is nearly unreachable
    # via the public API (every _ensure_run caller emits a real event
    # right after), so it's a cheap invariant, not a hot path. Runs with
    # real events keep today's sibling semantics (interleaved runs), and
    # an explicit run_id always means resume/interleave intent.
    if (
        run_id is None
        and state._run_materialized
        and state._run_origin == "implicit"
        and not state._run_has_real_events
        and state._active_run_id is not None
    ):
        adopted_id = state._active_run_id
        run_start_data: dict[str, Any] = {
            "script_path": os.path.abspath(sys.argv[0]) if sys.argv else "script",
            "timestamp": time.time(),
        }
        if name is not None:
            run_start_data["run_name"] = name
        # Same group precedence as _create_run_transport: NEBO_GROUP >
        # start_run(group=) > init(group=). The daemon's tree placement
        # is seed-once, so a re-emit only wins if nothing seeded yet.
        from nebo.core.groups import validate_group_path
        env_group = os.environ.get("NEBO_GROUP")
        resolved_group = validate_group_path(
            env_group if env_group is not None
            else (group if group is not None else state._pending_group)
        )
        if resolved_group:
            run_start_data["group"] = resolved_group
        # A repeat run_start updates run_name/script_path on the daemon.
        # In file mode it's a body entry read at deep ingest — the
        # already-written header lacks the name, so a shallow listing
        # shows the run unnamed until deepened (accepted).
        state._send_to_client({"type": "run_start", "data": run_start_data})
        if resolved_config is not None:
            state._send_to_client({"type": "run_config", "data": resolved_config})
        state._run_origin = "explicit"
        state._pending_run_id = None
        return _RunContext(adopted_id, name, resolved_config)

    # Snapshot the prior run's in-memory state under its run_id so a
    # later `nb.start_run(run_id=<prior>)` can resume from it. This
    # happens regardless of whether a transport is live — NEBO_NO_STORE
    # users still expect snapshot/resume semantics.
    if state._active_run_id is not None:
        state.save_run_state(state._active_run_id)

    # If an implicit run was live (something emitted before us), close
    # its transport cleanly so its .nebo file ends with run_completed
    # before we roll to this run.
    if state._transport is not None:
        client = state._transport
        client.send_event({
            "type": "run_completed",
            "data": {"timestamp": time.time()},
        })
        client.flush()
        from nebo.core.transport import FileTransport
        if isinstance(client, FileTransport):
            client._run_completed_sent = True
            client.close()
        state._transport = None

    if resuming:
        # Restore snapshot for resumed run.
        state.restore_run_state(run_id)  # type: ignore[arg-type]
    else:
        run_id = run_id or uuid.uuid4().hex[:12]
        state.clear_run_state()

    # Don't reuse the NEBO_RUN_ID env value here — start_run's run_id
    # is explicit. Clear so a later `_ensure_run` (if start_run is used
    # without a context manager and then more emits happen) wouldn't
    # accidentally re-consume the env override.
    state._pending_run_id = None

    _create_run_transport(
        run_id, name=name, config=resolved_config, group=group,
        origin="explicit",
        # A resumed run already got the template at its original
        # materialization — re-applying would double-append the md.
        apply_template=not resuming,
    )
    # Flip the materialized flag so _ensure_run is a no-op for the
    # remainder of the process: start_run takes ownership of the
    # run lifecycle.
    state._run_materialized = True

    return _RunContext(run_id, name, resolved_config)  # type: ignore[arg-type]


__all__ = [
    "fn",
    "track",
    "init",
    "flush",
    "log",  # deprecated alias for log_text
    "log_text",
    "log_cfg",
    "log_line",
    "log_bar",
    "log_pie",
    "log_scatter",
    "log_histogram",
    "log_image",
    "log_audio",
    "md",
    "labels",
    "groups",
    "ui",
    "start_run",
    "get_state",
    "DaemonLocalOnlyError",
]
