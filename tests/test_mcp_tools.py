"""Tests for MCP tools."""

from __future__ import annotations

import pytest

from nebo.core.state import SessionState


class TestMCPToolNames:
    """Tests for MCP tool naming convention."""

    def test_all_tool_names_use_nebo_prefix(self) -> None:
        """All MCP tools should use the nebo_ prefix.

        Claude Code's MCP namespace uses the prefix to scope tool calls
        — every registered tool must start with it or it won't dispatch.
        """
        from nebo.mcp.server import MCP_TOOLS
        for tool in MCP_TOOLS:
            assert tool["name"].startswith("nebo_"), f"Tool {tool['name']} should start with nebo_"

    def test_dispatcher_handles_nebo_prefixed_tools(self) -> None:
        """handle_tool_call should recognize nebo_ prefixed tool names."""
        from nebo.mcp.server import handle_tool_call
        import asyncio
        # This should not return "Unknown tool" error
        result = asyncio.get_event_loop().run_until_complete(
            handle_tool_call("nebo_get_run_history", {}, "http://localhost:19999")
        )
        # It may fail to connect, but should not say "Unknown tool"
        assert "Unknown tool" not in str(result.get("error", ""))


class TestMCPObservationTools:
    """Tests for MCP observation tools against in-process state."""

    def setup_method(self) -> None:
        SessionState.reset_singleton()

    @pytest.mark.asyncio
    async def test_get_description_daemon_unreachable(self) -> None:
        """When the daemon is down, observation tools surface that
        explicitly. The MCP bridge runs in its own process and cannot
        reach the user's pipeline SDK state, so there's no fallback to
        substitute."""
        from nebo.mcp.tools import get_description

        result = await get_description(server_url="http://localhost:19999")
        assert "error" in result
        assert "daemon unreachable" in result["error"]
        assert "hint" in result

    @pytest.mark.asyncio
    async def test_get_metrics_not_found(self) -> None:
        """Should return error when loggable not found."""
        from nebo.mcp.tools import get_metrics
        result = await get_metrics("nonexistent_loggable")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_get_loggable_status_not_found(self) -> None:
        """get_loggable_status should return error when loggable missing."""
        from nebo.mcp.tools import get_loggable_status
        result = await get_loggable_status("nonexistent", server_url="http://localhost:19999")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_get_text_accepts_loggable_id_kwarg(self) -> None:
        """get_text should accept loggable_id= filter keyword."""
        from nebo.mcp.tools import get_text
        result = await get_text(loggable_id="some_id", server_url="http://localhost:19999")
        # Should not raise; structure returns {"texts": [...]}
        assert "texts" in result or "error" in result

    def test_mcp_server_registers_loggable_status_tool(self) -> None:
        """MCP_TOOLS should expose nebo_get_loggable_status (renamed from nebo_get_node_status)."""
        from nebo.mcp.server import MCP_TOOLS
        names = [t["name"] for t in MCP_TOOLS]
        assert "nebo_get_loggable_status" in names
        assert "nebo_get_node_status" not in names

    def test_mcp_server_registers_get_text_tool(self) -> None:
        """The text-read tool is nebo_get_text (renamed from nebo_get_logs)."""
        from nebo.mcp.server import MCP_TOOLS
        names = [t["name"] for t in MCP_TOOLS]
        assert "nebo_get_text" in names
        assert "nebo_get_logs" not in names


class TestMCPLoadFileTool:
    """Tests for the nebo_load_file MCP tool."""

    def test_load_file_tool_exists(self) -> None:
        """nebo_load_file should be in MCP_TOOLS."""
        from nebo.mcp.server import MCP_TOOLS
        names = [t["name"] for t in MCP_TOOLS]
        assert "nebo_load_file" in names

    @pytest.mark.asyncio
    async def test_handle_load_file_tool(self) -> None:
        """nebo_load_file should handle nonexistent files gracefully."""
        from nebo.mcp.server import handle_tool_call
        result = await handle_tool_call(
            "nebo_load_file",
            {"filepath": "/nonexistent/file.nebo"},
            "http://localhost:19999",
        )
        assert "error" in result or "status" in result


