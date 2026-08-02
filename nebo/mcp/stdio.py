"""MCP stdio transport that bridges stdin/stdout to the daemon's HTTP API.

Used by Claude Code: `nebo mcp-stdio` runs this as a subprocess,
reading JSON-RPC from stdin and forwarding tool calls to the daemon.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any


async def _handle_request(request: dict, server_url: str) -> dict:
    """Handle a single MCP JSON-RPC request."""
    from nebo.mcp.server import MCP_TOOLS, handle_tool_call

    method = request.get("method", "")
    req_id = request.get("id")
    params = request.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {"listChanged": False},
                    "notifications": True,
                },
                "serverInfo": {
                    "name": "nebo",
                    "version": "0.1.0",
                },
            },
        }

    elif method == "notifications/initialized":
        # Client acknowledges initialization — no response needed
        return None  # type: ignore

    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": MCP_TOOLS},
        }

    elif method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})
        try:
            result = await handle_tool_call(tool_name, tool_args, server_url)
            # `_mcp_content` is the handlers' escape hatch for returning
            # real MCP content blocks (e.g. nebo_get_image's inline
            # image) instead of the default JSON-as-text wrapping.
            if isinstance(result, dict) and "_mcp_content" in result:
                content = result["_mcp_content"]
            else:
                content = [{"type": "text", "text": json.dumps(result, default=str)}]
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": content},
            }
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": str(e)},
            }

    elif method == "resources/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"resources": []},
        }

    else:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {},
        }


def run_stdio_bridge(port: int = 7861) -> None:
    """Run the MCP stdio bridge.

    Reads JSON-RPC messages from stdin line by line,
    dispatches to daemon, and writes responses to stdout.

    Args:
        port: The daemon server port.
    """
    server_url = f"http://localhost:{port}"

    async def _run() -> None:
        loop = asyncio.get_event_loop()

        # Read from stdin line by line
        while True:
            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue

                request = json.loads(line)
                response = await _handle_request(request, server_url)

                if response is not None:
                    sys.stdout.write(json.dumps(response) + "\n")
                    sys.stdout.flush()

            except json.JSONDecodeError:
                continue
            except EOFError:
                break
            except Exception as e:
                error_resp = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {e}"},
                }
                sys.stdout.write(json.dumps(error_resp) + "\n")
                sys.stdout.flush()

    asyncio.run(_run())
