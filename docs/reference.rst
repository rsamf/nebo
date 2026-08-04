.. _API Reference:

API Reference
#############

This is the complete API reference for the ``nebo`` package.


Module: ``nebo``
=================

The top-level module provides all the public functions you need. Import it as:

.. code-block:: python

    import nebo as nb


Decorators
----------

.. function:: nb.fn(func=None, depends_on=None, ui=None)

    Register a function or class for observability. When applied to a function, it sets up scope tracking — the node materializes (becomes visible in the DAG) as soon as the decorated function is executed, regardless of whether it calls a log function. When applied to a class, all methods are wrapped with scope tracking and the class becomes a visual grouping container in the DAG.

    :param func: The function or class to decorate (when used without parentheses).
    :param depends_on: Optional list of decorated functions or node ID strings that this node depends on.
    :param ui: Optional dict of per-node UI display hints (e.g., ``{"color": "#34d399", "default_tab": "metrics"}``).
    :returns: The decorated function or class (unchanged behavior, with observability added).

    Usage forms:

    .. code-block:: python

        @nb.fn                          # bare decorator
        @nb.fn()                        # with empty parentheses
        @nb.fn(depends_on=[setup])      # with explicit dependencies
        @nb.fn(ui={"color": "#34d399"}) # with per-node UI hints

    **On functions:** The node ID is derived from the function's ``__qualname__``. The function's docstring becomes the node's description. The node materializes (becomes visible) on the first call to the decorated function, so even silent functions that never log appear in the DAG and act as real links in dependency chains.

    **On classes:** All methods are wrapped. The class itself is never a node — it is a transparent bounding box in the DAG. Every method that runs materializes as a node inside that bounding box, including methods that never call a log function.


Logging Functions
-----------------

.. function:: nb.log_text(name: str, message: str | Any, *, step: int | None = None) -> None

    Log a text message to a named stream on the current node. Text is a named payload stream like the metric and media helpers — ``log_text`` pairs a stream ``name`` with a ``message`` the way ``log_line`` pairs a name with a value. Tensor-like objects (NumPy arrays, PyTorch tensors) are auto-formatted with shape, dtype, and statistics.

    :param name: Stream name for this entry, e.g. ``"status"`` or ``"rollout/summary"``. Different names create distinct streams, each shown as its own card in the UI and its own row in the Tracker tree.
    :param message: The message string or tensor-like object.
    :param step: Optional step counter.

.. function:: nb.log_line(name, value, *, step=None, tags=None)

   Log a scalar line-chart datapoint. Line **accumulates** — repeated
   calls with the same name append another point to the series.
   ``step`` auto-increments per ``(loggable, name)`` when omitted.

   :param name: Metric name.
   :param value: A scalar ``int | float`` (NumPy/PyTorch scalars are accepted).
   :param step: Optional step counter.
   :param tags: List of strings attached to this emission for UI filtering.

.. function:: nb.log_bar(name, value)

   Log a bar-chart snapshot. ``value`` is a dict ``{label: number}``.
   Repeated calls with the same name **overwrite** the prior value.

.. function:: nb.log_pie(name, value)

   Log a pie-chart snapshot. ``value`` is a dict ``{label: number}``.
   Repeated calls with the same name **overwrite** the prior value.

.. function:: nb.log_scatter(name, value, *, step=None, tags=None, colors=False)

   Log a labeled scatter emission. ``value`` is a dict
   ``{label: list[(x, y)]}`` — every label becomes its own series on
   the same chart and is toggleable via the UI chip row. Scatter
   **accumulates**: repeated calls with the same name append more
   points to the same plot. ``step`` auto-increments per
   ``(loggable, name)`` when omitted, so each emission can be
   correlated to a moment in the run (e.g. clicking a point in the UI
   filters text/images/audio to that step).

   :param step: Optional step counter for this emission.
   :param tags: List of strings attached to this emission for UI filtering.
   :param colors: When ``True``, distinguish labels by palette color
       (in addition to the per-label shape). Default ``False``
       (every label uses the run color, distinguished by shape only).
       **Not recommended in comparison views** where color is reserved
       for run identity.

