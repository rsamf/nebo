"""Tests for the server protocol and state."""

from __future__ import annotations

import json
import pytest

from nebo.server.protocol import Message, MessageType, encode_batch, decode_batch


class TestProtocol:
    """Tests for the message protocol."""

    def test_message_to_json(self) -> None:
        """Message should serialize to JSON."""
        msg = Message(type=MessageType.TEXT, data={"message": "hello"}, loggable_id="my_node")
        raw = msg.to_json()
        parsed = json.loads(raw)
        assert parsed["type"] == "text"
        assert parsed["data"]["message"] == "hello"
        assert parsed["loggable_id"] == "my_node"

    def test_message_from_json(self) -> None:
        """Message should deserialize from JSON."""
        raw = json.dumps({
            "type": "metric",
            "data": {"name": "loss", "value": 0.5},
            "timestamp": 1234567890.0,
            "loggable_id": "train",
        })
        msg = Message.from_json(raw)
        assert msg.type == MessageType.METRIC
        assert msg.data["value"] == 0.5
        assert msg.loggable_id == "train"

    def test_encode_decode_batch(self) -> None:
        """Batch encoding/decoding should round-trip."""
        events = [
            {"type": "text", "message": "hello"},
            {"type": "metric", "name": "loss", "value": 0.1},
        ]
        encoded = encode_batch(events)
        decoded = decode_batch(encoded)
        assert decoded == events

    def test_message_types_enum(self) -> None:
        """All expected message types should be defined."""
        assert MessageType.TEXT.value == "text"
        assert MessageType.METRIC.value == "metric"
        assert MessageType.PROGRESS.value == "progress"


class TestDaemonIngest:
    """Tests for the daemon state event ingestion."""

    @pytest.mark.asyncio
    async def test_ingest_text_event(self) -> None:
        """Daemon state should ingest text events."""
        from nebo.server.daemon import DaemonState

        state = DaemonState()
        state.create_run("test.py", run_id="r1")
        await state.ingest_events([{
            "type": "loggable_register",
            "data": {"loggable_id": "my_node", "func_name": "my_func"},
        }], "r1")
        assert "my_node" in state.runs["r1"].loggables

        await state.ingest_events([{
            "type": "text",
            "loggable_id": "my_node",
            "message": "test text",
        }], "r1")
        assert len(state.runs["r1"].texts) == 1

    @pytest.mark.asyncio
    async def test_ingest_edge(self) -> None:
        """Daemon state should track edges."""
        from nebo.server.daemon import DaemonState

        state = DaemonState()
        state.create_run("test.py", run_id="r1")
        await state.ingest_events([
            {"type": "loggable_register", "data": {"loggable_id": "a", "func_name": "a"}},
            {"type": "loggable_register", "data": {"loggable_id": "b", "func_name": "b"}},
        ], "r1")
        await state.ingest_events([{
            "type": "edge",
            "data": {"source": "a", "target": "b"},
        }], "r1")
        assert len(state.runs["r1"].edges) == 1
        assert state.runs["r1"].loggables["b"].is_source is False

    @pytest.mark.asyncio
    async def test_ingest_error_event_is_ignored(self) -> None:
        """Error reporting is removed: `error` events are silently dropped."""
        from nebo.server.daemon import DaemonState

        state = DaemonState()
        state.create_run("test.py", run_id="r1")
        await state.ingest_events([
            {"type": "loggable_register", "data": {"loggable_id": "err_node", "func_name": "err"}},
        ], "r1")
        await state.ingest_events([{
            "type": "error",
            "loggable_id": "err_node",
            "data": {"error": "something went wrong", "type": "RuntimeError"},
        }], "r1")
        assert not hasattr(state.runs["r1"].loggables["err_node"], "errors")
        assert state.runs["r1"].significant_events == []