class TestMCPWriteTools:
    """Tests for MCP write tools (log_metric / log_image / log_audio /
    log_text). These don't need a live daemon — we exercise input
    validation and the URL-fetching guard rails directly."""

    @pytest.mark.asyncio
    async def test_write_tools_registered(self) -> None:
        from nebo.mcp.server import MCP_TOOLS
        names = {t["name"] for t in MCP_TOOLS}
        assert {
            "nebo_log_metric",
            "nebo_log_image",
            "nebo_log_audio",
            "nebo_log_text",
        }.issubset(names)

    @pytest.mark.asyncio
    async def test_log_metric_rejects_empty(self) -> None:
        from nebo.mcp.tools import log_metric
        result = await log_metric([])
        assert "error" in result

    @pytest.mark.asyncio
    async def test_log_metric_defaults_to_agent_loggable(self) -> None:
        # When loggable_id is omitted the entry targets __agent__ — the
        # sandbox loggable for entries authored by an external agent.
        # With no live daemon we get a daemon-write error, but the
        # validation should accept the entry.
        from nebo.mcp.tools import log_metric
        result = await log_metric(
            {"name": "loss", "value": 0.1},
            server_url="http://localhost:19999",
        )
        assert "error" in result
        assert "daemon write failed" in result["error"]

    @pytest.mark.asyncio
    async def test_log_metric_routes_default_loggable_to_agent(self) -> None:
        # Capture the events the tool would have posted and assert the
        # loggable_id was filled in to __agent__.
        captured: list[dict] = []

        def fake_post(path, payload, *, url=None, port=None, api_token=None, timeout=10.0):  # noqa: ARG001
            captured.extend(payload)
            return {"status": "ok"}

        import nebo.client as client_mod
        original = client_mod._post
        client_mod._post = fake_post  # type: ignore[assignment]
        try:
            from nebo.mcp.tools import log_metric
            await log_metric({"name": "loss", "value": 0.1})
        finally:
            client_mod._post = original  # type: ignore[assignment]

        metric_events = [e for e in captured if e.get("type") == "metric"]
        assert metric_events
        assert all(e["loggable_id"] == "__agent__" for e in metric_events)
        register_events = [e for e in captured if e.get("type") == "loggable_register"]
        assert register_events
        assert all(
            e["data"]["kind"] == "agent"
            for e in register_events
            if e["loggable_id"] == "__agent__"
        )

    @pytest.mark.asyncio
    async def test_log_image_requires_url_or_data(self) -> None:
        from nebo.mcp.tools import log_image
        # With no url/data we expect an error before any default loggable
        # routing matters; loggable_id can be omitted and still surfaces
        # the same media-payload error.
        result = await log_image({"name": "img"})
        assert "error" in result
        assert "url" in result["error"] or "data" in result["error"]

    @pytest.mark.asyncio
    async def test_log_image_passes_url_to_daemon(self) -> None:
        # URLs (including non-http schemes) are forwarded to the daemon
        # rather than fetched by the bridge. With no live daemon on port 19999
        # we get a write-failed error; with a live daemon the daemon handles
        # validation. Either way the tool should not crash.
        from nebo.mcp.tools import log_image
        result = await log_image(
            {"loggable_id": "x", "name": "img", "url": "file:///etc/passwd"},
            server_url="http://localhost:19999",
        )
        assert "error" in result
        assert "daemon write failed" in result["error"]

    @pytest.mark.asyncio
    async def test_log_text_accepts_single_entry(self) -> None:
        # No live daemon → returns a daemon-write error, not a validation
        # error. We just want to confirm the input shape was accepted.
        from nebo.mcp.tools import log_text
        result = await log_text(
            {"loggable_id": "__global__", "message": "hello"},
            server_url="http://localhost:19999",
        )
        assert "error" in result
        assert "daemon write failed" in result["error"]

    @pytest.mark.asyncio
    async def test_log_text_defaults_loggable_to_agent(self) -> None:
        # Omitting loggable_id should route to __agent__ (the sandbox for
        # agent-authored entries), not __global__.
        captured: list[dict] = []

        def fake_post(path, payload, *, url=None, port=None, api_token=None, timeout=10.0):  # noqa: ARG001
            captured.extend(payload)
            return {"status": "ok"}

        import nebo.client as client_mod
        original = client_mod._post
        client_mod._post = fake_post  # type: ignore[assignment]
        try:
            from nebo.mcp.tools import log_text
            await log_text({"message": "hello from agent"})
        finally:
            client_mod._post = original  # type: ignore[assignment]

        text_events = [e for e in captured if e.get("type") == "text"]
        assert text_events
        assert all(e["loggable_id"] == "__agent__" for e in text_events)
        assert all("level" not in e for e in text_events)


class TestMCPWaitForAlert:
    """Tests for the nebo_wait_for_alert MCP tool."""

    def test_wait_for_alert_tool_registered(self) -> None:
        from nebo.mcp.server import MCP_TOOLS
        names = [t["name"] for t in MCP_TOOLS]
        assert "nebo_wait_for_alert" in names
        assert "nebo_wait_for_event" not in names

    def test_wait_for_alert_schema(self) -> None:
        from nebo.mcp.server import MCP_TOOLS
        tool = next(t for t in MCP_TOOLS if t["name"] == "nebo_wait_for_alert")
        props = tool["inputSchema"]["properties"]
        assert "run_id" in props
        assert "timeout" in props
        assert "min_level" in props
        assert props["min_level"]["type"] == "integer"
        assert "run_id" in tool["inputSchema"].get("required", [])

    @pytest.mark.asyncio
    async def test_wait_for_alert_daemon_unreachable(self) -> None:
        from nebo.mcp.tools import wait_for_alert
        result = await wait_for_alert("r1", server_url="http://localhost:19999")
        assert "error" in result
        assert "daemon unreachable" in result["error"]

    @pytest.mark.asyncio
    async def test_dispatcher_routes_wait_for_alert(self) -> None:
        from nebo.mcp.server import handle_tool_call
        result = await handle_tool_call(
            "nebo_wait_for_alert",
            {"run_id": "r1", "timeout": 5, "min_level": 30},
            "http://localhost:19999",
        )
        assert "error" in result
        assert "Unknown tool" not in str(result.get("error", ""))


class TestMCPDispatcher:
    """Tests for the MCP tool dispatcher."""

    def setup_method(self) -> None:
        SessionState.reset_singleton()

    @pytest.mark.asyncio
    async def test_unknown_tool(self) -> None:
        """Should return error for unknown tool names."""
        from nebo.mcp.server import handle_tool_call
        result = await handle_tool_call("nonexistent_tool", {})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_get_run_history_no_daemon(self) -> None:
        """Should return error when daemon not available."""
        from nebo.mcp.tools import get_run_history
        result = await get_run_history("http://localhost:19999")
        assert "error" in result