.. function:: nb.log_histogram(name, value, *, colors=False)

   Log a labeled histogram snapshot. ``value`` is a dict
   ``{label: list[number]}`` — every label is a distribution; the UI
   bins all labels against a shared range so overlapping
   distributions line up. Repeated calls with the same name
   **overwrite** the prior value. To log a single histogram, wrap in
   a single-key dict, e.g. ``{"all": samples}``.

   :param colors: When ``True``, give each label a distinct palette
       color so overlapping distributions can be picked apart.
       Default ``False`` (every label uses the run color; the overlap
       reads as a single mass via alpha compositing). **Not
       recommended in comparison views** where color is reserved for
       run identity.

The chart type locks on first emission per ``(loggable, name)`` pair —
mixing ``log_line`` and ``log_bar`` for the same metric name raises
``ValueError``.

Steps and tags apply to the accumulating helpers (``log_line`` and
``log_scatter``) only. The snapshot helpers ignore them; passing
``step=`` or ``tags=`` to ``log_bar`` / ``log_pie`` / ``log_histogram``
is a ``TypeError``.

Clicking any point on a line or scatter chart in the web UI sets a
global step filter: the Tracker (bottom panel) switches to step mode,
the clicked step is highlighted on every line/scatter chart (vertical
guideline + value bubble for line, dimmed non-matching points for
scatter), and the per-node text/images/audio panels filter to entries
whose ``step`` matches. Use the Tracker's **Clear all filters** button to
clear the step filter (the **Reset zoom** button only resets the timeline
zoom). You can also step through with the prev/next arrows or
Ctrl/⌘+Left/Right.

Example::

    nb.log_line("loss", 0.5)
    nb.log_bar("counts", {"cat": 3, "dog": 5})
    # Accumulating scatter — call once per emission; step auto-advances.
    for x, y in points:
        nb.log_scatter("embed_2d", {"inliers": [(x, y)]})
    nb.log_histogram(
        "latencies",
        {"p50": [...], "p95": [...], "p99": [...]},
        colors=True,
    )
    nb.log_line("lr", 3e-4, tags=["main"])

.. function:: nb.log_image(image, *, name=None, step=None, points=None, boxes=None, circles=None, polygons=None, bitmasks=None)

   Log an image with optional geometric labels overlaid.

   :param image: PIL.Image, numpy ndarray, or torch.Tensor.
   :param name: Display label for the image.
   :param step: Optional step counter.
   :param points: ``nb.labels.Points`` instance or list of them.
   :param boxes: ``nb.labels.Boxes`` instance or list of them (xyxy format).
   :param circles: ``nb.labels.Circles`` instance or list of them.
   :param polygons: ``nb.labels.Polygons`` instance or list of them.
       ``Polygons`` takes an extra ``fill: bool = True`` flag — set
       ``fill=False`` to stroke the outline only.
   :param bitmasks: ``nb.labels.Bitmasks`` instance or list of them.

   Each label dataclass pairs the raw geometry (list / ndarray / tensor)
   with a color string ("#hex" or any CSS color name). Pass a list to
   draw multiple groups of the same kind in different colors —
   ``boxes=[Boxes(preds, color="#22d3ee"), Boxes(gt, color="#22c55e")]``.
   Raw lists / tensors are rejected with a ``TypeError`` pointing at the
   matching ``nb.labels.*`` class.

   Tensors and ndarrays are normalized to plain Python lists; bitmasks
   are PNG-encoded and transmitted inline. The UI's Settings pane >
   "Image labels" section exposes per-(loggable, image, key) visibility
   and opacity controls.

   Example::

       nb.log_image(
           img, name="pred",
           boxes=nb.labels.Boxes([[10, 10, 50, 50]], color="#22d3ee"),
           points=nb.labels.Points([[30, 30]], color="red"),
       )

