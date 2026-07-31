.. _Guide:

Guide
#####

Humans may read this to get started. Agent skills are available to install from the nebo CLI.

This guide walks through building observable Python pipelines with Nebo, from basic to advanced usage.


Logging
=======

Nebo provides several logging functions.

Text
----

``nb.log_text(name, message, *, step=None)`` logs a text message to a named stream — text is a named payload stream like metrics and images, not a log console. The required ``name`` identifies the stream (the UI shows one card per stream name, and the stream appears in the Tracker tree); passing different names creates separate streams within the same loggable. Tensor-like objects (NumPy arrays, PyTorch tensors) are auto-formatted with shape, dtype, and statistics:

.. code-block:: python

    nb.log_text("status", "Starting training...")
    for epoch in range(10):
        loss = train_epoch(model, data)
        nb.log_text("epochs", f"Epoch {epoch}: loss={loss:.4f}")

.. raw:: html

    <iframe
        src="https://rsamf-nebo-demos.hf.space/?run=docs-guide-text-logs&text"
        width="100%" height="350"
        style="margin-top: 10px; border: 1px solid var(--color-border, #e5e7eb); border-radius: 8px;"
        loading="lazy">
    </iframe>

Metric charts
-------------

Nebo has one logging function per chart type — ``nb.log_line`` for
scalars over time, ``nb.log_bar`` and ``nb.log_pie`` for snapshot
distributions, ``nb.log_histogram`` for labeled distributions, and
``nb.log_scatter`` for labeled 2-D point clouds. The chart type locks
on first emission per ``(loggable, name)`` pair, so reusing a name
with a different ``log_*`` function raises ``ValueError``.

``log_line`` and ``log_scatter`` **accumulate** over time —
re-emitting with the same name appends to the series. Both auto-
increment ``step`` per ``(loggable, name)`` when omitted:

.. code-block:: python

    def train(model, data):
        for epoch in range(100):
            loss = train_epoch(model, data)
            accuracy = evaluate(model, data)
            nb.log_line("loss", loss)
            nb.log_line("accuracy", accuracy)

.. raw:: html

    <iframe
        src="https://rsamf-nebo-demos.hf.space/?run=docs-guide-log-line&metrics"
        width="100%" height="500"
        style="margin-top: 10px; border: 1px solid var(--color-border, #e5e7eb); border-radius: 8px;"
        loading="lazy">
    </iframe>

``log_scatter`` takes a labeled point dict and lets the UI toggle each
label on or off via the chip row above the chart. Repeated calls add
more points to the same plot, with each emission's points tagged with
the auto-incrementing step:

.. code-block:: python

    for i, (point, cluster) in enumerate(detections):
        nb.log_scatter("embed_2d", {cluster: [point]})  # step auto-advances

.. raw:: html

    <iframe
        src="https://rsamf-nebo-demos.hf.space/?run=docs-guide-log-scatter&metric=embed_2d"
        width="100%" height="500"
        style="margin-top: 10px; border: 1px solid var(--color-border, #e5e7eb); border-radius: 8px;"
        loading="lazy">
    </iframe>

``log_bar``, ``log_pie``, and ``log_histogram`` are **snapshots** —
every re-emission overwrites the prior value. They don't accept
``step`` or ``tags`` (those concepts apply to the accumulating
helpers).

``log_histogram`` accepts ``{label: list[number]}`` — every label is
its own distribution, all binned against a shared range so overlaps
line up. ``log_scatter`` and ``log_histogram`` also accept
``colors: bool = False``; setting ``colors=True`` distinguishes labels
by palette color (in addition to per-label shapes for scatter), but
is not recommended in comparison views where the palette is reserved
for run identity.

.. raw:: html

    <iframe
        src="https://rsamf-nebo-demos.hf.space/?run=docs-guide-snapshot-metrics&metrics"
        width="100%" height="500"
        style="margin-top: 10px; border: 1px solid var(--color-border, #e5e7eb); border-radius: 8px;"
        loading="lazy">
    </iframe>

Step filtering across panels
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

