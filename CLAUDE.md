# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Nebo is a modern logging SDK for multi-modal data. Users decorate functions with `@nb.fn()` and emit events with `nb.log()` (text; optional `name=` param, default `"text"`), `nb.log_line` / `log_bar` / `log_pie` / `log_scatter` / `log_histogram` (one helper per chart type), `nb.log_image` (with `nb.labels.{Points, Boxes, Circles, Polygons, Bitmasks}` overlays), `nb.log_audio`, `nb.log_cfg`, `nb.alert`, and `nb.track()`. Nebo infers a DAG from the call graph and surfaces everything through append-only `.nebo` files, a FastAPI daemon, a React web UI, and MCP tools. The repo contains the Python package (`nebo/`), the web UI (`ui/`), tests (`tests/`), docs (`docs/`), and runnable examples (`examples/`).

Nebo is a **logging** SDK — it does not run human-in-the-loop interactive features (no `nb.ask`, no pauseable nodes). Anything that needs to block on user input belongs outside the SDK.

## Commands

### Python (package is managed with `uv`, Python ≥3.10)

```bash
uv sync --all-groups            # install deps + dev tools
uv run pytest tests/ -v         # full test suite (CI matrix: 3.10, 3.11, 3.12)
uv run pytest tests/test_decorators.py -v         # single file
uv run pytest tests/test_decorators.py::test_bare_decorator -v   # single test
uv run nebo --help              # CLI entry point (defined in nebo/cli.py: main)
uv run nebo serve               # start daemon (port 7861)
uv run python examples/basic_pipeline.py   # run a pipeline (SDK auto-connects to the daemon)
```

### Web UI (`ui/`)

```bash
cd ui
npm install
npm run dev      # Vite dev server, proxies /health /events /runs /graph /logs /nodes /stream → localhost:7861
npm run build    # tsc -b && vite build
npm run lint     # eslint
```

The dev UI only works when `nebo serve` is running on port 7861.

## Architecture

### Execution modes

Two transports share the same SDK surface, selected by the `uri=` arg
on `nb.init()` (or `NEBO_URI`):

- **File mode** (default, `uri=".nebo/"` or any path): the SDK writes
  events directly to an append-only `.nebo` file via
  `nebo/core/transport.py:FileTransport`. No daemon required. Each
  run lives at `<uri>/<timestamp>_<run_id>.nebo`.
- **Network mode** (`uri="http://…"` or `host:port`): the SDK pushes
  events to a long-lived FastAPI daemon (`nebo serve`, port 7861) via
  `nebo/core/client.py:NetworkTransport`. The daemon fans events out
  to the web UI (WebSocket at `/stream`) and MCP tools.

Mode is resolved by `nebo.core.uri.resolve_uri()` in `nb.init()`. URIs
starting with `http(s)://` or matching `host:port` are network; anything
else is a directory path. `_ensure_init()` lazily auto-initializes on
first `nb.*` call, so pipelines never need an explicit `nb.init()`.

Daemon side: `nebo serve` watches `--logdir` (default `./.nebo`) for
files written by SDK file-mode runs and ingests them as they grow
(`nebo/server/watcher.py:DirectoryWatcher`). `--logdir` is the
**workspace root** in every mode — it anchors the cache identity, the
`meta/` tree, and the default remote dir. `--no-local` only disables the
watcher (the logdir still anchors everything); it errors unless a remote
flag is also given, since the daemon would then ingest nothing.

**Daemon persistence modes** (`DaemonState.mode`, reported on `/health`):

- **local** (default, plain `nebo serve`): watcher only. Network run
  creation is **rejected** — `POST /events` for a `run_start` or an
  unknown run_id returns `409 {"error": "daemon_local_only"}` whose
  detail names both remote flags (`_LOCAL_ONLY_ERROR`). Annotating a run
  the watcher already knows is still accepted.
- **remote** (`--remote [DIR]`, default `<logdir>/remote/`): accepts
  network runs and the daemon writes them as `.nebo` files in `DIR`
  (per-run `NeboFileWriter`, opened on `run_start`, closed on
  `run_completed`/shutdown). Media the daemon writes is stored by
  `(src_path, offset, length)` reference into that file — blob rows are
  now an ephemeral-only fallback.
- **remote-ephemeral** (`--remote-ephemeral`): accepts network runs but
  persists nothing (RAM + disposable cache only) — for CI, demos, tests.
  A bare `DaemonState()` (tests, embedders) defaults to this mode.

