# Nebo

A modern logging SDK for multi-modal data. Decorate your functions with `@nb.fn()`, and nebo automatically infers a DAG from your call graph, captures text, metrics, images, and audio -- all written to append-only `.nebo` files locally (tensorboard-style) and queryable in real time via CLI, MCP tools, or a web dashboard.

## Installation

```bash
pip install nebo
```

The CLI entry point is `nebo`:

```bash
nebo --help
```

## Quick Start

```python
import nebo as nb

@nb.fn()
def load_data(path: str = "data.csv") -> list[dict]:
    """Load records from a file."""
    records = [{"id": i, "value": i * 0.5} for i in range(100)]
    nb.log_text("status", f"Loaded {len(records)} records from {path}")
    return records

@nb.fn()
def transform(records: list[dict]) -> list[dict]:
    """Normalize values."""
    out = []
    for r in nb.track(records, name="transforming"):
        out.append({**r, "value": r["value"] / 50.0})
    nb.log_text("status", f"Transformed {len(out)} records")
    nb.log_line("record_count", float(len(out)))
    return out

@nb.fn()
def run():
    """Main pipeline entry point."""
    records = load_data()
    result = transform(records)
    nb.log_text("status", f"Pipeline complete: {len(result)} records")
    return result

if __name__ == "__main__":
    run()
```

Running this writes events to `./.nebo/<timestamp>_<run_id>.nebo`. Point `nebo serve --logdir ./.nebo` at the directory to inspect runs in the web UI. The DAG edges (`run -> load_data`, `load_data -> transform`) are inferred automatically from data flow -- no manual wiring required.

## Core Concepts

### `@nb.fn()` -- Register a function as a DAG node

Every function decorated with `@nb.fn()` becomes a node in the pipeline DAG. Edges are inferred from **data flow**: when a node's return value is passed as an argument to another node, an edge is created from the producer to the consumer.

```python
@nb.fn()
def load_data():
    return [1, 2, 3]

@nb.fn()
def transform(data):
    return [x * 2 for x in data]

@nb.fn()
def run():
    records = load_data()        # edge: run -> load_data (no data dependency)
    result = transform(records)  # edge: load_data -> transform (data flows from load_data)
    return result
```

When a child node receives no node-produced arguments, the edge falls back to the calling parent node.

You can use it in several ways:

```python
@nb.fn              # bare decorator
@nb.fn()            # with parentheses
@nb.fn(depends_on=[other_fn])  # with explicit dependencies
@nb.fn(ui={"collapsed": True})  # with per-node UI hints
```

### Class Decoration

`@nb.fn()` can be applied to classes. All methods are wrapped with scope tracking, and the class name becomes a visual group in the DAG:

```python
@nb.fn()
class Agent:
    def think(self, query):
        nb.log_text("thoughts", f"Thinking about: {query}")
        return {"plan": "respond"}

    def act(self, plan):
        nb.log_text("actions", f"Acting on: {plan}")
        return "result"

agent = Agent()
agent.think("hello")
agent.act({"plan": "respond"})
```

Methods appear as `Agent.think` and `Agent.act` in the DAG, grouped under `Agent`.

### Automatic Materialization

Decorated functions appear in the DAG as soon as they execute for the first time — a call to `nb.log_text()`, `nb.log_line()`, etc. is not required. This keeps dependency chains intact when an intermediate function only orchestrates calls to other nodes without logging anything itself.

### `depends_on` -- Explicit dependency declaration

Some dependencies cannot be detected automatically (shared mutable state, class attributes, global variables). Use `depends_on` to declare these explicitly:

```python
@nb.fn()
def setup():
    """Initialize shared resources."""
    ...

@nb.fn(depends_on=[setup])
def process():
    """Uses resources initialized by setup."""
    ...
```

### `nb.log_text(name, message)` -- Text streams

Log a text message to a named stream on the current node — text is a named payload stream like metrics and images, so `log_text` pairs a stream `name` with a `message` the way `log_line` pairs a name with a value. The UI shows one card per stream name; entries are queryable via CLI and MCP tools.

```python
@nb.fn()
def train(data):
    nb.log_text("status", f"Training on {len(data)} samples")
    for epoch in range(10):
        loss = do_train(data)
        nb.log_text("epochs", f"Epoch {epoch}: loss={loss:.4f}")
```

### Typed metric helpers — `nb.log_line` / `log_bar` / `log_pie` / `log_scatter` / `log_histogram`

One function per chart type. The chart type locks on first emission per `(loggable, name)` pair — reusing a name with a different `log_*` function raises `ValueError`.