In the web UI, you can filter your view of data by:
 * clicking any datapoint on a line or scatter chart
 * entering a step directly in the Tracker controls (bottom panel)
 * stepping with the prev/next arrows or Ctrl/⌘+Left/Right

Use the Tracker's **Clear all filters** button to clear the filter.
Bar/pie/histogram are stepless and stay visible when the filter is active.

Images
------

``nb.log_image(image, name=None, step=None)`` accepts PIL images, NumPy arrays, or PyTorch tensors:

.. code-block:: python

    def augment(image):
        result = apply_transforms(image)
        nb.log_image(result, name="augmented")
        return result

.. raw:: html

    <iframe
        src="https://rsamf-nebo-demos.hf.space/?run=docs-guide-images&node=augment"
        width="100%" height="450"
        style="margin-top: 10px; border: 1px solid var(--color-border, #e5e7eb); border-radius: 8px;"
        loading="lazy">
    </iframe>

Audio
-----

``nb.log_audio(audio, sr=16000, name=None)`` logs audio data as NumPy arrays:

.. code-block:: python

    def synthesize(text):
        waveform = tts_model(text)
        nb.log_audio(waveform, sr=22050, name="speech")
        return waveform

.. raw:: html

    <iframe
        src="https://rsamf-nebo-demos.hf.space/?run=docs-guide-audio&audios"
        width="100%" height="300"
        style="margin-top: 10px; border: 1px solid var(--color-border, #e5e7eb); border-radius: 8px;"
        loading="lazy">
    </iframe>

Configuration
-------------

.. note::

    Only supported in decorated functions. Use ``nb.start_run`` to log global-level config.

``nb.log_cfg(cfg)`` logs configuration for the current node.

.. code-block:: python

    def train(lr=0.001, epochs=50):
        nb.log_cfg({"lr": lr, "epochs": epochs})
        ...

.. raw:: html

    <iframe
        src="https://rsamf-nebo-demos.hf.space/?run=docs-guide-log-cfg&node=train"
        width="100%" height="450"
        style="margin-top: 10px; border: 1px solid var(--color-border, #e5e7eb); border-radius: 8px;"
        loading="lazy">
    </iframe>

Multiple ``log_cfg()`` calls within the same node merge their dictionaries.


Progress Tracking
=================

``nb.track(iterable, name, total)`` wraps an iterable for tqdm-like progress tracking. The terminal dashboard and UI render a live progress bar:

.. code-block:: python

    def process(items):
        results = []
        for item in nb.track(items, name="processing"):
            results.append(transform(item))
        return results

If the iterable has a ``__len__``, the total is auto-detected. Otherwise you can pass ``total`` explicitly:

.. code-block:: python

    for batch in nb.track(dataloader, name="training", total=len(dataloader)):
        ...

Scopes
======

Global
------

All prior logging examples were writing to a ``"__global__"`` scope as calls made at module scope or from
non-decorated helpers land on the **Global loggable**.

The Global loggable appears as a distinct card at the top of the flat
view (labelled **"List"** on mobile) and is excluded from the DAG view
— it is not a node. Its tabs (Text, Metrics, Images, Audio) work
identically to any node's tabs.

Example:

.. code-block:: python

    import nebo as nb

    nb.log_text("status", "environment looks good")   # → Global
    nb.log_line("warmup_heartbeat", 1.0)       # → Global

.. raw:: html

    <iframe
        src="https://rsamf-nebo-demos.hf.space/?run=docs-guide-global-scope&flat"
        width="100%" height="500"
        style="margin-top: 10px; border: 1px solid var(--color-border, #e5e7eb); border-radius: 8px;"
        loading="lazy">
    </iframe>

Function
--------

Nebo also supports **function-level logging**, where log statements under the ``@nb.fn()`` decorator will land under its associated function's scope.

Example:

.. code-block:: python

    @nb.fn()
    def train():
        for batch in nb.track(dataloader, name="training", total=len(dataloader)): # → train
            loss = model(batch)
            nb.log_line("loss", loss) # → train