`--remote` and `--remote-ephemeral` are mutually exclusive; a remote dir
may not equal `--logdir` (watcher/writer feedback), though nesting under
it is fine (the watcher is non-recursive). The equality check is enforced
in **both** `cli.py:cmd_serve` (flag form) and `create_daemon_app` (env
form — covers the Dockerfile's `uvicorn --factory` launch and embedders);
with the watcher off (`--no-local`) the combination is allowed. Env
mirrors: `NEBO_REMOTE` (path, or `1` for the default dir),
`NEBO_REMOTE_EPHEMERAL`. There is no `--save-files` (removed).

**Ingest source gating**: `ingest_events`/`_process_event` carry the batch
source (`"network"` vs `"watcher"`). Only network events pass through a
remote-mode `_file_writer` (watcher/loaded events came *from* a file —
re-writing them would duplicate entries or feed the watcher its own
output), a non-network `run_start` never opens a writer, and a watcher
batch for a run whose `source == "network"` is dropped with a once-per-run
warning (a `.nebo` file in the watched logdir that aliases a network run —
e.g. a stray copy — must not double RAM state). `POST /load` ingests with
`source="watcher"`.

**SDK fail-fast**: `NetworkTransport.connect()` reads `/health.mode`; a
`"local"` daemon makes `nb.init()` (or the first `nb.*` emit) raise
`nb.DaemonLocalOnlyError` immediately instead of buffering events that
will never be accepted. A mid-run `409 daemon_local_only` (daemon
restarted into local mode) is treated as fatal — the transport stops
retrying, drops its buffer, and logs once — whereas ordinary connection
failures keep the retry-forever-with-backoff behavior.

### Daemon SQLite cache & RAM eviction

`.nebo` files remain the sole source of truth; the daemon keeps a
**disposable, rebuildable** SQLite cache (`nebo/server/cache.py:RunCache`,
default `~/.nebo/cache/<sha1(logdir)[:16]>.db`, WAL) written *behind* the
RAM ingest path by a single writer thread (typed ops, one transaction per
~0.25 s batch, `flush()` barrier). Ingest hot path is unchanged — RAM
writes stay synchronous; the cache interaction is a `queue.put`.

- **Read routing**: every endpoint goes through `DaemonState.run_*`
  accessors. RAM serves a run only while it is resident with
  `Run.ram_complete=True`; evicted/demoted runs read from SQL. Never read
  `state.runs` directly in an endpoint.
- **Eviction janitor** (60 s lifespan task, no-op without a cache): a
  single idle threshold — idle > `EVICT_IDLE_S` (30 min) → evict from RAM
  (there is no completed/crashed distinction; a resumed run just rehydrates
  from the cache); resident points over budget (`--ram-budget`, default
  384 MB at 372 B/point) → evict runs idlest-`last_event_at` first; a
  single recently-active run that alone exceeds budget is *demoted* —
  read-state dropped, ingest-state (type locks, counters, edge dedup)
  kept, `ram_complete=False`, one-way.
- **Rehydration**: ingest for a run_id absent from RAM but present in the
  cache rebuilds ingest-state only (`_rehydrate_run`) — no duplicate runs
  after restarts, reads stay on SQL.
- **Media**: decoded once at ingest to bytes; `media_id =
  sha256(bytes)[:16]` (content-addressed, stable across restarts). Bytes
  live in a byte-budgeted LRU (`--media-lru`, default 256 MB) and durably
  either as `(src_path, offset, length)` refs into the `.nebo` file
  (watcher runs, and remote-mode runs that the daemon writes itself) or
  blob rows (`--remote-ephemeral` runs, which have no file to reference).
  `GET /runs/{id}/media/{media_id}` returns **raw bytes** with sniffed
  Content-Type, `ETag: media_id`, `Cache-Control: immutable` (304 on
  If-None-Match); the UI points `<img>/<audio>` straight at it.
- **Shallow ingest**: the watcher registers an unknown file by reading only
  its **header** (synthesizing a `run_start` so the run lists via the normal
  ingest/cache/WS path), then freezes the file's size as a baseline. A static
  historical file **stays shallow** (its body is never read) — cold-starting on
  1000 runs costs ~1 KB each, not a full replay. The body is deep-ingested
  lazily: when the file grows (a live run — `_deepen`) or on the first detail
  read of that run (`DaemonState.ensure_deep` → the watcher, under a per-run
  lock, called by every run-scoped read endpoint but **never** the run list).
  Deep ingest is chunked (`_INGEST_CHUNK`) so a huge file isn't buffered whole.
  The `shallow` flag lives in `watch_files`, so restarts keep shallow files
  shallow. This is possible with no file-format change because there is no
  `ended_at` — the header alone fully populates a run-list row.
- **Watcher offsets persist** in the cache (`watch_files` table): daemon
  restarts resume tailing instead of replaying. Reads use
  `NeboFileReader.read_entries_incremental`, which parks at a torn tail
  frame — offsets only advance past complete entries.
- **Single owner**: `RunCache.start()` takes an exclusive flock on
  `<db>.lock` and raises `CacheLockedError` if another live process holds
  it — two daemons sharing one cache duplicate history rows and clobber
  each other's `watch_files` offsets. `nebo serve` pre-probes the lock
  (`cache_lock_holder`) for a friendly error naming the holder pid. The
  kernel drops a flock on process death, so crashes never wedge a restart.
- **Idempotent history inserts**: `logs`/`metrics`/`media`/`alerts`/
  `significant_events` carry unique indexes over one event's identity
  (COALESCE sentinels for nullable step/ts — NULLs are distinct in SQLite
  unique indexes) and insert with `OR IGNORE`, so re-ingesting
  already-cached events (re-scanned file, replayed batch) is a no-op —
  matching the upsert semantics of `runs`/`loggables`/`media_blobs`.
- **Escape hatches**: `--no-cache` (pure-RAM daemon, janitor disabled) and
  `--cache-path`. A `DaemonState()` constructed directly (tests) has
  `cache=None` and behaves exactly like the pre-cache daemon. Stale cache
  dbs are swept at startup after `--cache-retention-days` (default 30).
  `nebo cache ls|clear` manages the cache dir from the CLI.
- Env mirrors of the flags: `NEBO_CACHE_PATH`, `NEBO_NO_CACHE`,
  `NEBO_RAM_BUDGET_MB`, `NEBO_MEDIA_LRU_MB`, `NEBO_CACHE_RETENTION_DAYS`.

Env vars: `NEBO_URI` overrides the constructor arg.
`NEBO_RUN_ID`, `NEBO_FLUSH_INTERVAL`, `NEBO_API_TOKEN` are unchanged.
`NEBO_QUIET=1` suppresses the startup banner.
`NEBO_NO_STORE=1` makes file mode a no-op (used by the test suite).

Two process-wide escape hatches let headless contexts (CI, embedders,
tests) suppress side-effects:
- `NEBO_NO_STORE=1` — SDK file mode opens no file; events are dropped.
- A `--remote-ephemeral` daemon accepts network runs but persists none of
  them (RAM + disposable cache only).

### Run tree (groups)

Runs organize into a filesystem-like hierarchy of **groups** — a *virtual*
tree over run_ids (`.nebo` files never move; the physical layout stays flat).
`nebo/server/tree.py:TreeStore` owns it, persisted to `<logdir>/meta/tree.json`
(the workspace root's `meta/`, **outside** the disposable cache, so it survives
`nebo cache clear`). The daemon holds it in RAM and rewrites the whole tiny JSON
atomically (tmp + fsync + `os.replace`) on every mutation, guarded by a
`threading.Lock` (mutations come from both async endpoints and the sync ingest
seed). Group docs are real markdown files under `meta/docs/<group-path>/`.

- **Single placement store, seed-once.** `tree.json`'s `runs` map (run_id →
  group) is the *only* placement store — no birth-placement fallback, no
  override layer. The `group` recorded at run start (SDK `run_start` data, and
  the `.nebo` header for shallow watcher runs) only *seeds* the map **if the
  run_id is absent** (`TreeStore.seed_run`). Because `tree.json` survives cache
  clears, a moved run stays moved when its file is re-scanned — the header never
  re-wins. This is the load-bearing invariant (`test_move_survives_rescan`).
