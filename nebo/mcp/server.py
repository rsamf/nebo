"""MCP server for nebo.

Exposes both observation tools (query state) and action tools (control pipelines)
via the Model Context Protocol.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from nebo.mcp import tools


MCP_TOOLS = [
    # ── Observation Tools ──
    {
        "name": "nebo_get_graph",
        "description": "Get the full DAG structure of the running pipeline. Returns nodes (with docstrings, source/non-source status, execution counts), edges, and workflow description.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "Optional run ID. Uses latest run if omitted."},
            },
        },
    },
    {
        "name": "nebo_get_loggable_status",
        "description": "Get detailed status for a specific loggable (node or global). Includes execution count, params, docstring, recent logs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "loggable_id": {"type": "string", "description": "The loggable ID."},
                "run_id": {"type": "string", "description": "Optional run ID."},
            },
            "required": ["loggable_id"],
        },
    },
    {
        "name": "nebo_get_text",
        "description": "Get recent text entries, optionally filtered by loggable and run.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "loggable_id": {"type": "string", "description": "Optional loggable ID filter."},
                "run_id": {"type": "string", "description": "Optional run ID."},
                "limit": {"type": "integer", "description": "Max entries (default 100)."},
            },
        },
    },
    {
        "name": "nebo_get_metrics",
        "description": "Get metric time series for a loggable.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "loggable_id": {"type": "string", "description": "The loggable ID."},
                "name": {"type": "string", "description": "Optional specific metric name."},
            },
            "required": ["loggable_id"],
        },
    },
    {
        "name": "nebo_get_description",
        "description": "Get workflow-level description and all node docstrings.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "nebo_list_images",
        "description": (
            "List a run's images: per-loggable entries with name, step, "
            "timestamp, and the content-addressed media_id used by "
            "nebo_get_image."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "The run ID."},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "nebo_get_image",
        "description": (
            "Fetch one logged image (media_id from nebo_list_images). "
            "Returns the actual image as an MCP image content block, so "
            "it renders inline in the conversation and is visible to "
            "both the model and the user."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "The run ID."},
                "media_id": {"type": "string", "description": "Content-addressed media id from nebo_list_images."},
            },
            "required": ["run_id", "media_id"],
        },
    },
    # ── Action Tools ──
    {
        "name": "nebo_get_run_status",
        "description": "Get the summary of a run: timestamps, node/edge counts, metric series, run config, error counts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "The run ID."},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "nebo_get_run_history",
        "description": "List all runs with outcomes, timestamps, and error counts.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "nebo_wait_for_alert",
        "description": (
            "Block until an alert at or above `min_level` fires (via "
            "`nb.alert(...)` or a rule set with nebo_set_alert), or timeout "
            "elapses. To wait for a run to finish, first set a heartbeat "
            "rule ('last_event > 60' scoped to the run) — this wakes once "
            "the run has been idle that long (immediately if it already is)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "Run ID to monitor for alerts."},
                "timeout": {"type": "number", "description": "Max seconds to wait (default 300)."},
                "min_level": {"type": "integer", "description": "Minimum alert level to trigger on (default 20)."},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "nebo_list_alerts",
        "description": (
            "List alerts: rules created via CLI/MCP (triggered_by='cli', with "
            "their metric condition and fired history) and alerts fired by "
            "nb.alert(...) in pipeline code (triggered_by='code')."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "Optional run ID to scope the listing."},
            },
        },
    },
    {
        "name": "nebo_set_alert",
        "description": (
            "Create an alert rule that fires when a metric satisfies a "
            "condition — no code changes needed. The rule fires at most once "
            "per run; fired alerts wake nebo_wait_for_alert. Condition is a "
            "string like 'train/loss > 5' (ops: > >= < <= == !=). The "
            "reserved metric 'last_event' fires on idle seconds: "
            "'last_event > 60' fires once a run has been quiet for 60s "
            "(ops > >= only; nebo's run-completion signal)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Alert headline."},
                "condition": {"type": "string", "description": "Metric condition, e.g. 'train/loss > 5', or 'last_event > 60' to fire on run idleness."},
                "text": {"type": "string", "description": "Optional body / details."},
                "level": {"type": "integer", "description": "Severity: 10=DEBUG, 20=INFO, 30=WARN, 40=ERROR (default 20)."},
                "loggable_id": {"type": "string", "description": "Only match the metric on this loggable."},
                "run_id": {"type": "string", "description": "Only apply to this run (default: all runs)."},
            },
            "required": ["title", "condition"],
        },
    },
    {
        "name": "nebo_delete_alert",
        "description": "Delete an alert rule by id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "rule_id": {"type": "string", "description": "Alert rule id (from nebo_list_alerts)."},
            },
            "required": ["rule_id"],
        },
    },
    {
        "name": "nebo_load_file",
        "description": "Load a .nebo log file into the daemon for viewing and Q&A. The file will appear as a historical run.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filepath": {
                    "type": "string",
                    "description": "Absolute path to the .nebo file",
                },
            },
            "required": ["filepath"],
        },
    },
    # ── Run Tree (groups) ──
    {
        "name": "nebo_get_tree",
        "description": (
            "Get the run tree: groups (each with its markdown doc filenames) "
            "and per-run placements (run_id -> group path). Runs absent from "
            "the placements map are at the root."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "nebo_create_group",
        "description": (
            "Create a run group (and any missing ancestors), e.g. "
            "'vision/detr/lr-sweep'. Idempotent."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Group path, '/'-delimited."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "nebo_move_group",
        "description": "Rename/move a group subtree — its runs and docs move with it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Existing group path."},
                "new_path": {"type": "string", "description": "New group path."},
            },
            "required": ["path", "new_path"],
        },
    },
    {
        "name": "nebo_delete_group",
        "description": (
            "Delete an empty group. Refuses (409) if it still has member runs "
            "or subgroups — move them out first (nebo has no run deletion)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Group path to delete."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "nebo_move_run",
        "description": (
            "Place a run into a group (empty string = root). Auto-creates the "
            "target group. This is an explicit override that persists."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "The run to move."},
                "group": {"type": "string", "description": "Destination group path ('' = root)."},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "nebo_get_group_doc",
        "description": "Read a group's markdown doc (default README.md).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Group path."},
                "name": {"type": "string", "description": "Doc filename (default README.md)."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "nebo_set_group_doc",
        "description": (
            "Write/overwrite a group's markdown doc (default README.md). After "
            "finishing work in a group, record: WHAT the group represents, WHY "
            "the experiments ran (the question/hypothesis), HOW they ran "
            "(commands, config, the NEBO_GROUP used), and the FINDINGS — "
            "conclusive results, updated as they land. Cite specific runs and "
            "steps with nebo:// links: [baseline](nebo://run/<id>) or "
            "[diverged](nebo://run/<id>?step=<n>), and groups as "
            "nebo://group/<path>."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Group path."},
                "content": {"type": "string", "description": "Markdown body."},
                "name": {"type": "string", "description": "Doc filename (default README.md)."},
            },
            "required": ["path", "content"],
        },
    },
    # ── Write Tools ──
    # These mirror the SDK's nb.log_* helpers so an external agent can push
    # data into a run without owning the SDK process. Each tool accepts a
    # single entry or a list — `entries` is the canonical input shape.
    {
        "name": "nebo_log_metric",
        "description": (
            "Log one or more metric points to a run. Mirrors nb.log_line / "
            "log_bar / log_pie / log_scatter / log_histogram from the SDK. "
            "Default chart type is 'line' (accumulating); other types are "
            "snapshots — re-emitting the same name overwrites. "
            "loggable_id defaults to '__agent__' (sandbox for agent-authored "
            "entries) when omitted."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entries": {
                    "description": "Single entry or list of entries. Each: {run_id?, loggable_id?, name, value, type?, step?, tags?}. loggable_id defaults to '__agent__'.",
                    "oneOf": [
                        {"type": "object"},
                        {"type": "array", "items": {"type": "object"}},
                    ],
                },
                "run_id": {"type": "string", "description": "Default run_id if entries don't specify one."},
            },
            "required": ["entries"],
        },
    },
    {
        "name": "nebo_log_image",
        "description": (
            "Log one or more images to a run. Mirrors nb.log_image. Each "
            "entry supplies either `url` (fetched server-side, persisted) "
            "or `data` (already-base64 bytes). Bytes are stored on the "
            "daemon so the run survives the source URL going stale. "
            "loggable_id defaults to '__agent__' when omitted."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entries": {
                    "description": "Single entry or list. Each: {run_id?, loggable_id?, name, url? | data?, step?, labels?}. loggable_id defaults to '__agent__'.",
                    "oneOf": [
                        {"type": "object"},
                        {"type": "array", "items": {"type": "object"}},
                    ],
                },
                "run_id": {"type": "string"},
            },
            "required": ["entries"],
        },
    },
    {
        "name": "nebo_log_audio",
        "description": (
            "Log one or more audio recordings to a run. Mirrors "
            "nb.log_audio. Same input shape as nebo_log_image plus an "
            "optional `sr` (sample rate) per entry; default 16000. "
            "loggable_id defaults to '__agent__' when omitted."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entries": {
                    "description": "Single entry or list. Each: {run_id?, loggable_id?, name, url? | data?, sr?, step?}. loggable_id defaults to '__agent__'.",
                    "oneOf": [
                        {"type": "object"},
                        {"type": "array", "items": {"type": "object"}},
                    ],
                },
                "run_id": {"type": "string"},
            },
            "required": ["entries"],
        },
    },
    {
        "name": "nebo_log_text",
        "description": (
            "Log one or more text entries to a run. Mirrors nb.log_text. "
            "loggable_id defaults to '__agent__' (sandbox for agent-authored "
            "entries) when omitted."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entries": {
                    "description": "Single entry or list. Each: {run_id?, loggable_id?, name?, message, step?}.",
                    "oneOf": [
                        {"type": "object"},
                        {"type": "array", "items": {"type": "object"}},
                    ],
                },
                "run_id": {"type": "string"},
            },
            "required": ["entries"],
        },
    },
]


async def handle_tool_call(name: str, arguments: dict[str, Any], server_url: str = "http://localhost:7861") -> Any:
    """Dispatch an MCP tool call to the appropriate handler."""
    handlers = {
        # Observation
        "nebo_get_graph": lambda a: tools.get_graph(a.get("run_id"), server_url),
        "nebo_get_loggable_status": lambda a: tools.get_loggable_status(a["loggable_id"], a.get("run_id"), server_url),
        "nebo_get_text": lambda a: tools.get_text(a.get("loggable_id"), a.get("run_id"), a.get("limit", 100), server_url),
        "nebo_get_metrics": lambda a: tools.get_metrics(a["loggable_id"], a.get("name"), server_url),
        "nebo_get_description": lambda a: tools.get_description(server_url),
        "nebo_list_images": lambda a: tools.list_images(a["run_id"], server_url),
        "nebo_get_image": lambda a: tools.get_image(a["run_id"], a["media_id"], server_url),
        # Action
        "nebo_get_run_status": lambda a: tools.get_run_status(a["run_id"], server_url),
        "nebo_get_run_history": lambda a: tools.get_run_history(server_url),
        "nebo_wait_for_alert": lambda a: tools.wait_for_alert(a["run_id"], a.get("timeout", 300), a.get("min_level", 20), server_url),
        "nebo_list_alerts": lambda a: tools.list_alerts(a.get("run_id"), server_url),
        "nebo_set_alert": lambda a: tools.set_alert(
            a["title"], a["condition"], a.get("text", ""), a.get("level", 20),
            a.get("loggable_id"), a.get("run_id"), server_url,
        ),
        "nebo_delete_alert": lambda a: tools.delete_alert(a["rule_id"], server_url),
        "nebo_load_file": lambda a: tools.load_file(a["filepath"], server_url),
        # Run tree
        "nebo_get_tree": lambda a: tools.get_tree(server_url),
        "nebo_create_group": lambda a: tools.create_group(a["path"], server_url),
        "nebo_move_group": lambda a: tools.move_group(a["path"], a["new_path"], server_url),
        "nebo_delete_group": lambda a: tools.delete_group(a["path"], server_url),
        "nebo_move_run": lambda a: tools.move_run(a["run_id"], a.get("group", ""), server_url),
        "nebo_get_group_doc": lambda a: tools.get_group_doc(a["path"], a.get("name", "README.md"), server_url),
        "nebo_set_group_doc": lambda a: tools.set_group_doc(a["path"], a["content"], a.get("name", "README.md"), server_url),
        # Write
        "nebo_log_metric": lambda a: tools.log_metric(a["entries"], a.get("run_id"), server_url),
        "nebo_log_image": lambda a: tools.log_image(a["entries"], a.get("run_id"), server_url),
        "nebo_log_audio": lambda a: tools.log_audio(a["entries"], a.get("run_id"), server_url),
        "nebo_log_text": lambda a: tools.log_text(a["entries"], a.get("run_id"), server_url),
    }
    handler = handlers.get(name)
    if handler is None:
        return {"error": f"Unknown tool: {name}"}
    return await handler(arguments)


def run_mcp_server(port: int = 7861) -> None:
    """Run the MCP server in stdio mode (JSON-RPC over stdin/stdout)."""
    from nebo.mcp.stdio import run_stdio_bridge
    run_stdio_bridge(port=port)