Decorating with ``@nb.fn()``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The ``@nb.fn()`` decorator is a core primitive. It registers a function for scope tracking in the pipeline DAG. A node only materializes (appears in the DAG) if the logging function executes. Edges are inferred from **data flow**: when a node's return value is passed as an argument to another node, an edge is created from the producer to the consumer.

.. code-block:: python

    import nebo as nb

    @nb.fn()
    def load_data():
        """Load raw data."""
        nb.log_text("status", "Loading data")
        return [1, 2, 3]

    @nb.fn()
    def transform(data):
        """Transform data."""
        nb.log_text("status", f"Transforming {len(data)} items")
        return [x * 2 for x in data]

    def run():
        records = load_data()
        result = transform(records)  # edge: load_data -> transform (data flow)
        return result

.. raw:: html

    <iframe
        src="https://rsamf-nebo-demos.hf.space/?run=docs-guide-fn-pipeline&dag"
        width="100%" height="450"
        style="margin-top: 10px; border: 1px solid var(--color-border, #e5e7eb); border-radius: 8px;"
        loading="lazy">
    </iframe>

The decorator can be used in several forms:

.. code-block:: python

    @nb.fn                       # bare (no parentheses)
    @nb.fn()                     # empty parentheses
    @nb.fn(depends_on=[setup])   # with explicit dependencies
    @nb.fn(ui={"color": "#34d399"})  # with per-node UI hints

DAG Strategy
~~~~~~~~~~~~

Switch how edges are inferred via ``nb.init(dag_strategy=...)``:

- ``"object"`` (default) — data-flow edges (``A → B`` when ``B``
  receives an argument produced by ``A``), with caller→callee as
  fallback.
- ``"stack"`` — caller→callee only; arguments ignored. Use when nodes
  share state through globals or class attributes.
- ``"both"`` — union of ``object`` and ``stack``. Busy but thorough.
- ``"linear"`` — chain nodes in first-execution order. Good for demos
  and notebooks.
- ``"none"`` — no auto edges; only ``depends_on=[...]`` adds them.

The same three-node script under ``dag_strategy="stack"`` — the
data-flow edge between ``load`` and ``transform`` vanishes; ``run``
fans out to both:

.. raw:: html

    <iframe
        src="https://rsamf-nebo-demos.hf.space/?run=docs-guide-dag-strategy-stack&dag"
        width="100%" height="450"
        style="margin-top: 10px; border: 1px solid var(--color-border, #e5e7eb); border-radius: 8px;"
        loading="lazy">
    </iframe>


Explicit Dependencies with ``depends_on``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Some dependencies cannot be detected automatically — shared mutable state, class attributes, closures, or global variables. Use ``depends_on`` to declare these explicitly:

.. code-block:: python

    @nb.fn()
    def setup():
        """Initialize shared resources."""
        nb.log_text("status", "Setting up")

    @nb.fn(depends_on=[setup])
    def process():
        """Uses resources initialized by setup."""
        nb.log_text("status", "Processing")

.. raw:: html

    <iframe
        src="https://rsamf-nebo-demos.hf.space/?run=docs-guide-depends-on"
        width="100%" height="400"
        style="margin-top: 10px; border: 1px solid var(--color-border, #e5e7eb); border-radius: 8px;"
        loading="lazy">
    </iframe>

``depends_on`` accepts a list of decorated functions or node ID strings. Explicit dependencies are added alongside any auto-detected data-flow edges.


.. note::

    With dag_strategy="object", Nebo tracks data flow via argument passing (``id()``-based return value tracking). Dependencies through shared mutable state, class attributes, closures, or global variables are **not** automatically detected. Use ``depends_on`` for these cases.


Decorating Classes
~~~~~~~~~~~~~~~~~~

``@nb.fn()`` can also be applied to a class. All methods are wrapped with scope tracking. The class itself is never a node — it serves as a visual grouping container (transparent bounding box) in the DAG.

.. code-block:: python

    import nebo as nb

    @nb.fn()
    class DataPipeline:
        def load(self):
            nb.log_text("status", "Loading data")
            return [1, 2, 3]

        def transform(self, data):
            nb.log_text("status", f"Transforming {len(data)} items")
            return [x * 2 for x in data]

        def save(self, data):
            nb.log_text("status", f"Saving {len(data)} items")

