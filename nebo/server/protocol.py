"""Client-server message protocol for nebo."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


class MessageType(str, Enum):
    """Types of messages in the protocol."""
    # Named text streams. The legacy wire/file value is "log" — readers
    # accept both, writers emit "text" (see daemon ingest normalization).
    TEXT = "text"
    METRIC = "metric"
    METRIC_BATCH = "metric_batch"
    IMAGE = "image"
    ALERT = "alert"
    AUDIO = "audio"
    PROGRESS = "progress"
    LOGGABLE_REGISTER = "loggable_register"
    EDGE = "edge"
    GRAPH_UPDATE = "graph_update"
    HEALTH = "health"
    CONFIG = "config"
    DESCRIPTION = "description"
    TREE_UPDATED = "tree_updated"


@dataclass
class Message:
    """A protocol message."""
    type: MessageType
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    loggable_id: Optional[str] = None

    def to_json(self) -> str:
        d = {
            "type": self.type.value if isinstance(self.type, MessageType) else self.type,
            "data": self.data,
            "timestamp": self.timestamp,
            "loggable_id": self.loggable_id,
        }
        return json.dumps(d, default=str)

    @classmethod
    def from_json(cls, raw: str) -> Message:
        d = json.loads(raw)
        return cls(
            type=MessageType(d["type"]) if d.get("type") in MessageType._value2member_map_ else d.get("type", "text"),
            data=d.get("data", {}),
            timestamp=d.get("timestamp", time.time()),
            loggable_id=d.get("loggable_id"),
        )


def encode_batch(events: list[dict[str, Any]]) -> str:
    """Encode a batch of events as JSON."""
    return json.dumps(events, default=str)


def decode_batch(raw: str) -> list[dict[str, Any]]:
    """Decode a batch of events from JSON."""
    return json.loads(raw)