.. function:: nb.log_audio(audio: Any, sr: int = 16000, *, name: str | None = None, step: int | None = None) -> None

    Log audio data.

    :param audio: Audio data as a NumPy array.
    :param sr: Sample rate (default: 16000).
    :param name: Optional audio clip name.
    :param step: Optional step counter.

.. function:: nb.log_cfg(cfg: dict[str, Any]) -> None

    Log configuration for the current node. Merges *cfg* into the node's ``params`` dict so the Info tab displays all configuration in one place.

    :param cfg: A flat or nested dictionary of configuration values. Only JSON-serializable values are retained.

    Multiple calls within the same node merge dictionaries (later calls win on key conflicts).


Progress Tracking
-----------------

.. function:: nb.track(iterable: Iterable[T], name: str | None = None, total: int | None = None) -> TrackedIterable[T]

    Wrap an iterable for tqdm-like progress tracking. The progress is reported to the terminal dashboard, daemon, and web UI.

    :param iterable: The iterable to wrap.
    :param name: Display name for the progress bar.
    :param total: Total number of items. Auto-detected from ``__len__`` if available.
    :returns: A ``TrackedIterable`` that yields items and reports progress.


Workflow Description
--------------------

.. function:: nb.md(description: str) -> None

    Set or append to the workflow-level Markdown description. Distinct from node docstrings — this describes the overall workflow.

    Declarative: called outside a run it creates none — the description is script-level and applies to every run the script opens; called inside a run it applies to that run only.

    :param description: Markdown description text.


UI Configuration
-----------------

.. function:: nb.ui(layout: str | None = None, view: str | None = None, minimap: bool | None = None, theme: str | None = None) -> None

    Set run-level UI defaults. These are sent to the daemon and web UI as defaults that the user can override.

    :param layout: DAG layout direction: ``"horizontal"`` or ``"vertical"``.
    :param view: Default view mode: ``"dag"`` or ``"flat"``.
    :param minimap: Whether to show the minimap.
    :param theme: Color theme: ``"dark"`` or ``"light"``.

    Calling ``nb.ui()`` again overwrites the previous defaults.

    Declarative: called outside a run it creates none — the defaults are script-level and apply to every run the script opens; called inside a run they apply to that run only.


Initialization
--------------

.. function:: nb.init(uri: str | None = None, *, dag_strategy: str = "object", flush_interval: float = 0.1, api_token: str | None = None, group: str | None = None, webhook_url: str | None = None, webhook_min_level: int | None = None) -> None

    Explicitly initialize nebo.

    :param uri: Destination for events. Selects the transport by shape:

        * ``None`` or a path-like string (default: ``".nebo/"``) — **file
          mode**. The SDK writes ``<uri>/<timestamp>_<run_id>.nebo``
          directly via ``FileTransport``. No daemon required.
        * An HTTP URL (``"http://localhost:7861"``, ``"https://my-space.hf.space"``)
          or bare ``"host:port"`` — **network mode**. Events are POSTed
          to the daemon via ``NetworkTransport``.

        Overridable via the ``NEBO_URI`` environment variable.

    :param dag_strategy: How DAG edges are inferred: ``"object"`` (default), ``"stack"``, ``"both"``, ``"linear"``, or ``"none"``. ``"linear"`` chains nodes in first-execution order.
    :param flush_interval: Seconds between event flushes (default: 0.1).
    :param api_token: Token for daemons that require auth (network mode only). Sent as the ``X-Nebo-Token`` header. Defaults to env var ``NEBO_API_TOKEN``.
    :param group: Run-tree group path this run is born into (e.g. ``"vision/detr/lr-sweep"``), organizing it in the UI/CLI tree. ``nb.start_run(group=)`` overrides per run; ``NEBO_GROUP`` overrides both. Invalid paths raise ``ValueError``.
    :param webhook_url: Slack-compatible webhook URL for ``nb.alert()``.
    :param webhook_min_level: Minimum ``AlertLevel`` that fires the webhook.
    :raises nb.DaemonLocalOnlyError: In network mode, if the daemon is
        running local-only (plain ``nebo serve``) and so refuses network
        runs — restart it with ``--remote`` or ``--remote-ephemeral``. Raised
        immediately (fail-fast) rather than after buffering events.

    .. note::

        You rarely need to call ``init()`` explicitly. The SDK auto-initializes on the first ``@fn`` execution or ``nb.log*`` call. Set ``NEBO_URI`` and ``NEBO_API_TOKEN`` in the environment so the same code works in file mode locally and against a remote daemon without a change.