.. raw:: html

    <iframe
        src="https://rsamf-nebo-demos.hf.space/?run=docs-guide-class-grouping&dag"
        width="100%" height="450"
        style="margin-top: 10px; border: 1px solid var(--color-border, #e5e7eb); border-radius: 8px;"
        loading="lazy">
    </iframe>

In the DAG, ``DataPipeline`` appears as a transparent bounding box containing ``load``, ``transform``, and ``save`` as individual nodes.

**Scoping rules:**

- Every method gets its own scope. Text and metrics logged inside ``transform()`` are scoped to ``DataPipeline.transform``.
- Every method that runs materializes as a node, including silent methods that never call a log function — this keeps dependency chains in the DAG intact even when an intermediate method only orchestrates calls to other nodes.
- If a method also has ``@nb.fn()`` on it, a warning is issued (the decorator is redundant).
- A standalone ``@nb.fn()`` function called from within the class also appears inside the class group.
- A decorated method in an **undecorated** class is a regular standalone node with no bounding box.

Workflow Description
====================

``nb.md(description)`` sets a Markdown description for the overall workflow. This is visible in MCP tools and the terminal dashboard:

.. code-block:: python

    nb.md("""
    # Image Classification Pipeline

    Loads images from disk, runs inference with a pretrained ResNet,
    and exports predictions to a JSON file.
    """)

.. raw:: html

    <iframe
        src="https://rsamf-nebo-demos.hf.space/?run=docs-guide-workflow-md"
        width="100%" height="450"
        style="margin-top: 10px; border: 1px solid var(--color-border, #e5e7eb); border-radius: 8px;"
        loading="lazy">
    </iframe>

Calling ``nb.md()`` multiple times appends to the description.

``nb.md()`` is declarative: called outside a run (e.g. at module level)
it does not create a run — the description is script-level and applies
to every run the script opens, including runs from ``nb.start_run()``.
Called inside a run, it applies to that run only.


UI Configuration from Code
============================

``nb.ui()`` sets run-level UI defaults. The web UI reads these as defaults that the user can override:

.. code-block:: python

    nb.ui(
        layout="horizontal",     # or "vertical"
        view="dag",              # or "flat"
        minimap=True,            # show minimap
        theme="dark",            # or "light"
    )

Like ``nb.md()``, ``nb.ui()`` is declarative: outside a run it sets
script-level defaults applied to every run the script opens (no run is
created); inside a run it applies to that run only.

Per-node display hints can be set via ``@nb.fn(ui={})``. Supported keys:

- ``color`` (str) — accent color for the node's badge / border.
- ``default_tab`` (str) — which tab opens by default for the node. One of
  ``"info"``, ``"text"``, ``"metrics"``, ``"images"``, ``"audio"``. The user's
  clicks always override this preference.

Unknown keys are forwarded verbatim for forward compatibility with
future UI features.

.. code-block:: python

    @nb.fn(ui={"color": "#fb923c"})
    def data_loader():
        nb.log_text("status", "Loading data")
        ...

    @nb.fn(ui={"default_tab": "metrics"})
    def train(epochs=100):
        for step in range(epochs):
            nb.log_line("loss", compute_loss(step))

.. raw:: html

    <iframe
        src="https://rsamf-nebo-demos.hf.space/?run=docs-guide-ui-hints&dag"
        width="100%" height="450"
        style="margin-top: 10px; border: 1px solid var(--color-border, #e5e7eb); border-radius: 8px;"
        loading="lazy">
    </iframe>


Execution Modes
===============

Nebo has two transports, selected by the ``uri=`` argument to
``nb.init()`` (or the ``NEBO_URI`` environment variable):

File Mode (Default)
-------------------

When you run a script directly (``python my_pipeline.py``), nebo
writes events to ``./.nebo/<timestamp>_<run_id>.nebo`` — an
append-only file. No daemon is required. Point ``nebo serve --logdir
<dir>`` at the directory later to inspect runs in the web UI.

You can change the output directory explicitly:

.. code-block:: python

    import nebo as nb
    nb.init(uri="runs/today/")

Network Mode
------------

When the ``uri`` is an HTTP URL or a ``host:port`` pair, nebo streams
events to a running daemon over HTTP instead of writing files
locally:

.. code-block:: python

    import nebo as nb
    nb.init(uri="localhost:7861")
    # or
    nb.init(uri="https://my-space.hf.space", api_token="nb_...")


Persistent .nebo Files
=======================

In file mode the SDK writes ``.nebo/<timestamp>_<run_id>.nebo``
directly. To make a particular invocation a no-op (no file opened),
set ``NEBO_NO_STORE=1`` — used by the test suite.

A plain ``nebo serve`` is **local-only**: it watches ``--logdir`` and
*rejects* runs pushed over the network (the SDK raises
``nb.DaemonLocalOnlyError`` at ``init`` time so the misconfiguration
surfaces immediately). To host a daemon that accepts network runs, choose
how they're persisted:

.. code-block:: bash

    nebo serve --remote ./runs/       # accept + persist to ./runs/*.nebo
    nebo serve --remote-ephemeral     # accept, persist nothing (CI/demos)

The watcher (``--logdir``) and the ``--remote`` writer dir can't be the
same directory — the daemon refuses to start if they resolve to the same
path (nesting under the logdir is fine). Env mirrors: ``NEBO_REMOTE`` and
``NEBO_REMOTE_EPHEMERAL``.

To load a ``.nebo`` file into a *local* daemon for viewing and Q&A:

.. code-block:: bash

    nebo load path/to/run.nebo

To load a file into a *remote* daemon (e.g. one running on a
Hugging Face Space), pass ``--url``. The file is read locally and
its events are replayed through ``/events`` because the remote
daemon can't see your filesystem:

.. code-block:: bash

    nebo load path/to/run.nebo \
        --url https://username-space.hf.space \
        --api-token nb_…

``NEBO_URL`` and ``NEBO_API_TOKEN`` env vars work as defaults so
you don't have to repeat the flags.


Organizing Runs into Groups
===========================

Runs live in a filesystem-like tree of **groups** (e.g.
``vision/detr/lr-sweep``). A run is born into a group three ways, in
precedence order ``NEBO_GROUP`` > ``start_run(group=)`` > ``init(group=)``:

.. code-block:: python

    nb.init(group="vision/detr")                      # process default
    with nb.start_run(name="lr=3e-4", group="vision/detr/lr-sweep"):
        ...

For a sweep, set ``NEBO_GROUP`` per child process so the launcher places
each run without touching the code:

.. code-block:: bash

    NEBO_GROUP=sweeps/lr/run-3 python train.py

Reorganize and document groups from the CLI (or the MCP tools):

.. code-block:: bash

    nebo tree                                    # the whole tree
    nebo runs mv run_1748_0 vision/detr/lr-sweep
    nebo groups doc set vision/detr README.md --file findings.md

Each group holds markdown docs (``README.md`` renders first in the UI).
Docs support canonical ``nebo://`` references — ``nebo://run/<id>``,
``nebo://run/<id>/<loggable>/<stream>``, any run form suffixed with
``@<step>``, and ``nebo://group/<path>`` — that become clickable
navigation in the web UI (``?step=<n>`` is still accepted as a legacy
alias for ``@<step>``). Groups are a *virtual* tree over
run_ids; ``.nebo`` files never move. The tree persists in
``<logdir>/meta/tree.json`` (outside the disposable cache, so it survives
``nebo cache clear``). See :doc:`the CLI reference <cli>` for every command.


MCP Integration for AI Agents
===============================

Nebo includes 23 MCP tools that allow AI agents (like Claude) to
monitor, debug, query, organize, and *push data into* pipelines. To set
up MCP:

1. Start the daemon:

   .. code-block:: console

       $ nebo serve -d

2. Get the MCP config:

   .. code-block:: console

       $ nebo mcp

3. Add the printed config to your Claude Desktop or Claude Code MCP configuration.

The tools fall into three buckets:

- **Observation** — ``nebo_get_graph``, ``nebo_get_loggable_status``,
  ``nebo_get_text``, ``nebo_get_metrics``,
  ``nebo_get_description``, ``nebo_get_run_status``,
  ``nebo_get_run_history``.
- **Alerts & utility** — ``nebo_wait_for_alert``, ``nebo_list_alerts``,
  ``nebo_set_alert``, ``nebo_delete_alert``, ``nebo_load_file``.
  Alert rules fire on metric conditions (e.g. ``train/loss > 5``)
  without any code changes; pipelines start/stop via the user's shell.
- **Write** — ``nebo_log_metric``, ``nebo_log_image``,
  ``nebo_log_audio``, ``nebo_log_text``. These mirror the SDK's
  ``nb.log_*`` helpers so an external agent can push metrics, media,
  and text into a run without owning the SDK process. URL-based
  media is fetched server-side and persisted via the existing media
  path so runs stay self-contained even when the source URL goes
  stale.

An AI agent can use these to autonomously run experiments, diagnose
failures, patch code, ask questions about runs, and iterate.


Notebook Embedding via ``nb.show()``
======================================

In a Jupyter / IPython context, ``nb.show()`` returns an inline
``<iframe>`` pointing at the running daemon. The slice rendered is
inferred from which kwargs you pass — there is no ``view=``
discriminator. Pick at most one of ``metric`` / ``image`` / ``audio``
/ ``text`` / ``dag``; pass nothing for the full run dashboard.

.. code-block:: python

    import nebo as nb

    nb.show()                                  # full run
    nb.show(node="train")                      # single node detail
    nb.show(node="train", metric="loss")       # one metric, scoped to a node
    nb.show(metric=True)                       # gallery of all metrics
    nb.show(text=True)                         # text panel
    nb.show(text="status")                     # one named text stream
    nb.show(dag=True)                          # DAG only

Each call maps to a URL the iframe loads — the same query-param
scheme the dashboard accepts directly:

- ``nb.show()`` → ``?run=<id>``
- ``nb.show(node="t")`` → ``?run=<id>&node=t``
- ``nb.show(metric="loss")`` → ``?run=<id>&metric=loss``
- ``nb.show(metric=True)`` → ``?run=<id>&metrics``
- ``nb.show(image="hero.png")`` → ``?run=<id>&image=hero.png``
- ``nb.show(audio=True)`` → ``?run=<id>&audios``
- ``nb.show(text=True)`` → ``?run=<id>&text``
- ``nb.show(text="status")`` → ``?run=<id>&text=status``
- ``nb.show(dag=True)`` → ``?run=<id>&dag``

When the daemon enforces auth, append ``&token=…`` to the URL — the
dashboard captures it once on first load, persists it in
localStorage, and strips it from the visible URL via
``replaceState``.


Hosting on Hugging Face Spaces
================================

Nebo's daemon is the same FastAPI app whether it runs on your laptop,
in CI, or on a Hugging Face Space. ``nebo deploy`` bundles a
Docker-SDK Space, sets the necessary secrets, and gives you the
endpoint URL plus a token to share with the SDK.

Install the optional ``deploy`` extra and authenticate:

.. code-block:: bash

    pip install 'nebo[deploy]'
    huggingface-cli login           # writes a token write-scoped for your account

Deploy to a new (or existing) Space:

.. code-block:: bash

    nebo deploy --space-id <user>/nebo-test --from-source

The CLI prints the public URL and a randomly-generated
``NEBO_API_TOKEN``. Save it — the deploy won't show it again. Use
``--api-token <tok>`` to supply your own.

Connect the SDK from anywhere:

.. code-block:: python

    import nebo as nb

    nb.init(
        uri="https://<user>-nebo-test.hf.space",
        api_token="nb_…",
    )

    @nb.fn()
    def step():
        nb.log_text("status", "hello from a remote Space")
        nb.log_line("loss", 0.42)

    step()

Or set ``NEBO_URI`` / ``NEBO_API_TOKEN`` in the environment so the
same script works locally and remotely without a code change.

Access modes
------------

