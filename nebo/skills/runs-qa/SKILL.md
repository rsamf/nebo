---
name: nebo-runs-qa
description: Use when the user asks questions about nebo runs — text, metrics, DAG structure, comparisons across runs — or wants you to compute and display a derived metric (line, bar, pie, scatter, histogram) in the nebo web UI. Talks to the daemon via the `nebo` CLI (no MCP configuration required). The daemon must be running (`nebo serve`).
---

# Nebo Q&A and derived metrics (CLI)

Answer questions about nebo runs by spawning `nebo` CLI subprocesses with
`--json`. Optionally compute derived metrics and push them back into the
nebo UI via the same CLI.

## Precondition

The nebo daemon must be running:

    nebo serve            # foreground
    nebo serve --daemon   # background

The `nebo` CLI must be on `$PATH`. No MCP configuration required.

If the user asks about a finished run not currently in the daemon's state,
there are two options:

1. The user can point `nebo serve --logdir <dir>` at the directory containing
   the `.nebo` file — the watcher will auto-discover it within a tick.

2. For ad-hoc loading of a file outside `--logdir`, use:

       nebo load <path-to-.nebo>

## Finding the run id

When the user runs a script, the SDK prints a banner to their terminal.

In **file mode** (default — SDK writes .nebo files directly):

    nebo: writing to .nebo/2026-05-16_140513_abc123def456.nebo
    run_id=abc123def456

In **network mode** (SDK pushes events to a running daemon):

    nebo: connected to http://localhost:7861
    run_id=abc123def456

Either way, extract the 12-char hex id from the `run_id=...` line. If the user
hasn't shared their terminal output:

    nebo runs list --json

and pick the most recent.

## Q&A playbook (single run)

Always pass `--json`. Pipe the output into your reasoning step — don't
rely on the human-formatted columns.

| Intent | Command |
|---|---|
| What runs exist? | `nebo runs list --json` |
| Summarize run R | `nebo runs show <R> --json` |
| What hparams/config did R use? | `nebo runs show <R> --json` → `run_config` |
| What does the workflow do? | `nebo describe --run <R> --json` |
| Inspect the DAG | `nebo graph show --run <R> --json` |
| What did node N do? | `nebo loggables show <N> --run <R> --json` |
| Get text entries for node N | `nebo text ls --run <R> --node <N> --json` |
| List available metrics | `nebo metrics list --run <R> --json` |
| Read one metric's series | `nebo metrics get <loggable> --name <M> --values-only --run <R> --json` |
| Read all of a loggable's metrics | `nebo metrics get <loggable> --run <R> --json` |
| Filter a metric by tag | append `--tag <T>` |
| Filter a metric by step | append `--step <S>` |
| Compare a metric across runs | `nebo metrics get <loggable> --name <M> --runs R1,R2,R3 --values-only --json` |
| Wait for an alert | `nebo runs wait <R> --timeout 300 --min-level 20 --json` |
| Set an alert on a metric | `nebo alerts set --title <T> --condition "<M> > 5" --json` |
| Wait for a run to finish (go idle) | `nebo alerts set --title done --condition "last_event > 60" --run <R> --json` then `nebo runs wait <R> --json` |

`nebo runs show --json` keys worth knowing: `run_config` (the dict passed
to `nb.start_run(config=...)`), `metrics_index` ({loggable: [metric
names]}), `metric_series_count`, `latest_step`, `node_count`,
`text_count`, `started_at`, `last_event_at` (epoch seconds of the most
recent event — there is no end time; a run is never known to be "done").

## Organizing runs into groups

Runs live in a filesystem-like tree of **groups** (e.g. `vision/detr/lr-sweep`).
A run is born into a group via `nb.init(group=...)`, `nb.start_run(group=...)`,
or the `NEBO_GROUP` env var; you can reorganize afterward from the CLI. Each
group can hold markdown docs.

| Intent | Command |
|---|---|
| Render the whole tree | `nebo tree --json` |
| List a group (subgroups, runs, docs) | `nebo groups ls <path> --json` |
| Create a group (with ancestors) | `nebo groups add <path>` |
| Rename/move a group subtree | `nebo groups mv <path> <new-path>` |
| Delete an empty group | `nebo groups rm <path>` |
| Move a run into a group | `nebo runs mv <run_id> <group>` |
| Move a run back to the root | `nebo runs mv <run_id> --root` |
| List a group's docs | `nebo groups doc ls <path>` |
| Read a doc | `nebo groups doc get <path> <name>` |
| Write a doc | `nebo groups doc set <path> <name> --file F` (or `--text T`) |
| Delete a doc | `nebo groups doc rm <path> <name>` |

