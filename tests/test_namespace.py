"""Public import-surface contract tests.

Each test pins a path callers depend on. A surprise break in any of these
breaks downstream users, so the surface change is worth a deliberate
review.
"""

from __future__ import annotations


class TestNamespaceImports:
    """Verify that the public import surface is wired up."""

    def test_import_nebo(self) -> None:
        """import nebo should work."""
        import nebo
        assert hasattr(nebo, "fn")
        assert hasattr(nebo, "log_text")
        assert hasattr(nebo, "log_line")
        assert hasattr(nebo, "log_bar")
        assert hasattr(nebo, "log_pie")
        assert hasattr(nebo, "log_scatter")
        assert hasattr(nebo, "log_histogram")
        assert hasattr(nebo, "init")
        # log_metric was split into the typed log_* functions above
        assert not hasattr(nebo, "log_metric")
        # nb.log was renamed to nb.log_text; a deprecated forwarding shim
        # remains (warns once per process, removed in a later release).
        assert hasattr(nebo, "log")

    def test_import_nebo_as_nb(self) -> None:
        """import nebo as nb should work."""
        import nebo as nb
        assert hasattr(nb, "fn")
        assert hasattr(nb, "log_text")

    def test_import_core_state(self) -> None:
        """nebo.core.state should be importable."""
        from nebo.core.state import get_state, SessionState, _current_node
        assert get_state is not None

    def test_import_core_decorators(self) -> None:
        """nebo.core.decorators should be importable."""
        from nebo.core.decorators import fn
        assert fn is not None

    def test_import_core_config(self) -> None:
        """nebo.core.config should be importable."""
        from nebo.core.config import log_cfg
        assert log_cfg is not None

    def test_import_core_tracker(self) -> None:
        """nebo.core.tracker should be importable."""
        from nebo.core.tracker import track
        assert track is not None

    def test_import_core_dag(self) -> None:
        """nebo.core.dag should be importable."""
        from nebo.core.dag import get_sources, get_topology_order, get_dag_summary
        assert get_sources is not None

    def test_import_core_client(self) -> None:
        """nebo.core.client should be importable."""
        from nebo.core.client import NetworkTransport as DaemonClient
        assert DaemonClient is not None

    def test_import_logging_logger(self) -> None:
        """nebo.logging.logger should be importable."""
        from nebo.logging.logger import (
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
        assert log_text is not None
        assert log_line is not None and log_bar is not None
        assert log_pie is not None and log_scatter is not None
        assert log_histogram is not None

    def test_import_logging_queue(self) -> None:
        """nebo.logging.queue should be importable."""
        from nebo.logging.queue import LogQueue
        assert LogQueue is not None

    def test_import_logging_serializers(self) -> None:
        """nebo.logging.serializers should be importable."""
        from nebo.logging.serializers import serialize_image, serialize_audio
        assert serialize_image is not None

    def test_import_server_daemon(self) -> None:
        """nebo.server.daemon should be importable."""
        from nebo.server.daemon import DaemonState, create_daemon_app
        assert DaemonState is not None

    def test_import_server_protocol(self) -> None:
        """nebo.server.protocol should be importable."""
        from nebo.server.protocol import MessageType, Message
        assert MessageType is not None

    def test_import_mcp_server(self) -> None:
        """nebo.mcp.server should be importable."""
        from nebo.mcp.server import MCP_TOOLS, handle_tool_call
        assert MCP_TOOLS is not None

    def test_import_mcp_tools(self) -> None:
        """nebo.mcp.tools should be importable."""
        from nebo.mcp import tools
        assert hasattr(tools, "get_graph")

    def test_import_cli(self) -> None:
        """nebo.cli should be importable."""
        from nebo.cli import main
        assert main is not None

    def test_import_extensions(self) -> None:
        """nebo.extensions should be importable."""
        import nebo.extensions
        assert nebo.extensions is not None

    def test_init_reads_env_vars(self) -> None:
        """nebo.init must read NEBO_URI / NEBO_RUN_ID / NEBO_FLUSH_INTERVAL.

        These env vars are the contract external runners use (HF Spaces,
        CI wrappers) to override SDK defaults at process start. Removing
        them silently would break those callers.
        """
        import nebo
        import inspect
        source = inspect.getsource(nebo.init)
        assert "NEBO_URI" in source
        assert "NEBO_RUN_ID" in source
        assert "NEBO_FLUSH_INTERVAL" in source

    def test_pyproject_has_nebo_script(self) -> None:
        """pyproject.toml should have nebo = nebo.cli:main entry point."""
        from pathlib import Path
        toml_path = Path(__file__).parent.parent / "pyproject.toml"
        content = toml_path.read_text()
        assert 'nebo = "nebo.cli:main"' in content