Run Lifecycle
-------------

.. function:: nb.start_run(name: str | None = None, config: dict | None = None, run_id: str | None = None, group: str | None = None) -> _RunContext

    Start a new run, or resume one started earlier in the same process.
    Works as a context manager (the run completes when the block exits)
    or as a plain call (the run stays open until the next ``start_run``
    or process exit):

    .. code-block:: python

        with nb.start_run(name="lr=3e-4", config={"lr": 3e-4}):
            train()

        run = nb.start_run(name="eval")   # plain call
        evaluate()

    If a run is already live when ``start_run`` is called, it is closed
    cleanly (its file ends with a completion marker) and its in-memory
    state is snapshotted under its run_id — so one process can open many
    runs back to back, and later resume any of them.

    :param name: Display name for the run, shown in the UI and run listings
        in place of the script basename.
    :param config: Run-level config dict (plain dict or OmegaConf
        ``DictConfig``), shown in the UI's Config panel. This is the
        run-scoped counterpart of ``nb.log_cfg`` (which attaches config to
        the current ``@fn`` node).
    :param run_id: Resume the run with this id. Only run_ids from the
        same process can be resumed — the SDK restores the run's saved
        in-memory state (metric cursors, DAG bookkeeping) and subsequent
        events append to the same run. When omitted, a fresh run_id is
        generated.
    :param group: Run-tree group path for this run (e.g.
        ``"vision/detr/lr-sweep"``). Overrides ``nb.init(group=)``;
        ``NEBO_GROUP`` overrides both.
    :returns: A ``_RunContext`` handle with a ``.run_id`` attribute, usable
        as a context manager.
    :raises ValueError: If *group* is not a valid group path.

    .. note::

        Declarative script-level metadata (``nb.md()`` / ``nb.ui()`` called
        outside a live run) is applied to every **new** run ``start_run``
        opens — but not on ``run_id=`` resume, which would duplicate it.

    .. note::

        Without any ``start_run``, a script is simply one implicit run,
        materialized on the first logged event. Calling ``start_run``
        before anything has been logged names that run rather than opening
        a second one.


Notebook Embedding
------------------

.. function:: nb.show(*, run=None, node=None, metric=None, image=None, audio=None, text=None, dag=False, width="100%", height=600)

    Return a Jupyter-renderable iframe of the daemon UI scoped to one
    slice of a run. The slice is determined by which kwargs are set —
    pass nothing to embed the full run dashboard. At most one slice
    kwarg may be set; passing more than one raises ``ValueError``.

    :param run: Run ID to embed. Defaults to the active run.
    :param node: Node ID or function name. With no slice kwarg, shows the node detail; combined with a slice it filters that slice.
    :param metric: ``str`` shows a single metric by name; ``True`` shows the metrics gallery (filtered by ``node`` if set).
    :param image: Same shape as ``metric`` for images.
    :param audio: Same shape as ``metric`` for audio recordings.
    :param text: Same shape as ``metric`` for text streams — a ``str`` shows a single named stream; ``True`` shows the text panel (filtered by ``node`` if set).
    :param dag: ``True`` shows the DAG-only view.
    :param width, height: iframe dimensions. Strings (``"100%"``) or ints (px).
    :returns: A handle whose ``_repr_html_`` emits the ``<iframe>``.

    Example::

        nb.show(metric="loss")                # one metric chart
        nb.show(node="train", metric=True)    # gallery of train's metrics
        nb.show(image="hero.png")             # one image
        nb.show(text=True, node="train")      # text panel filtered to train
        nb.show(text="status")                # one named text stream