`nebo tree --json` returns `{groups: {path: {docs: [...]}}, runs: {run_id:
group_path}}`; runs absent from `runs` are at the root. `groups rm` refuses
(exit non-zero) if the group still has member runs or subgroups — move them out
first (nebo has no run deletion).

### The curation contract (do this — it's the point)

When you create a group or finish a batch of work inside one, **write or update
that group's `README.md`**. Cover, in order:

1. **What** this group represents (one or two sentences).
2. **Why** the experiments were run — the question or hypothesis.
3. **How** they were run — commands, configs, data, the `NEBO_GROUP` used —
   enough for someone to reproduce them.
4. **Findings** — the conclusive impressions and results, updated as results
   land, each citing the specific run(s)/step(s) they came from.

Docs are living documents: update `README.md` when conclusions change, and add
extra named docs (`ablations.md`, `failures.md`) rather than letting the README
sprawl. Write it with `nebo groups doc set <path> README.md --file <file>`.

### Canonical references (`nebo://`)

Every nebo resource has a canonical `nebo://` reference. Use these when citing
runs, streams, and moments in group docs — clicking them in the UI jumps
straight there:

```
nebo://run/<run_id>                          a run
nebo://run/<run_id>/<loggable_id>            a loggable — qualname, __global__,
                                             or __agent__ (always ONE path segment)
nebo://run/<run_id>/<loggable_id>/<name...>  a named stream (name may contain '/')
<any run form>@<step>                        a datapoint at a step
                                             (e.g. nebo://run/abc123/train/loss@120)
nebo://group/<path>                          a group
```

```markdown
The [baseline](nebo://run/a3f8c2d1) plateaued, but
[lr=3e-4 diverged at step 1200](nebo://run/9b04e7aa@1200) — see
[train/loss there](nebo://run/9b04e7aa/train/loss@1200) and the earlier
[detr experiments](nebo://group/vision/detr).
```

- Prefer the `@<step>` form whenever a claim is about a specific moment in
  training. `?step=<n>` on a run ref is still accepted as a legacy alias.
- To validate a reference before citing it, the daemon's `GET /resolve?ref=<uri>`
  returns the parsed components plus `exists`.

### `metrics get` response schema

With `--name <M> --values-only --json` (preferred — the series directly):

    [{"step": 0, "value": 0.93, "tags": [], "timestamp": 1760000000.0}, ...]

Without `--values-only`, the response nests per metric name:

    {"metrics": {"<name>": {"type": "line", "entries": [{step, value, tags, timestamp}, ...]}}}

With `--runs R1,R2`, both shapes are wrapped per run:

    {"loggable_id": ..., "name": ..., "runs": {"R1": <series or entries>, "R2": ...}}

## Alerts

`nebo runs wait` blocks until an alert fires for the run at a level at
or above `--min-level`, or the `--timeout` elapses. Alerts come from two
places (`triggered_by` distinguishes them):

- **code**: the pipeline called `nb.alert(title, text, level=...)`.
- **cli**: an alert *rule* you created matched a metric — no code change
  needed.

Rules are conditions on metric values, evaluated by the daemon as
metrics arrive; a rule fires at most once per run:

    nebo alerts set --title "loss diverged" --condition "train/loss > 5" --level WARN --json
    nebo alerts set --title "solved" --condition "avg_return >= 200" --run <R> --json