`log_line` and `log_scatter` **accumulate** (every call appends more data). `log_bar`, `log_pie`, and `log_histogram` are **snapshots** — re-emitting the same name overwrites the prior value, and they don't take `step` or `tags`.

```python
@nb.fn()
def train(model, data):
    for epoch in range(100):
        loss = train_one_epoch(model, data)
        nb.log_line("loss", loss)                                  # scalar
        nb.log_line("lr", 3e-4, tags=["main"])                     # tagged for UI filter

    nb.log_bar("counts", {"cat": 3, "dog": 5})                     # {label: number}
    nb.log_scatter("embed_2d", {                                   # {label: list[(x, y)]}
        "inliers":  [(0.1, 0.2), (0.3, 0.4)],
        "outliers": [(2.0, -1.0)],
    })
    nb.log_histogram(                                              # {label: list[number]}
        "latencies",
        {"p50": [...], "p95": [...], "p99": [...]},
        colors=True,                                               # palette per label
    )
```

`log_scatter` and `log_histogram` accept `colors: bool = False`. With `colors=True` the UI distinguishes labels using the shared palette; not recommended in comparison views, where color is reserved for run identity.

### `nb.log_cfg(cfg)` -- Configuration logging

Log configuration for the current node.

```python
@nb.fn()
def train(lr=0.001, epochs=50):
    nb.log_cfg({"lr": lr, "epochs": epochs})
    ...
```

### `nb.track(iterable, name=None, total=None)` -- Progress tracking

Wrap any iterable for tqdm-like progress tracking.

```python
@nb.fn()
def process(items):
    for item in nb.track(items, name="processing"):
        transform(item)
```

### `nb.log_image(image, *, name=None, step=None, points=None, boxes=None, circles=None, polygons=None, bitmask=None)` -- Image logging

Log images (PIL, NumPy arrays, or PyTorch tensors) for visual inspection, with optional geometric labels overlaid. Points are `[x, y]` (or a list of them); boxes are `[x1, y1, x2, y2]` in xyxy format; circles are `[x, y, r]`; polygons are `[[x, y], ...]`; bitmasks are 2D (HxW) or stacked (NxHxW). The UI's Settings pane > "Image labels" section exposes per-(loggable, image, key) visibility and opacity controls.

### `nb.log_audio(audio, sr=16000, name=None, step=None)` -- Audio logging

Log audio data for playback and analysis.

### `nb.md(description)` -- Workflow description

Set a workflow-level description (Markdown supported). Visible in MCP tools and the dashboard. Declarative: outside a run it applies to every run the script opens (creating none); inside a run, to that run only.

```python
nb.md("A pipeline that loads images, runs inference, and exports predictions.")
```

### `nb.ui()` -- Run-level UI defaults

Set default layout and display options for the web UI. Declarative, with the same scoping as `nb.md()`:

```python
nb.ui(layout="horizontal", view="dag", minimap=True, theme="dark")
```

## CLI Reference

### Start the daemon server

```bash
nebo serve                  # foreground
nebo serve -d               # background (daemon mode)
nebo serve --port 3000      # custom port
nebo serve --no-store       # disable .nebo file storage
```

### Run a pipeline

Launch pipelines from your shell — the SDK auto-detects a running daemon
and connects. The SDK prints `Nebo daemon fully connected. Your run id
is: <id>.` to stdout on connect.

```bash
uv run python my_pipeline.py
```

Use that run id with the read/write CLI subcommands. Stop a running
pipeline from the shell (Ctrl+C, `kill`, `pkill`).

### Load a .nebo file

```bash
nebo load .nebo/2026-04-06_143000_run-1.nebo
```

### Check status and text

```bash
nebo status
nebo text ls
nebo text ls --run experiment-1 --node train --limit 50
```

### Stop the daemon

```bash
nebo stop
```

### MCP integration

```bash
nebo mcp   # print Claude Code MCP config
```

## MCP Tools for AI Agents

Nebo exposes 23 MCP tools for querying and controlling pipelines from an AI agent (e.g., Claude). The daemon server must be running.

Each tool below is available both as a CLI subcommand (no setup) and as an
MCP tool (for clients that prefer it). Pipeline lifecycle is deliberately
not exposed — the user launches scripts from their own shell.

### Observation Tools