Iframe URL scheme
~~~~~~~~~~~~~~~~~

The dashboard switches into embedded mode whenever a ``?run=…`` query
param is present. The slice is inferred from the other params — there
is no ``view=`` discriminator:

================================  ====================================
URL                               Renders
================================  ====================================
``?run=X``                        Full run dashboard (DAG + timeline)
``?run=X&dag``                    DAG only
``?run=X&flat``                   Flat card grid (desktop flat view)
``?run=X&node=Y``                 Single node detail
``?run=X&text``                   Text panel
``?run=X&text=status``            Single named text stream
``?run=X&metrics``                Metrics gallery
``?run=X&metric=loss``            Single metric
``?run=X&images``                 Image gallery
``?run=X&image=hero.png``         Single image
``?run=X&audios``                 Audio gallery
``?run=X&audio=bell.wav``         Single audio recording
================================  ====================================

Add ``&node=Y`` to any of the slice forms to filter to one node.
Append ``&token=…`` to authenticate with a token-protected daemon —
the dashboard captures it once, persists it in localStorage, and
strips it from the visible URL.

A canonical ``nebo://`` reference can also be embedded directly via
``?ref=…``: a bare run ref (``?ref=nebo://run/X``) renders the full
dashboard, a loggable ref (``?ref=nebo://run/X/train``) renders that
node's card.


State Access
------------

.. function:: nb.get_state() -> SessionState

    Get the global session state singleton. Advanced usage — most users won't need this.

    :returns: The ``SessionState`` instance containing all nodes, edges, and configuration.


CLI Reference: ``nebo``
=======================

.. code-block:: text

    nebo <command> [options]

Commands
--------

``serve``
    Start the persistent daemon server.

    .. code-block:: console

        $ nebo serve [--host HOST] [--port PORT] [-d] [--no-store]
                     [--store-dir DIR] [--api-token TOKEN]
                     [--read public|private] [--write public|private]

    ``--host``
        Host to bind (default: ``localhost``).

    ``--port``
        Port to bind (default: ``7861``).

    ``-d``, ``--daemon``
        Run in background (daemon mode).

    ``--no-store``
        Disable ``.nebo`` file storage globally.

    ``--store-dir DIR``
        Directory for ``.nebo`` files (default: ``./.nebo``). Sets
        ``NEBO_STORE_DIR``. Useful when hosting on Hugging Face Spaces
        where the persistent volume mounts at ``/data``.

    ``--api-token TOKEN``
        Require this token on API requests. Sets ``NEBO_API_TOKEN``.
        See ``--read`` / ``--write`` for the modes that activate when
        the token is set.

    ``--read public|private``
        Read-access mode (default: ``public``). Only matters when
        ``--api-token`` is set.

    ``--write public|private``
        Write-access mode (default: ``private``). Only matters when
        ``--api-token`` is set.

``status``
    Show daemon status and recent runs.

    .. code-block:: console

        $ nebo status [--port PORT]

``stop``
    Stop the daemon.

    .. code-block:: console

        $ nebo stop [--port PORT]

``text ls``
    View text entries from runs. Each entry prints as
    ``[loggable_id] name@step: message``.

    .. code-block:: console

        $ nebo text ls [--run RUN_ID] [--node NODE] [--limit N] [--port PORT]

``load``
    Load a ``.nebo`` file into the daemon for viewing and Q&A. With
    ``--url`` (or ``NEBO_CLI_URL`` env), the file is read locally and its
    events are replayed to the remote daemon — useful when the daemon
    is on a Hugging Face Space and can't see the user's filesystem.

    .. code-block:: console

        $ nebo load <file> [--port PORT]
        $ nebo load <file> --url URL [--api-token TOKEN]

    ``--url URL``
        Remote daemon URL (e.g. an HF Space). Defaults to ``NEBO_CLI_URL``.

    ``--api-token TOKEN``
        Token for the remote daemon. Defaults to ``NEBO_API_TOKEN``.

``mcp``
    Print MCP connection config for Claude Code / Claude Desktop.

    .. code-block:: console

        $ nebo mcp

``mcp-stdio``
    Run the MCP stdio bridge (used internally).

    .. code-block:: console

        $ nebo mcp-stdio [--port PORT]

``deploy``
    Deploy the nebo daemon to a Hugging Face Space. Creates (or
    reuses) a Docker-SDK Space, sets ``NEBO_API_TOKEN`` as a Space
    secret, sets the read/write mode variables, and uploads a
    Dockerfile and a Spaces-flavored README.

    Requires the optional ``deploy`` extra:

    .. code-block:: console

        $ pip install 'nebo[deploy]'

    .. code-block:: console

        $ nebo deploy --space-id <user>/<space> [--from-source]
                      [--api-token TOKEN] [--hf-token TOKEN] [--private]
                      [--read public|private] [--write public|private]

    ``--space-id <user>/<space>``
        Hugging Face Space identifier (required).

    ``--from-source``
        Build a wheel from the current checkout and ship it instead of
        installing from PyPI. Use this to deploy un-released code.

    ``--api-token TOKEN``
        Token clients must send via ``X-Nebo-Token`` / ``?token=``.
        Random if omitted; printed once after the deploy completes.

    ``--hf-token TOKEN``
        Hugging Face write token. Defaults to the ``HF_TOKEN`` env or
        a cached login (``huggingface-cli login``).

    ``--private``
        Create the Space as HF-private (visible only to your account).

    ``--read public|private`` (default ``public``)
        Read-access mode for the deployed daemon.

    ``--write public|private`` (default ``private``)
        Write-access mode for the deployed daemon.


MCP Tools Reference
====================

The daemon exposes 23 MCP tools split across observation, action, run-tree,
and write categories.

Observation Tools
-----------------

``nebo_get_graph``
    Get the full DAG structure: nodes (with docstrings, execution counts, group membership), edges, and workflow description.

    :param run_id: Optional run ID. Uses the latest run if omitted.

``nebo_get_loggable_status``
    Get detailed status for a specific loggable (node or global): execution count, params, docstring, recent text entries, and progress.

    :param loggable_id: The loggable ID (required).
    :param run_id: Optional run ID.

``nebo_get_text``
    Get recent text entries, optionally filtered by loggable and run.

    :param loggable_id: Optional loggable ID filter.
    :param run_id: Optional run ID.
    :param limit: Maximum entries (default: 100).

``nebo_get_metrics``
    Get metric time series for a loggable.

    :param loggable_id: The loggable ID (required).
    :param name: Optional specific metric name.

    :param run_id: Optional run ID.

``nebo_get_description``
    Get the workflow-level description and all node docstrings.

Run & Alert Tools
-----------------

``nebo_get_run_status``
    Get the summary of a run: timestamps, node/edge counts,
    ``run_config``, ``metric_series_count``, ``latest_step``, and
    ``metrics_index`` (``{loggable_id: [name, ...]}``) so callers can
    discover available metric names without iterating every loggable
    card.

    :param run_id: The run ID (required).

``nebo_get_run_history``
    List all runs with timestamps, counts, and metric indexes.

``nebo_wait_for_alert``
    Block until an alert at or above ``min_level`` fires for the run,
    or the timeout elapses. Alerts come from ``nb.alert(...)`` calls in
    pipeline code or from alert rules created via ``nebo_set_alert``.

    :param run_id: Run ID to monitor (required).
    :param timeout: Max seconds to wait (default: 300).
    :param min_level: Minimum alert level to trigger on (default: 20).

``nebo_list_alerts``
    List alert rules (``triggered_by: "cli"``, with condition and fired
    history) and code-fired alerts (``triggered_by: "code"``).

    :param run_id: Optional run ID to scope the listing.

``nebo_set_alert``
    Create an alert rule on a metric condition — no code changes
    needed. The rule fires at most once per run.

    :param title: Alert headline (required).
    :param condition: Condition string, e.g. ``"train/loss > 5"``
        (ops: ``> >= < <= == !=``) (required). The reserved metric
        ``last_event`` fires on idle seconds (``"last_event > 60"`` =
        run quiet for 60s; ops ``>``/``>=`` only) — the way to detect
        run completion.
    :param text: Optional body.
    :param level: 10/20/30/40 for DEBUG/INFO/WARN/ERROR (default: 20).
    :param loggable_id: Only match the metric on this loggable.
    :param run_id: Only apply to this run (default: all runs).

``nebo_delete_alert``
    Delete an alert rule by id.

    :param rule_id: Alert rule id (required).

``nebo_load_file``
    Load a ``.nebo`` log file into the daemon for viewing.

    :param filepath: Absolute path to the ``.nebo`` file (required).

Write Tools
-----------

These mirror the SDK's ``nb.log_*`` helpers as MCP tools so an
external agent can push data into a run without owning the SDK
process. Each tool accepts a single entry or a list of entries;
entries are auto-registered as loggables so unknown ``loggable_id``
values aren't silently dropped.

``nebo_log_metric``
    Log one or more metric points. Mirrors ``nb.log_line`` /
    ``log_bar`` / ``log_pie`` / ``log_scatter`` / ``log_histogram``.

    :param entries: Single dict or list. Each: ``{run_id?, loggable_id, name, value, type?, step?, tags?}``. Default ``type`` is ``"line"``.
    :param run_id: Default run ID for entries that don't specify one.

``nebo_log_image``
    Log one or more images. Mirrors ``nb.log_image``. Each entry
    supplies either ``url`` (fetched server-side, persisted) or
    ``data`` (already-base64 bytes). Bytes are stored on the daemon so
    the run survives the source URL going stale.

    :param entries: Single dict or list. Each: ``{run_id?, loggable_id, name, url? | data?, step?, labels?}``.
    :param run_id: Default run ID.

``nebo_log_audio``
    Log one or more audio recordings. Same shape as ``nebo_log_image``
    plus an optional per-entry ``sr`` (sample rate, default 16000).

    :param entries: Single dict or list. Each: ``{run_id?, loggable_id, name, url? | data?, sr?, step?}``.
    :param run_id: Default run ID.

``nebo_log_text``
    Log one or more text entries. Mirrors ``nb.log_text``. ``name`` names
    the text stream (default ``"text"``); ``loggable_id`` defaults to
    ``"__agent__"`` when omitted.

    :param entries: Single dict or list. Each: ``{run_id?, loggable_id?, name?, message, step?}``.
    :param run_id: Default run ID.

Run Tree Tools
--------------

``nebo_get_tree``
    Get the run tree: ``groups`` (each with its doc filenames) and per-run
    placements. Runs absent from the placements map are at the root.

``nebo_create_group``
    Create a group (and any missing ancestors), e.g. ``vision/detr/lr-sweep``.

    :param path: Group path (required).

``nebo_move_group`` / ``nebo_delete_group``
    Rename/move a group subtree, or delete an empty one (409 if it has member
    runs or subgroups). :param path: (and ``new_path`` for move).

``nebo_move_run``
    Place a run in a group (empty string = root); auto-creates the target.

    :param run_id: The run (required).
    :param group: Destination group path (default: root).

``nebo_get_group_doc`` / ``nebo_set_group_doc``
    Read or write a group's markdown doc (default ``README.md``). Record what
    the group is, why/how the experiments ran, and the findings — citing runs
    with canonical ``nebo://run/<id>@<step>`` references.

    :param path: Group path (required).
    :param content: Markdown body (``set`` only, required).
    :param name: Doc filename (default ``README.md``).


Environment Variables
======================

SDK-side (read by ``nb.init()``):

``NEBO_URI``
    Destination for events. A path (file mode, default ``".nebo/"``) or an
    HTTP URL / ``host:port`` (network mode). Overrides the ``uri=`` arg.

``NEBO_RUN_ID``
    The current run identifier.

``NEBO_FLUSH_INTERVAL``
    Override the event flush interval (seconds).

``NEBO_QUIET``
    When set, suppresses the one-line startup banner.

``NEBO_NO_STORE``
    In file mode, opens no ``.nebo`` file — events are dropped. Used by
    the test suite. No effect in network mode.

``NEBO_API_TOKEN``
    Token sent on every network-mode request as the ``X-Nebo-Token``
    header. Required when the target daemon enforces auth.

Daemon-side (read by ``nebo serve``):

``NEBO_LOGDIR``
    The daemon's workspace root (watched dir, cache anchor, and ``meta/``
    home). Set automatically by ``nebo serve --logdir``.

``NEBO_REMOTE``
    A path (or ``1`` for the default ``<logdir>/remote/``): the daemon
    accepts network runs and persists them there. Set by
    ``nebo serve --remote``.

``NEBO_REMOTE_EPHEMERAL``
    When set, the daemon accepts network runs but persists none of them
    (RAM + cache only). Set by ``nebo serve --remote-ephemeral``. Mutually
    exclusive with ``NEBO_REMOTE``.

``NEBO_GROUP``
    Run-tree group path a run is born into — overrides
    ``nb.start_run(group=)`` and ``nb.init(group=)`` (useful for placing
    each child of a sweep without code changes).

``NEBO_NO_LOCAL``
    When set, the daemon's directory watcher is disabled (needs a remote
    flag). Set automatically by ``nebo serve --no-local``.