- `--condition` is `<metric> <op> <number>` with ops `> >= < <= == !=`.
- The metric name `last_event` is **reserved** for heartbeat rules: the
  value is seconds since the run's last event, evaluated ~every second
  by the daemon (ops `>` `>=` only; `--loggable` doesn't apply).
- `--loggable <L>` restricts matching to one loggable; default any.
- `--run <R>` scopes the rule to one run; default all runs (right for sweeps).
- `--level` takes DEBUG/INFO/WARN/ERROR or an integer (10/20/30/40).

**Detecting run completion.** Nebo has no run status or end time — a
quiet run is the only "done" signal. Set a heartbeat rule scoped to the
run, then wait:

    nebo alerts set --title done --condition "last_event > 60" --run <R> --json
    nebo runs wait <R> --json

The wait returns once the run has been idle 60s — immediately if it
already is (so waiting on an already-finished run doesn't block). Pick
the threshold from the run's cadence: longer than its slowest gap
between events, or you'll get a false "done" mid-run.

Manage rules with `nebo alerts ls [--run R]`, `nebo alerts get <id>`,
`nebo alerts rm <id>`. `ls` also shows code-fired alerts. The watch
pattern for unattended training: set a rule, then `nebo runs wait <R>
--min-level 30` — it returns as soon as the rule fires.

## Drawing a derived metric

When the answer is better as a chart, compute the values and write them
to the `__agent__` sandbox loggable:

    nebo metrics log --run <R> --entries-json '[{"name":"loss_ma_10","type":"line","value":0.412,"step":10}]'

Value shapes per chart type:

- `line` (accumulating): `value` is a number. `step` aligns the x-axis.
- `bar` (snapshot): `value` is `{"label": number}`; re-emit overwrites.
- `pie` (snapshot): `value` is `{"label": number}`; re-emit overwrites.
- `scatter` (accumulating): `value` is `{"label": {"x": [...], "y": [...]}}`.
- `histogram` (snapshot): `value` is `{"label": [number, ...]}`.

`loggable_id` defaults to `__agent__` when omitted — the right home for
derived work. Use a distinct `name` (e.g. `derived_<intent>`) to avoid
colliding with user-emitted metrics.

### Adding to an existing chart vs. making a new one

Each `(loggable_id, name)` pair is one chart. The **chart type** locks
on first emission for that pair — once a series is created as `scatter`
you can't later re-emit it as `bar`. The **data and labels do not lock**:

- `line` and `scatter` *accumulate*. Re-emitting the same
  `(loggable_id, name)` with a new value (and, for scatter, new labels)
  adds points/series to the existing chart. Old data stays.
- `bar`, `pie`, `histogram` are *snapshots*. Re-emitting overwrites the
  prior value — the chart now shows only the new data.

So to overlay agent-computed points onto a user's existing scatter chart,
emit with the user's exact `loggable_id` and `name` (and `type:"scatter"`).
To keep agent output separated, emit to `__agent__` (the default) under a
new `name`.

## Images and audio

You can emit images or audio with three input shapes:

| Field | Where the bytes are |
|---|---|
| `path: "/abs/path.png"` | On disk; the CLI reads and base64-encodes |
| `url: "https://..."` | Remote; the daemon fetches and stores |
| `data: "<base64>"` | Already base64 |

Exactly one of `path` / `url` / `data` per entry. Examples:

    nebo images log --run <R> --entries-json '[{"name":"plot","path":"/tmp/plot.png"}]'
    nebo audio log  --run <R> --entries-json '[{"name":"snd","path":"/tmp/snd.wav","sr":22050}]'

`path` is the most natural form when you've just generated the file
yourself (e.g. matplotlib `savefig` to a tmp file).

### Reading media back

To *view* a run's media (e.g. inspect a logged sample, or show it to
the user), list it and download by `media_id`:

    nebo images ls --run <R>           # per-loggable: name@step media_id=...
    nebo audio ls  --run <R>           # same, plus sr=...
    nebo images get <media_id> --run <R> -o /tmp/sample.png
    nebo audio get  <media_id> --run <R> -o /tmp/sample.wav

`get` prints the written path — read the file to see the image
yourself, and attach/send it if the user should see it too. media_ids
are content-addressed (sha256 of the bytes): stable across daemon
restarts, and byte-identical media logged from several places shares
one id. Over MCP, `nebo_get_image` returns the image directly as an
inline image content block (no temp file needed); audio has no MCP
read path — use the CLI download.

## 3D scenes (the action modality)

A run may carry 3D scenes — logged with `nb.log_body_model` /
`nb.log_body_transform`, rendered in the UI's **Actions** tab.

    nebo actions ls --run <R>

lists each scene with its instance labels, frame count and step range, then
the run's body models (name, model_id, body count, source format). To fetch
a model's geometry:

    nebo actions get <model_id> --run <R> -o /tmp/robot.glb

Read-only by design: logging poses means shipping per-body transform arrays,
which belongs in the pipeline, not a shell.

**You cannot see a scene.** There is no server-side 3D renderer, so neither
the CLI nor MCP (`nebo_list_actions`) can turn one into an image. Answer
questions about a scene from its *metadata* (which instances exist, how many
frames, what step range, how many bodies) and from the metrics logged
alongside it — and say plainly that visual inspection means opening the
Actions tab. Don't describe what a scene "looks like"; you haven't seen it.

## Multi-run Q&A

For one metric across several runs, use the cross-run query directly:

    nebo metrics get <loggable> --name <M> --runs R1,R2,R3 --values-only --json

This returns `{"runs": {"R1": [{step, value, ...}, ...], "R2": ...}}` —
one call instead of a per-run loop. For anything beyond a single metric
name (different metrics per run, text, configs), loop:

1. `nebo runs list --json` — pick the relevant run ids.
2. For each: `nebo runs show <id> --json` (for `metrics_index` and
   `run_config`) then targeted `metrics get` calls.
3. Reason across the responses in your reply.

Token discipline: prefer `metrics_index` from `runs show` over fetching
every loggable's metrics blindly.

### Visualizing a cross-run answer

Comparison views in the nebo web UI are pure UI state — the daemon
doesn't know which runs the user has selected, and you can't push the
UI into a particular comparison. But you have two ways to make a
cross-run answer *visual*:

**A. Seed an overlay the user can open themselves.** Emit the same
metric `name` to the same `loggable_id` on each run you want compared.
Example: after looping the runs you want to compare, write a
`derived_loss` line metric to `__agent__` on each:

    nebo metrics log --run R1 --entries-json '[{"name":"derived_loss","type":"line","value":0.41,"step":0}]'
    nebo metrics log --run R2 --entries-json '[{"name":"derived_loss","type":"line","value":0.38,"step":0}]'
    nebo metrics log --run R3 --entries-json '[{"name":"derived_loss","type":"line","value":0.45,"step":0}]'

Tell the user: "Open the comparison view, select R1/R2/R3, and look at
`__agent__ ▸ derived_loss`." The UI overlays the three series — same
loggable_id + name across selected runs is the comparison contract.

**B. Synthesize the comparison into a single chart on one run.** When
you don't need the user to do anything in the UI, fold the multi-run
result into a single chart on one chosen run. A bar chart keyed by run
name is the most common form:

    nebo metrics log --run R1 --entries-json '[{
      "name": "final_loss_by_run",
      "type": "bar",
      "value": {"R1": 0.41, "R2": 0.38, "R3": 0.45}
    }]'