| CLI | MCP | Description |
|------|------|-------------|
| `nebo runs list` | `nebo_get_run_history` | All runs with timestamps, counts, and metric indexes |
| `nebo runs show <id>` | `nebo_get_run_status` | One run's summary + `metrics_index` |
| `nebo graph show` | `nebo_get_graph` | Full DAG: nodes, edges, execution counts |
| `nebo loggables show <id>` | `nebo_get_loggable_status` | One loggable: text, metrics, params |
| `nebo text ls` | `nebo_get_text` | Text entries, filterable by loggable and run |
| `nebo metrics get <loggable>` | `nebo_get_metrics` | Metric series with `--tag` / `--step` filters |
| `nebo describe` | `nebo_get_description` | Workflow description + node docstrings |

### Utility & Write Tools

| CLI | MCP | Description |
|------|------|-------------|
| `nebo load <file>` | `nebo_load_file` | Load a `.nebo` file into the daemon |
| `nebo runs wait <id>` | `nebo_wait_for_alert` | Block until `nb.alert(...)` fires |
| `nebo metrics log --entries-json '[...]'` | `nebo_log_metric` | Push derived metrics (defaults to `__agent__`) |
| `nebo text log --entries-json '[...]'` | `nebo_log_text` | Push text entries |
| `nebo images log --entries-json '[...]'` | `nebo_log_image` | Push images by `path` / `url` / `data` |
| `nebo audio log --entries-json '[...]'` | `nebo_log_audio` | Push audio recordings |

## .nebo File Format

Runs are persisted as `.nebo` binary files using MessagePack serialization. Each file contains a header (magic, version, metadata) followed by append-only event entries. Use `nebo load` to replay a file into the daemon.

## Architecture

```
+----------------+     +------------------+     +------------------+
|  Your Python   |---->|    Nebo SDK      |---->|  Daemon Server   |
|   Pipeline     |     |  (@fn, log_text, |     |  (FastAPI,       |
|                |     |   track, ...)    |     |   port 7861)     |
+----------------+     +--------+---------+     +--------+---------+
                                |                        |
                        +-------v-------+ +--------------+---------------+
                        |   Terminal    | |              |               |
                        |   Dashboard  | |       +------v------+ +------v------+
                        |   (Rich)     | |       |  MCP Tools  | |   Web UI    |
                        +--------------+ |       |  (Claude)   | |             |
                                         |       +-------------+ +-------------+
                                   +-----v-----+
                                   |    CLI    |
                                   |    nebo   |
                                   +-----------+
```

Two execution modes:

- **Local mode** (default): In-process only. No daemon needed.
- **Server mode**: Events stream to a persistent daemon via HTTP. Use `nebo serve` to start the daemon.

## API Reference

### Module: `nebo`

| Function | Signature | Description |
|----------|-----------|-------------|
| `fn` | `@fn()`, `@fn(depends_on=[...])`, `@fn(ui={...})` | Register a function/class as a DAG node |
| `log_text` | `log_text(name, message, *, step=None)` | Log a text message to a named stream |
| `log_line` | `log_line(name, value, *, step=None, tags=None)` | Log a scalar line-chart datapoint |
| `log_bar` | `log_bar(name, value)` | Bar-chart snapshot (`{label: number}`); overwrites |
| `log_pie` | `log_pie(name, value)` | Pie-chart snapshot (`{label: number}`); overwrites |
| `log_scatter` | `log_scatter(name, value, *, step=None, tags=None, colors=False)` | Labeled scatter (`{label: list[(x, y)]}`); accumulates |
| `log_histogram` | `log_histogram(name, value, *, colors=False)` | Labeled histogram snapshot (`{label: list[number]}`); overwrites |
| `log_cfg` | `log_cfg(cfg: dict)` | Log node configuration |
| `log_image` | `log_image(image, *, name=None, step=None, points=None, boxes=None, circles=None, polygons=None, bitmasks=None)` | Log an image (label kwargs accept `nb.labels.<Class>` instances or lists of them) |
| `log_audio` | `log_audio(audio, sr=16000, name=None, step=None)` | Log audio data |
| `labels` | `nb.labels.{Points, Boxes, Circles, Polygons, Bitmasks}(data, color)` | Image-label dataclasses; each pairs raw geometry with a CSS color |
| `track` | `track(iterable, name=None, total=None)` | Progress tracking |
| `md` | `md(description: str)` | Set workflow description |
| `ui` | `ui(layout, view, collapsed, minimap, theme)` | Set run-level UI defaults |
| `init` | `init(uri, dag_strategy, flush_interval, api_token, group, ...)` | Manual initialization |
| `get_state` | `get_state() -> SessionState` | Access the global state singleton |

## Recent changes

- Global loggable catches `nb.log*` calls outside `@nb.fn()`; node_id is now loggable_id on the wire.