``NEBO_DAEMON_PORT``
    Used internally by the daemon process itself.

Daemon-side (read by ``nebo serve`` / the deployed Space):

``NEBO_API_TOKEN``
    When set, gates the API per the read/write modes below. Routes
    accept the token via ``X-Nebo-Token`` header or ``?token=…`` query
    parameter (the query form is for browser/iframe flows that can't
    set custom WebSocket headers). ``/health`` and the static UI
    bundle stay open in every mode.

``NEBO_READ_MODE``
    ``public`` (default) or ``private``. Gates GET requests when
    ``NEBO_API_TOKEN`` is set. The WebSocket handshake follows this
    mode.

``NEBO_WRITE_MODE``
    ``public`` or ``private`` (default). Gates non-GET requests when
    ``NEBO_API_TOKEN`` is set. Inbound WebSocket events follow this
    mode (unauthed events are silently dropped while the subscription
    stays open).

``NEBO_STORE_DIR``
    Directory the daemon writes ``.nebo`` files into (default:
    ``./.nebo``). Set to a persistent path (e.g. ``/data`` on Hugging
    Face Spaces) so runs survive container restarts.


.nebo File Format
==================

Nebo persists runs as append-only binary files using MessagePack.

.. code-block:: text

    [Header]
      magic: "nebo" (4 bytes)
      version: u16 big-endian (currently 1)
      metadata_size: u32 big-endian
      metadata: msgpack map {run_id, script_path, started_at, nebo_version, args}

    [Entry]*
      type: u8 (entry type index)
      size: u32 big-endian (payload size in bytes)
      payload: msgpack map (entry-specific data)

Entry types: metric (1), image (2), audio (3), node_register (4), edge (5), ui_config (8), text (9), progress (10), config (11), description (12), node_executed (13), run_start (15), run_completed (16), run_config (18), loggable_register (19). Code 0 is the legacy ``log`` entry — readers still accept it as a text entry, but writers emit ``text`` (9). Codes 6, 7, 14, and 17 are reserved (formerly error, ask, ask_response, pause_state — removed).

Media assets (images, audio) are embedded as raw bytes inside the msgpack payload, avoiding base64 overhead.