That single bar chart on `__agent__` answers the cross-run question
without requiring the user to open the comparison view at all. Useful
when the comparison itself is the deliverable rather than a starting
point for further exploration.

## Anti-patterns

- Don't omit `--json`. Human columns drift and break parsers.
- Don't write to `__global__` — that's the user's space. Default
  `loggable_id` `__agent__` is correct for derived work.
- Don't try to declare new chart types — only line/bar/pie/scatter/histogram.
- Don't try to start/stop pipelines from the skill. That's the user's
  shell (`uv run python script.py`, Ctrl+C, `pkill`).

## Connection settings

Every read/write subcommand accepts:

- `--url <url>` — daemon URL (defaults to `NEBO_CLI_URL` env or `http://localhost:7861`).
- `--port <N>` — daemon port if the daemon isn't at the default.
- `--api-token <token>` — required if the daemon was started with
  `nebo serve --api-token <X>` and read access is gated.

You can also set `NEBO_CLI_URL`, `NEBO_CLI_PORT`, `NEBO_API_TOKEN` environment
variables once instead of passing flags every call.

## Optional: MCP

If the user already has the nebo MCP server configured, the same tools
are available without spawning subprocesses. Both transports are parallel
— pick one based on the user's setup.

| CLI | MCP tool |
|---|---|
| `nebo runs list` | `nebo_get_run_history` |
| `nebo runs show <R>` | `nebo_get_run_status` |
| `nebo describe` | `nebo_get_description` |
| `nebo graph show` | `nebo_get_graph` |
| `nebo loggables show <id>` | `nebo_get_loggable_status` |
| `nebo text ls` | `nebo_get_text` |
| `nebo metrics get` | `nebo_get_metrics` |
| `nebo metrics log` | `nebo_log_metric` |
| `nebo alerts ls` | `nebo_list_alerts` |
| `nebo alerts set` | `nebo_set_alert` |
| `nebo alerts rm` | `nebo_delete_alert` |
| `nebo text log` | `nebo_log_text` |
| `nebo images log` | `nebo_log_image` |
| `nebo audio log` | `nebo_log_audio` |
| `nebo images ls` | `nebo_list_images` |
| `nebo images get` | `nebo_get_image` (renders inline) |
| `nebo runs wait` | `nebo_wait_for_alert` |
| `nebo tree` | `nebo_get_tree` |
| `nebo groups add` | `nebo_create_group` |
| `nebo groups mv` | `nebo_move_group` |
| `nebo groups rm` | `nebo_delete_group` |
| `nebo runs mv` | `nebo_move_run` |
| `nebo groups doc get` | `nebo_get_group_doc` |
| `nebo groups doc set` | `nebo_set_group_doc` |
| `nebo load` | `nebo_load_file` |