The deployed daemon defaults to **public reads, private writes** —
anyone with the URL can view runs in the dashboard, but only token
holders can push events or control runs. Override with the
``--read`` / ``--write`` flags:

.. code-block:: bash

    nebo deploy --space-id <user>/private-dash \
        --read private --write private \
        --from-source

=================  ====================  ======================
Mode               ``--read``            ``--write``
=================  ====================  ======================
Public dashboard   ``public`` (default)  ``private`` (default)
Private dashboard  ``private``           ``private``
Read-only mirror   ``public``            ``public`` (no SDK auth)
=================  ====================  ======================

Embed slices on a website
-------------------------

The same iframe URL scheme used by ``nb.show()`` works against the
deployed Space. Anything that renders HTML can host a live slice:

.. code-block:: html

    <iframe
        src="https://<user>-nebo-test.hf.space/?run=<id>&metric=loss"
        width="100%" height="600">
    </iframe>

A canonical ``nebo://`` reference also works as an embed target via
``?ref=…`` — a bare run ref renders the full dashboard, a loggable ref
renders that node's card:

.. code-block:: html

    <iframe
        src="https://<user>-nebo-test.hf.space/?ref=nebo://run/<id>/train"
        width="100%" height="600">
    </iframe>

For private dashboards, append ``&token=…`` once — the dashboard
caches it for subsequent visits.

Loading a local file into a deployed Space
------------------------------------------

If you have a ``.nebo`` file from a local run and want it visible on
the Space:

.. code-block:: bash

    export NEBO_URL=https://<user>-nebo-test.hf.space
    export NEBO_API_TOKEN=nb_…
    nebo load path/to/run.nebo --url "$NEBO_URL"

The events are read locally and replayed through ``/events`` on the
remote daemon (the daemon's ``POST /load`` only accepts server-side
paths, which don't help when the file is on your laptop).


Complete Example: Data Processing Pipeline
============================================

.. code-block:: python

    import numpy as np
    import nebo as nb

    nb.md("# Data Processing Pipeline\nGenerate, normalize, filter, and analyze data.")

    @nb.fn()
    def generate(num_samples: int = 200, noise: float = 0.1, seed: int = 42):
        """Generate synthetic signal data."""
        nb.log_cfg({"num_samples": num_samples, "noise": noise, "seed": seed})
        np.random.seed(seed)
        t = np.linspace(0, 4 * np.pi, num_samples)
        signal = np.sin(t) + noise * np.random.randn(num_samples)
        nb.log_text("status", f"Generated {num_samples} samples")
        return signal

    @nb.fn()
    def normalize(data, method: str = "standard", clip_min: float = -3.0, clip_max: float = 3.0):
        """Normalize and clip the signal."""
        nb.log_cfg({"method": method, "clip_min": clip_min, "clip_max": clip_max})
        if method == "standard":
            data = (data - data.mean()) / (data.std() + 1e-8)
        data = np.clip(data, clip_min, clip_max)
        nb.log_text("status", f"Normalized with method={method}, clipped to [{clip_min}, {clip_max}]")
        return data

    @nb.fn()
    def analyze(data):
        """Compute statistics on the processed data."""
        stats = {"mean": float(data.mean()), "std": float(data.std()), "n": len(data)}
        nb.log_text("stats", f"Stats: mean={stats['mean']:.4f}, std={stats['std']:.4f}")
        nb.log_line("mean", stats["mean"])
        nb.log_line("std", stats["std"])
        return stats

    def run():
        """Main entry point."""
        data = generate()
        normed = normalize(data)
        return analyze(normed)

    if __name__ == "__main__":
        result = run()
        print(result)

.. raw:: html

    <iframe
        src="https://rsamf-nebo-demos.hf.space/?run=docs-guide-complete-pipeline"
        width="100%" height="550"
        style="margin-top: 10px; border: 1px solid var(--color-border, #e5e7eb); border-radius: 8px;"
        loading="lazy">
    </iframe>


More Examples
=============

Runnable examples live in the `examples <https://github.com/graphbookai/nebo/tree/main/examples>`_ directory of the repository.