- **Group paths** (`nebo/core/groups.py:validate_group_path`, shared SDK+daemon)
  are `/`-delimited, no `.`/`..`/reserved chars, depth ≤ 16. SDK: `nb.init(group=)`
  / `nb.start_run(group=)` / `NEBO_GROUP` (env > start_run > init), validated at
  the call site.
- **HTTP**: `GET /tree` (payload filtered to known runs — dangling placements
  and placements to deleted groups read as root), `POST /groups`, `PATCH/DELETE
  /groups/{path:path}` (docs routes declared **before** the catch-all so
  `{path:path}` doesn't swallow them), `PUT /runs/{id}/group`, and
  `GET/PUT/DELETE /groups/{path:path}/docs/{name}`. `MessageType.TREE_UPDATED`
  broadcasts the full tree over WS after every mutation (and after an ingest
  seeds a group). Deleting a group with known member runs or subgroups → 409
  (`TreeConflict`); nebo has no run deletion, so groups can't sneak one in.
- **Surfaces**: CLI `nebo tree` / `groups add|ls|mv|rm` / `groups doc
  ls|get|set|rm` / `runs mv`; MCP `nebo_get_tree` / `nebo_{create,move,delete}_group`
  / `nebo_move_run` / `nebo_{get,set}_group_doc`. Group docs support `nebo://`
  deep links (`nebo://run/<id>?step=<n>`, `nebo://group/<path>`) that the UI
  intercepts. Agents are the primary doc authors — the skills carry the
  what/why/how/findings curation contract.
- **UI is read-only** for the tree (no write-access model for UI users): the
  sidebar renders a collapsible group tree (`ui/src/components/runs/RunTree.tsx`)
  over the store's `runTree` slice (`{groups, runs}`), hydrated from `GET /tree`
  and replaced wholesale on `tree_updated`. A group page
  (`components/layout/GroupPage.tsx`, shown when `selectedGroup` is set) renders
  the group's docs (README first) then member runs. `nebo://` links are handled
  by `components/shared/NeboMarkdown.tsx` (a `urlTransform` that passes `nebo:`
  through the v10 sanitizer + a custom `a` renderer → `store.navigateNebo`).
  Reorganization happens through the CLI/MCP, never the UI.

### DAG inference

Edges are inferred at runtime, not declared. `nebo/core/decorators.py` wraps every `@nb.fn()` call; `nebo/core/dag.py` and `nebo/core/state.py` track which node produced each return value (`return_origins`) and which node is currently on the call stack. When a wrapped callee receives an argument that was produced by another node, a data-flow edge is added; otherwise the edge falls back to the calling parent. `depends_on=[...]` declares edges that can't be inferred (shared state, globals, class attrs). `dag_strategy` switches between `object` (data-flow, default), `stack` (caller→callee only), `both`, `linear` (chain nodes in first-execution order), or `none`.

### .nebo format v4 + transport coalescing

`fileformat.py`'s module docstring is the format spec of record. v4 adds:

- **`metric_batch` (entry code 20)** — a columnar batch of accumulating
  (line/scatter) points: parallel `steps`/`timestamps`/`values` arrays with
  whole-batch `tags`/`colors`. **Equivalence rule:** a batch of length N ≡ N
  consecutive `metric` events with the shared fields copied onto each
  (`nebo/core/coalesce.py:expand_metric_batch` is the inverse). Plain
  `metric` events stay legal; snapshot types are never batched.
- **Media as raw bytes.** `log_image`/`log_audio` put PNG/WAV bytes in
  `event["data"]` (msgpack bin on disk, ~25% smaller). Base64 exists only at
  the JSON wire boundary (`NetworkTransport._jsonable`); the daemon accepts
  both str and bytes.

Batching happens in the **transports**, not the logger: both flush loops
drain their queue per tick and run `coalesce()` (`nebo/core/coalesce.py`) —
same-series points group per `(loggable_id, name)`, cut on any
`(metric_type, tags, colors)` change or at `MAX_BATCH_POINTS=5000`;
singletons pass through as plain `metric`. The coalescer also keeps only
the **last** snapshot metric per series and the last `progress` event per
loggable within a window (they overwrite on ingest anyway), and folds
`node_executed` ticks into one event per `(loggable, caller)` carrying
`data.count` (absent = 1; daemon and UI add the delta). Per-series order
is preserved; cross-type order within a flush window is best-effort
(timestamps are authoritative). Coalescing is an optimization, never
required — every consumer (daemon `_process_event`, UI `processWsEvents`,
readers) accepts both shapes, so degraded paths may skip it. FileTransport
also flushes the stream once per drain tick (not per entry) and its
`flush()` is a barrier event, not a queue-empty poll. Measured: ~21 B/point
on disk (was 116), ~156k scalar events/s end-to-end in file mode (was ~91k).

### SDK write-path costs (deferred media, throttles, backpressure)

- **Media encoding is deferred.** `log_image`/`log_audio` only validate
  (TypeError still raises at the call site) and copy on the caller thread
  (~9 ms for a 1080p frame, was ~300 ms); `event["data"]` holds a
  `PendingMedia` (`nebo/logging/serializers.py`) that PNG/WAV-encodes in
  the transport flush thread via `resolve_media`. Encode failures log +
  drop (background threads can't raise). `serialize_image/serialize_audio`
  are eager wrappers over the same `prepare_*` path.
- **`nb.track` throttles wire emissions** to one per `min_interval`
  (default 0.1 s; first and final always emit). Local `node.progress`
  still updates every iteration.
- **`track_return` doesn't pin user data**: weakrefs where the type
  supports them (arrays/tensors/objects); non-weakrefable builtins keep
  strong refs in a 4096-entry recency window (`RETURN_ORIGINS_MAX`).
- **Network wire is msgpack**: `NetworkTransport` POSTs
  `application/msgpack` bodies — a concatenation of individually-packed
  event maps (daemon splits with `msgpack.Unpacker`; JSON bodies still
  accepted for MCP/CLI writers). Each event packs exactly once
  (`_prepare_packed`); chunking sums pre-encoded sizes; media bytes ride
  natively (no base64 anywhere). Keep-alive `http.client` connections are
  **per-thread** (`_conn_local`) — the flush loop and explicit `flush()`
  post concurrently and `http.client` is not thread-safe.
- **Shutdown drains to completion, progress-based.** Deferred encoding
  means seconds of backlog can legally exist at exit, so neither
  transport uses a fixed drain deadline. `FileTransport.close()` (and
  the atexit hook, which now *always* closes — only the duplicate
  `run_completed` emission is guarded) waits while the worker makes
  progress and gives up only after `NEBO_SHUTDOWN_TIMEOUT` (default
  10 s) with zero events resolved/written — then warns to stderr and
  leaves the stream open rather than closing it under the worker; if
  the worker died, the residue is drained synchronously.
  `NetworkTransport._flush_remaining` drains the fallback buffer too
  (exit-while-disconnected events get a final send attempt and are
  counted in the drop warning), and `disconnect()` joins the flush loop
  long enough to cover one in-flight POST. Don't reintroduce fixed
  short timeouts on these paths — that's the v0.3.0 missing-logs bug.
- **Backpressure**: `send_event` charges an approximate byte budget
  (`NEBO_BUFFER_BUDGET_MB`, default 128) across queue+buffer+fallback;
  over budget, non-structural events drop (progress first at 90%) with a
  one-time warning + shutdown summary. `STRUCTURAL_TYPES` (run_start,
  run_completed, loggable_register, edge, node_executed, …) always get
  through. While disconnected, the flush loop retries `connect()` with
  1 s → 30 s backoff forever and replays the fallback buffer on success.
- **WS broadcast never blocks ingest**: the daemon serializes each batch
  once and enqueues it into bounded per-client queues (`_WsClient`,
  256 batches, drop-oldest); per-client sender tasks own the sockets.

### Metrics model (line + scatter accumulate, bar/pie/histogram snapshot)

Two chart types accumulate — `log_line` and `log_scatter`. The other three (`log_bar`, `log_pie`, `log_histogram`) are snapshots: the SDK still sends an event over the wire on each call, but the daemon (`nebo/server/daemon.py` metric handler) and the UI store (`ui/src/store/index.ts:appendMetric`) **overwrite** the prior entry instead of appending. The chart type locks on first emission per `(loggable, name)` pair via `SessionState._metric_cursors[loggable][name]` (a `MetricCursor(type, next_step)` — that's the only metric metadata the SDK keeps in process, by design).

Per chart type:

- `log_line(name, value, *, step=None, tags=None)` — accumulating; `step` auto-increments via the cursor; `tags` partition emissions for the UI tag-chip filter.
- `log_bar(name, value)`, `log_pie(name, value)` — `value` is `{label: number}`; no step, no tags. Re-emitting overwrites.
- `log_scatter(name, value, *, step=None, tags=None, colors=False)` — accumulating; each emission's `value` is `{label: list[(x, y)]}` (stored on the wire as `{label: {"x": [...], "y": [...]}}`). The chart shows the union of all emissions' points; `step` auto-increments per emission. UI varies labels by shape (`shapeForLabel`).
- `log_histogram(name, value, *, colors=False)` — `value` is `{label: list[number]}`. UI bins all labels against a shared min/max so overlapping distributions line up. Bin count and per-label EMA smoothing are global settings (see "Chart settings" below).
- `colors=False` (default) draws labels in the run color; `colors=True` switches to `RUN_COLOR_PALETTE`. Document warning: not recommended in comparison views, where the palette is reserved for run identity.

Step/tags only flow on the wire for accumulating types (line, scatter). `_emit_metric` in `nebo/logging/logger.py` strips them to `None`/`[]` for snapshot types (bar/pie/histogram) so stale values can't leak.

Step filter: clicking a datapoint on a line or scatter chart sets `timeline.step` (and auto-flips `timeline.mode` to `'step'`) via the chart's `onClick`. `useTimelineFilter` (`src/hooks/useTimelineFilter.ts`) then narrows logs/images/audio panels to entries whose `step` matches; metric charts ignore the entry-level filter and instead mark the active step inline (LineMetric draws a vertical guideline + value bubble via an inline chart.js plugin; ScatterMetric dims non-matching points). **Don't filter metric entries by step at the parent level** (e.g. in `LoggableGridView.MetricCardBody`) — doing so collapses the chart and breaks the in-chart highlight.

**Tracker (bottom panel).** The bottom of the UI is the **Tracker** — a full-width, resizable and collapsible panel that replaces the old timeline scrubber. It is built around **streams**: a stream is a named series of datapoints within a loggable. Full stream paths are `/<func_name>/<name>` for `@nb.fn` nodes, `/agent/<name>` for the `__agent__` loggable, and `/<name>` (root) for the global loggable. `nb.log()` entries are streams named `"text"` by default (or whatever `name=` was passed). Names split on `/` to form a searchable tree. Key sub-components (`ui/src/components/timeline/`):

- `StreamTree.tsx` — desktop-only left pane: a searchable `/`-delimited tree of text/image/audio streams (metrics are NOT in the tree), capped at 15% of the tracker width. Clicking a leaf highlights it (`timeline.selectedStream`) and scrolls the main view to that loggable's card — it does **not** filter the content panels. On mobile the tree is hidden and each stream's full path is drawn left-aligned on its canvas row instead (dimmed to 30% while the user is touching the canvas).
- `TrackerControls.tsx` — Step/Time mode dropdown, numeric step input, prev/next step arrows (also Ctrl/⌘+Left/Right), a **Reset zoom** icon button, a **Clear all filters** button, and modality chips (text/image/audio). No play/pause. Below 768px these fold into a single **Filters** popover (which also holds the stream search).
- `TimelineGrid.tsx` — per-stream datapoint rows with a single playhead for both step and time modes (time mode is a single playhead, not the old two-handle range). Left-drag scrubs the playhead; **zoom is ctrl/⌘+wheel (trackpad pinch)**, pan is middle-drag or shift/horizontal wheel, and a plain vertical wheel scrolls the row list. A constant horizontal pad keeps the first/last tick and edge datapoints from clipping; the playhead carries a downward triangle handle. `ticks.ts` holds the tick-generation helper.
- `Tracker.tsx` — top-level shell that owns the single shared vertical scroll (so the tree column and canvas render one flattened row list at matching heights and scroll together, staying row-aligned), plus collapse/search state, drag-to-resize, the collapse toggle, and the mobile flat-label rendering.

Supporting modules: `ui/src/lib/streams.ts` (stream path + tree-flatten helpers), `ui/src/hooks/useStreams.ts` (stream data hook), `ui/src/hooks/useAxisTransform.ts` (zoom/pan math via a native non-passive wheel listener). The store `timeline` slice is `{ mode, step, time, selectedStream }`.

The old `ui/src/components/timeline/TimelineScrubber.tsx` was removed.

UI perf: line charts (`LineMetric`, `ComparisonLine`) run with
`parsing: false` + sorted `{x, y}` data and Chart.js **LTTB decimation**
(500 samples past 1000 points, re-decimated on zoom) — keep data sorted by
x and don't reintroduce `parsing: true` there. The tracker dedupes
datapoints per row by quantized x (bucket resolution scales with zoom) and
scrub commits to the store at most once per animation frame. `useStreams`
keeps a per-run incremental accumulator (only newly-appended logs are
walked per WS batch) and computes nothing while the tracker is collapsed.
Run-list/status polls pause while `document.hidden`.

UI invariant: chart components index palette colors by `allLabels.indexOf(label)` (the full vocabulary), never by the iteration index over the filtered list — otherwise toggling a label off via the chip row reshuffles the remaining colors. `ScatterMetric` and `HistogramMetric` both follow this rule.

UI invariant: chart components must NOT early-return `null` for empty data while their canvas is conditionally mounted by a parent that re-mounts on data changes. `useChartJs`'s mount effect uses `[]` deps; if the canvas remounts, the Chart instance keeps a reference to the old detached canvas and subsequent `chart.update()` calls are invisible. Always render the canvas (an empty Chart.js plot is fine); guard via the parent component's "no data" placeholder if needed.

### Image labels (`nb.labels.*`)

`nb.log_image` accepts geometric overlays only as instances of the dataclasses in `nebo/labels.py`: `Points`, `Boxes`, `Circles`, `Polygons`, `Bitmasks`. Each pairs raw geometry (list / ndarray / tensor) with a CSS color string. Each kwarg on `log_image` accepts a single instance OR a `list[Class]` so one image can carry multiple groups of the same kind in different colors (e.g. predictions vs. ground truth boxes). Raw lists/tensors are rejected with a `TypeError` pointing at the matching `nb.labels.*` class — there is no backwards-compat path. `_serialize_labels` in `nebo/logging/serializers.py` flattens each kwarg to `[{data, color}, ...]` on the wire; `ImageWithLabels.tsx` renders each group with its own color.

`Polygons` carries an extra `fill: bool = True` flag (filled interior vs. outline only) which lands as `{data, color, fill}` on the wire. No other label kind has an analogue.

Bitmasks are tinted via CSS `mask-image` + `background-color` + `mask-mode: luminance`. The server emits a single-channel grayscale PNG (PIL mode `"L"`) with no alpha channel; without `mask-mode: luminance` the browser would default to `mask-mode: alpha` and treat every pixel as opaque, flooding the entire image with the group color.

Note the kwarg is `bitmasks=` (plural) — matches the dataclass name and parallels the other four kinds.

### Chart interactivity (zoom/pan/reset, smoothing, tag mute)

`ui/src/components/charts/zoomBindings.ts` is the shared zoom/pan glue, used by both `LineMetric` and `ScatterMetric`. The interaction model is fixed:

- Left click (no drag) → existing `onClick` (sets timeline step). Never overridden.
- Mouse wheel (integer `deltaY`, no `deltaX`) → zoom (chartjs-plugin-zoom).
- Trackpad pinch (`wheel` event with `ctrlKey`) → zoom.
- Middle-mouse drag → pan. Plain left-click drag is gated off via `pan.onPanStart` returning `false` unless `event.button === 1`.
- Trackpad two-finger drag → pan via a custom capture-phase wheel listener (`attachWheelHandler`) that detects fractional/horizontal `deltaY` with no `ctrlKey` and routes to `chart.pan()` before the plugin sees the event.

Line charts pan/zoom on `'x'` only (Y is locked); scatter is `'xy'`. Both expose a "Reset zoom" button calling `chart.resetZoom()`. Stroke and point pixel sizes are fixed; do not introduce data-driven sizing — that breaks the constant-size-during-zoom contract.

### Chart settings (global, in `Settings`)

`ui/src/store/index.ts:Settings` carries three global chart knobs surfaced in `SettingsPanel.tsx`:
- `lineSmoothing: number` (0–1, EMA factor) — `LineMetric` runs an EMA over each dataset's data at render time.
- `histogramSmoothing: number` (0–1, EMA factor) — `HistogramMetric` smooths bin counts per-label after binning.
- `histogramBinCount: number` — replaces the previously-hardcoded bin count. `DEFAULT_HISTOGRAM_BIN_COUNT` is exported from the store.

Smoothed values are rendered, not persisted: raw entries in the store remain untouched.

### Tag mute on line charts

`LineMetric` accepts `tags?: string[]` and `inactiveTags?: Set<string>`. When `tags` is provided, the chart renders one dataset per tag (each tag's emissions filtered by `entry.tags.includes(tag)`; untagged entries map to `UNTAGGED_KEY`). Tags in `inactiveTags` render in soft gray (`rgba(156, 163, 175, 0.45)`) instead of being filtered out. `NodeMetrics::MetricBlock` now passes `entries` + `allTags` + `inactiveTags` for line metrics instead of pre-filtering with `entriesMatchingTags()` — that helper is still used for non-line types.

### Alerts (code-fired + condition rules)

`nb.alert(title, text, level)` fires a webhook (if configured) and emits an `alert` wire event stamped `triggered_by: "code"`; the daemon appends it to `run.alerts`. **Alert rules** are set without code changes (`nebo alerts set --condition "train/loss > 5"`, MCP `nebo_set_alert`): they live in `DaemonState.alert_rules` (in-memory) and are evaluated in `_process_event`'s metric branch (`_evaluate_alert_rules`) against numeric metric values only. A rule fires at most once per run; the fired alert lands in `run.alerts` with `triggered_by: "cli"` plus a `condition` display string (and is cache-persisted via `_fire_rule_alert`), so `/runs/{id}/alerts/wait` (`nebo runs wait`, `nebo_wait_for_alert`) wakes on it with no extra wiring. Rule CRUD: `GET/POST /alerts`, `GET/DELETE /alerts/{id}`; condition strings (`<metric> <op> <number>`) are parsed by `nebo/client.py:parse_condition`. `GET /runs/{id}/alerts` lists one run's fired alerts (code- and rule-fired) — the mobile alerts sheet hydrates from it, and live `alert` WS events append to the store's per-run `alerts` slice. The metric name `last_event` is **reserved for heartbeat rules** (`"last_event > 60"` = fire once the run has been idle 60 s — nebo's run-completion signal): evaluated by an always-on ~1 s lifespan task (`evaluate_heartbeat_rules`, `HEARTBEAT_TICK_S`) that notifies `_event_notify` itself (a quiet run has no ingest to wake waiters); ops `>`/`>=` only, no `loggable_id`; run-scoped rules fall back to the cache for RAM-evicted runs and fire immediately for already-idle runs, global rules skip runs whose last activity predates the rule. File-mode `alert` entries are intentionally **not** in `ENTRY_TYPES` (byte 255); the watcher's payload-type recovery ingests them — don't make the type byte authoritative.

### Global state singleton

`SessionState` in `nebo/core/state.py` is a threadsafe singleton holding nodes, edges, the daemon client, the terminal display, and per-run snapshots (`_run_snapshots`). `_current_node` is a `ContextVar` so concurrent node scopes work under threads. When working on anything involving run lifecycle, read `state.py` first — `save_run_state` / `restore_run_state` / `clear_run_state` are the primitives `start_run()` uses to multiplex multiple runs in one process.

### Package layout

- `nebo/core/` — decorators, DAG builder, session state, `DaemonClient`, config, tracker, `.nebo` file format, `groups.py` (`validate_group_path` — shared SDK/daemon group-path validation).
- `nebo/logging/` — user-facing `log`/`log_line`/`log_bar`/`log_pie`/`log_scatter`/`log_histogram`/`log_image`/`log_audio`/`md`, plus the serializer/queue that batches events to the daemon.
- `nebo/labels.py` — public dataclasses (`Points`, `Boxes`, `Circles`, `Polygons`, `Bitmasks`) for `nb.log_image` overlays. Re-exported as `nb.labels`.
- `nebo/server/` — `daemon.py` (FastAPI app, created via `create_daemon_app` factory), `cache.py` (`RunCache` write-behind SQLite cache, `MediaLRU`, `media_id_for`, cache-path/sweep helpers), `watcher.py` (directory watcher with persisted offsets + shallow header-only registration), `tree.py` (`TreeStore` — run-tree groups/placements/docs over `meta/tree.json`), `runner.py` (vestigial subprocess manager), `protocol.py` (`MessageType` enum + `decode_batch`).
- `nebo/mcp/` — MCP tools (`tools.py`) and stdio/server entry points. Split into observation (graph, logs, metrics, description, run summary/history), alerts (`wait_for_alert`, `list_alerts`, `set_alert`, `delete_alert`), utility (`load_file`), and write (`log_metric/text/image/audio`). Run lifecycle is NOT exposed — pipelines start/stop via the user's shell.
- `nebo/client.py` — single HTTP client shared by `nebo/mcp/tools.py` and `nebo/cli.py`. Owns all daemon-bound `urllib` traffic; resolves `--url`/`--port`/`--api-token` from kwargs → `NEBO_URL`/`NEBO_PORT`/`NEBO_API_TOKEN` → defaults.
- `nebo/core/transport.py` — `Transport` Protocol shared by the two SDK transports. `FileTransport` (this module) writes append-only `.nebo` files in file mode; `NetworkTransport` (in `nebo/core/client.py`) POSTs events to a daemon in network mode.
- `nebo/cli.py` — subcommands split into two groups:
  - **Server/admin:** `serve`, `cache ls|clear`, `status`, `stop`, `mcp`, `mcp-stdio`, `skill`, `deploy`. PID file at `~/.nebo/server.pid`.
  - **Agent-callable Q&A:** `runs list|show|wait`, `graph show`, `loggables show`, `describe`, `logs`, `metrics list|get|log`, `alerts ls|get|set|rm`, `text|images|audio log`, `load`. Each takes `--url`/`--port`/`--api-token`/`--json` via the shared `_common_conn_parser()` and routes through `nebo/client.py`. `metrics get` supports `--values-only` (emit just the entries array; requires `--name`) and `--runs R1,R2` (client-side cross-run fan-out).
- `nebo/extras/cv/` — optional computer-vision helpers. `nebo/extensions/` — extension hook point.

### Web UI (`ui/`)

React 19 + Vite 7 + TypeScript + Tailwind v4 + shadcn-style components. State via `zustand` (`src/store/index.ts`). WebSocket handled in `src/hooks/useWebSocket.ts`, connecting to the daemon's `/stream` endpoint. Graph rendering uses `@xyflow/react` with `@dagrejs/dagre` layout (`src/components/graph/DagGraph.tsx`). Metrics charts use **Chart.js 4** (registered in `src/components/charts/registerChartJs.ts`) with `chartjs-plugin-zoom` for pan/zoom; the shared lifecycle hook is `src/components/charts/useChartJs.ts`. The bottom panel is the **Tracker** (`src/components/timeline/`); see "Tracker" under the Metrics model section. The default view is "Flat" (store key `'flat'`, wire value `nb.ui(view="flat")`); the DAG view is opt-in. The `@/` import alias maps to `ui/src/`. shadcn registry is configured via `.mcp.json` (the `shadcn` MCP server).

**Mobile experience** (`src/components/mobile/`, <768px via `useIsDesktop`): a dedicated touch UI rendered by `MobileApp` from App.tsx's mobile branch — the desktop layout is untouched. Screens: `MobileRunList` (uniform cards; group tap opens the group page; the search field segues to a flat search screen) → `MobileRunView` (header: group crumb / title → `MobileRunInfoSheet` with notes + config + copyable identifiers; bell → `MobileAlertsSheet`, severity-filterable, tap jumps to the node sheet; gear → `MobileSettingsSheet` sliders over the shared `Settings` keys). Body toggles DAG ⇄ Feed via the shared `viewMode` store key ('graph'/'flat', so `nb.ui(view=)` still applies): `MobileDagCanvas` is a custom dagre + pan/pinch canvas (not ReactFlow; no explicit `setPointerCapture` — it would retarget the derived click and swallow node taps), `MobileFeed` is a stage-rail + type-filter card feed whose expanded charts reuse `SingleRunChart` (exported from `NodeMetrics`). `MobileTracker` renders the persistent heat-strip bar + scrub sheet over `useStreams`. Node taps and alerts open `MobileNodeSheet`, which wraps the desktop `LoggableTabContainer` for full tab parity. Store invariant: **every run mutator clones the run object** (`runs.set(id, { ...run, field })` — see the comment above `setRuns`), so `s.runs.get(id)` selectors are sound and fire only for that run; never mutate a stored run in place. Components that read one slice should select the leaf field (`s.runs.get(id)?.logs`) so unrelated mutations don't re-render them. Phone-width embeds (`?run=`, `&dag`, `&flat`) render the mobile layout via `EmbeddedMobileLayout`.

### Tests

Plain `pytest` + `pytest-asyncio`. Tests are self-contained and exercise the public surface (`test_decorators.py`, `test_client.py`, `test_daemon.py`, `test_mcp_tools.py`, `test_fileformat.py`, …). There is no separate lint/type-check step in CI — only the pytest matrix in `.github/workflows/ci.yml`.

`tests/conftest.py` carries an autouse `monkeypatch.setenv` fixture that pins `NEBO_NO_STORE=1` for every test. This keeps the suite from creating real `.nebo` files in the working directory. Tests that specifically exercise the file writer import `FileTransport` directly.

## Conventions to preserve

- **Auto-init is load-bearing.** Any new public SDK function that touches state must call `_ensure_init()` before reading/writing it (see `ui()` and `start_run()` for examples). Breaking this makes nebo require an explicit `nb.init()`, which it is explicitly designed not to need. Data-emitting surfaces (`log_*`, `alert`, `@fn` execution) additionally call `_ensure_run()` — materializing a run is reserved for real events.
- **`nb.md()`/`nb.ui()` are declarative.** Outside a live run they write a script-level template on `SessionState` (`_script_description`/`_script_ui_config`) — no run, no file, no event; the template is applied (state + `description`/`ui_config` events) at every *new*-run materialization, never on `start_run(run_id=)` resume, and is only cleared by `reset()`. Inside a live run they keep per-run semantics (append/overwrite + emit). One-line rule: metadata outside a run describes every run the script opens; inside a run, that run only. Corollaries: an implicit run only materializes on the first real event (so an event-less implicit run persists nothing), and `start_run` **adopts** a materialized-but-virgin implicit run in place (same run_id/transport, re-emitted `run_start` carries the name; no `run_completed`, no sibling — see `IDENTITY_EVENT_TYPES` in `state.py`) while an implicit run with real events still closes-and-rolls.
- **Run lifecycle flows through events.** The daemon only opens a `.nebo` writer after receiving a `run_start` event — so any code path that connects a client in network mode must also emit `run_start` (see the comment block in `init()` around `script_name`).
- **No run states, no end times.** There is no `status` (running/crashed/completed) anywhere, and no `ended_at` either — a logging SDK can't keep either in sync (a crashed run never reports it). The only temporal facts are `started_at` and `last_event_at` (max observed event timestamp); recency is the sole liveness signal (the UI shows "last active Xs ago", not a completed/duration verdict). `run_completed` survives as a **writer-finalization marker only** — the SDK's `FileTransport` writes its final frame on it and the remote-mode daemon closes its per-run writer on it; it sets no field and has no read-side meaning. Don't reintroduce `ended_at`, run status, or any derived lifecycle field.
- **No error reporting, period.** `@nb.fn()` lets exceptions propagate untouched — no error event, no excepthook. There is no `error` event type anywhere: incoming `error` wire events are silently ignored by the daemon, entry code 6 is retired in the file format, and there are no error read paths (no `/errors`, no `nebo errors`, no UI error panel). Don't reintroduce any of it.
- **`MessageType` is the source of truth for protocol events.** Add new event kinds to `nebo/server/protocol.py` and handle them in the daemon, not ad-hoc strings.
- **The Global loggable is always present.** `SessionState.loggables["__global__"]` is seeded on init/reset/clear. `nb.log*` calls outside any `@nb.fn()` context route there. Any code that iterates loggables and assumes node-only fields (`func_name`, `exec_count`, etc.) must filter by `isinstance(l, NodeInfo)` or `kind == "node"`.
- **`@nb.fn(ui={})` keys.** Production code reads `color` and `default_tab`. `default_tab` values are `"info"` / `"logs"` / `"metrics"` / `"images"` / `"audio"` (no `"ask"` — that tab was removed along with `nb.ask`). Unknown keys are forwarded to the UI verbatim so adding a new hint requires only a UI consumer, no SDK change.
- **No interactive blocking from the SDK.** `nb.ask` and pauseable nodes are intentionally absent — the SDK is for logging, not orchestration. Don't reintroduce wire-level events that block the running pipeline; if a feature needs that, it belongs outside nebo.
- **`nb.log_image` only takes `nb.labels.*` instances.** Raw lists/tensors raise a `TypeError`. The kwarg names are `points`, `boxes`, `circles`, `polygons`, `bitmasks` (note plural for the last). Each kwarg accepts one instance or a list of them — don't reintroduce a "single raw geometry" path.
