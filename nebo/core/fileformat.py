"""Nebo binary file format -- append-only, MessagePack-based log files.

File structure:
    [Header]
      magic: b"nebo" (4 bytes)
      version: u16 big-endian (currently 3)
      metadata_size: u32 big-endian
      metadata: msgpack map {run_id, script_path, started_at, nebo_version,
                args, and optionally run_name, group} — the header is an open
                map; run_name/group are additive and may be absent on older
                files (the shallow watcher reads them to list a header-only run)

    [Entry]*
      type_byte: u8 (entry type index)
      size: u32 big-endian (payload size in bytes)
      payload: msgpack map (entry-specific data)

Format versions:
    v1: top-level / entry-type names used the legacy ``node`` /
        ``node_register`` / ``data.node_id`` spelling. In-memory events have
        since been renamed to ``loggable_id`` / ``loggable_register`` /
        ``data.loggable_id``, so the reader translates v1 files on the way
        out for backwards compatibility.
    v2: writes ``loggable_id`` / ``loggable_register`` / ``data.loggable_id``
        natively. Adds a new entry-type code ``loggable_register`` = 16.
        The legacy ``node_register`` code (4) stays in the table so v1 files
        decode. Labels on image events pass through unchanged.
    v3: metric entries carry ``metric_type`` and ``tags`` inline on the
        payload. On read, v2 metric entries are upgraded on the fly:
        ``metric_type`` defaults to ``"line"`` and ``tags`` defaults to ``[]``.
        The on-disk format is otherwise identical to v2.
    v4: two changes, both producer-side (the reader is pure passthrough):

        * New entry type ``metric_batch`` (code 20) — a columnar batch of
          accumulating-metric points produced by the transport coalescer
          (``nebo/core/coalesce.py``). Payload::

              {type: "metric_batch", loggable_id, name,
               metric_type: "line" | "scatter",
               steps: [int, ...], timestamps: [float, ...],
               values: [...],          # parallel arrays, length N
               tags: [str, ...],       # whole-batch
               colors: bool}           # optional, whole-batch

          Equivalence rule: a batch of length N is semantically identical
          to N consecutive v3 ``metric`` entries with the shared fields
          copied onto each (``expand_metric_batch`` is the inverse).
          Plain ``metric`` entries remain legal in v4 for all types;
          snapshot types (bar/pie/histogram) are never batched.

        * Image/audio ``data`` is raw bytes (msgpack bin) instead of a
          base64 ASCII string. Consumers accept both; base64 encoding now
          only happens at the JSON wire boundary (network transport).

        * (Later amendment, no version bump.) Text entries are written as
          ``text`` (code 9): payload ``{type: "text", loggable_id, name,
          message, step, timestamp}`` — no level field. ``log`` (code 0)
          is the legacy spelling of the same entry; readers keep it in the
          table and every ingest path normalizes ``log`` → ``text``, so
          older files decode unchanged.

        * (Later amendment, no version bump.) The **action modality** adds
          two entry types::

              body_model     (21)  {type, loggable_id, name, model_id,
                                    body_names, source_format,
                                    data: <glb bytes>}
              body_transform (22)  {type, loggable_id, name, step, timestamp,
                                    instances: {label: {model: model_id,
                                        pos_quat_xyzw: <7N f32 LE bytes>}}}

          ``body_model`` is a media entry like ``image`` / ``audio``: its
          ``data`` holds a self-contained GLB whose ``sha256(...)[:16]`` is
          both ``model_id`` and the daemon's media id.

          ``body_transform`` is one frame of a 3D scene. Each instance
          carries ``7 * N`` values -- ``[x, y, z, qx, qy, qz, qw]`` per body,
          in the model's body order. Quaternions are **vector-scalar
          (xyzw)**; that is the sole accepted convention and it is named in
          the field. Poses are **world transforms**, never joint
          coordinates, so no consumer needs forward kinematics.

          The payload is packed as **little-endian float32 bytes** (msgpack
          bin): msgpack has no float32 for Python floats, so a list costs
          9 B per value against 4, and positions are millimetre-scale while
          quaternions are unit. Readers go through
          ``nebo/logging/bodies.py:decode_poses``, which also accepts a
          plain list (files written before this change) and base64 -- the
          same accept-both rule media follows.

          These codes are additive and deliberately do **not** bump
          ``FORMAT_VERSION``. An older reader maps an unknown type byte to
          ``unknown_<n>`` and recovers the real type from ``payload["type"]``
          (the same path that ingests alert frames), then drops the event it
          does not understand -- whereas a version bump would make
          ``read_header`` reject the whole file.

Event semantics note: ``run_completed`` (code 16) is a *writer-finalization
marker* only — it flushes the file's final frame and, on the daemon, closes
the per-run writer. It carries no lifecycle state: there is no ``ended_at``
and no notion of *when* or *whether* a run ended (a crashed run simply never
gets one). Recency is derived from the last event's timestamp instead.
"""

from __future__ import annotations

import struct
import time
from typing import Any, BinaryIO, Iterator, Optional

import msgpack

from nebo.core.coalesce import expand_metric_batch

__all__ = [
    "FORMAT_VERSION",
    "MAGIC",
    "ENTRY_TYPES",
    "ENTRY_TYPES_REVERSE",
    "NeboFileWriter",
    "NeboFileReader",
    "expand_metric_batch",
]

FORMAT_VERSION = 4
MAGIC = b"nebo"

# Entry-type string -> on-disk integer code.
#
# Codes are part of the on-disk format and must never be reassigned (doing so
# would break backwards compatibility with existing .nebo files). New entry
# types get the next free integer.
#
# ``node_register`` (code 4) is retained purely for reading v1 files; v2
# writers emit ``loggable_register`` (code 16) instead.
ENTRY_TYPES = {
    "log": 0,  # legacy spelling of "text"; kept for backward read compat
    "metric": 1,
    "image": 2,
    "audio": 3,
    "node_register": 4,  # v1-only; kept for backward read compat
    "edge": 5,
    # 6 was "error" (removed)
    # 7 was "ask" (removed)
    "ui_config": 8,
    "text": 9,  # named text streams; writers emit this instead of "log"
    "progress": 10,
    "config": 11,
    "description": 12,
    "node_executed": 13,
    # 14 was "ask_response" (removed)
    "run_start": 15,
    "run_completed": 16,
    # 17 was "pause_state" (removed)
    "run_config": 18,
    "loggable_register": 19,  # v2: replaces node_register
    "metric_batch": 20,  # v4: columnar batch of line/scatter points
    "body_model": 21,  # action modality: a compiled GLB body model
    "body_transform": 22,  # action modality: one scene frame of poses
}

ENTRY_TYPES_REVERSE = {v: k for k, v in ENTRY_TYPES.items()}


# --- v1 -> in-memory translation --------------------------------------------
#
# v1 .nebo files stored the legacy field names:
#   * top-level field   : ``node``              (in-memory: ``loggable_id``)
#   * entry / type name : ``node_register``     (in-memory: ``loggable_register``)
#   * nested in ``data``: ``node_id``           (in-memory: ``loggable_id``)
#
# v2 writes the in-memory shape directly, so these translators are only
# invoked when the reader detects a v1 file header.


def _v1_entry_type_to_in_memory(entry_type: str) -> str:
    """v1 on-disk entry type -> in-memory entry type."""
    if entry_type == "node_register":
        return "loggable_register"
    return entry_type


def _v1_payload_to_in_memory(payload: dict[str, Any]) -> dict[str, Any]:
    """v1 on-disk payload -> in-memory payload (node -> loggable_id)."""
    if not isinstance(payload, dict):
        return payload
    out = dict(payload)
    if "node" in out:
        out["loggable_id"] = out.pop("node")
    if out.get("type") == "node_register":
        out["type"] = "loggable_register"
    data = out.get("data")
    if isinstance(data, dict) and "node_id" in data:
        new_data = dict(data)
        new_data["loggable_id"] = new_data.pop("node_id")
        out["data"] = new_data
    return out


class NeboFileWriter:
    """Append-only writer for .nebo files (emits format v3)."""

    def __init__(
        self,
        stream: BinaryIO,
        run_id: str,
        script_path: str,
        args: Optional[list[str]] = None,
        run_name: Optional[str] = None,
        group: str = "",
    ) -> None:
        self._stream = stream
        self._run_id = run_id
        self._script_path = script_path
        self._args = args or []
        self._run_name = run_name
        self._group = group
        self._started_at = time.time()

    def write_header(self) -> None:
        """Write the file header (magic, version, metadata)."""
        self._stream.write(MAGIC)
        self._stream.write(struct.pack(">H", FORMAT_VERSION))

        metadata = {
            "run_id": self._run_id,
            "script_path": self._script_path,
            "started_at": self._started_at,
            "nebo_version": "0.1.0",
            "args": self._args,
        }
        # Optional additive keys (no format-version bump): the shallow watcher
        # reads these from the header so a header-only run lists with its name
        # and group. Absent on older files — readers treat the header as an
        # open map.
        if self._run_name is not None:
            metadata["run_name"] = self._run_name
        if self._group:
            metadata["group"] = self._group
        meta_bytes = msgpack.packb(metadata, use_bin_type=True)
        self._stream.write(struct.pack(">I", len(meta_bytes)))
        self._stream.write(meta_bytes)
        self._stream.flush()

    def write_entry(self, entry_type: str, payload: dict[str, Any]) -> tuple[int, int]:
        """Write a single log entry; return its ``(frame_start, frame_length)``.

        v2 is passthrough: the in-memory event dict is serialized as-is, so
        ``loggable_id`` / ``loggable_register`` / ``data.loggable_id`` land on
        disk verbatim. Labels (e.g. on image events) pass through unchanged.

        The returned span brackets the whole ``[type][u32 size][payload]``
        frame on the stream, so a media event's bytes can later be read back
        by reference (``RunCache._read_media_ref``) instead of duplicated into
        a blob row. Callers that don't need it (the SDK's FileTransport)
        simply ignore the return value.
        """
        type_byte = ENTRY_TYPES.get(entry_type, 255)
        payload_bytes = msgpack.packb(payload, use_bin_type=True)

        start = self._stream.tell()
        self._stream.write(struct.pack(">B", type_byte))
        self._stream.write(struct.pack(">I", len(payload_bytes)))
        self._stream.write(payload_bytes)
        # No flush here — callers own flush cadence (FileTransport flushes
        # once per drain tick instead of once per entry).
        return start, 5 + len(payload_bytes)

    def close(self) -> None:
        """Flush the stream."""
        self._stream.flush()


class NeboFileReader:
    """Reader for .nebo files (supports formats v1, v2, and v3)."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        # Populated by read_header(); None until the header has been parsed.
        # Raw-read paths that skip read_header() default to treating data as
        # the current format (passthrough), which matches all freshly-written
        # files.
        self._version: Optional[int] = None

    def read_header(self) -> dict[str, Any]:
        """Read and validate the file header. Returns metadata dict."""
        magic = self._stream.read(4)
        if magic != MAGIC:
            raise ValueError(f"Not a .nebo file: invalid magic {magic!r}")

        version = struct.unpack(">H", self._stream.read(2))[0]
        if version > FORMAT_VERSION:
            raise ValueError(f"Unsupported .nebo format version {version}")
        self._version = version

        meta_size = struct.unpack(">I", self._stream.read(4))[0]
        meta_bytes = self._stream.read(meta_size)
        return msgpack.unpackb(meta_bytes, raw=False)

    def read_next_entry_raw(self) -> Optional[dict[str, Any]]:
        """Read the next entry in its raw on-disk shape (no translation).

        For v2 files this is the same as :meth:`read_next_entry`. For v1 files
        the returned dict carries the legacy field names (``node``,
        ``node_register``, ``data.node_id``). Returns None at EOF.
        """
        type_data = self._stream.read(1)
        if not type_data:
            return None

        type_byte = struct.unpack(">B", type_data)[0]
        size = struct.unpack(">I", self._stream.read(4))[0]
        payload_bytes = self._stream.read(size)
        payload = msgpack.unpackb(payload_bytes, raw=False)

        entry_type = ENTRY_TYPES_REVERSE.get(type_byte, f"unknown_{type_byte}")
        return {"type": entry_type, "payload": payload}

    def _translate(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Translate a raw on-disk entry to the in-memory shape.

        For v3 files the entry passes through unchanged. For v1 files the
        legacy on-disk spellings (``node`` / ``node_register`` /
        ``data.node_id``) are rewritten to their in-memory equivalents
        (``loggable_id`` / ``loggable_register`` / ``data.loggable_id``).
        For v2 (and older) metric entries, ``metric_type`` and ``tags`` are
        synthesized onto the payload (``"line"`` / ``[]``) since they did
        not exist on-disk prior to v3.
        """
        if self._version == 1:
            entry = {
                "type": _v1_entry_type_to_in_memory(entry["type"]),
                "payload": _v1_payload_to_in_memory(entry["payload"]),
            }
        # v2 files predate metric_type / tags; synthesize defaults so callers
        # never have to care what format version produced the file.
        if self._version is not None and self._version <= 2:
            if entry["type"] == "metric" and isinstance(entry["payload"], dict):
                payload = dict(entry["payload"])
                payload.setdefault("metric_type", "line")
                payload.setdefault("tags", [])
                entry = {"type": entry["type"], "payload": payload}
        return entry

    def read_next_entry(self) -> Optional[dict[str, Any]]:
        """Read the next entry and translate it to the in-memory shape.

        See :meth:`_translate` for the per-version rules. Returns None at EOF.
        """
        entry = self.read_next_entry_raw()
        if entry is None:
            return None
        return self._translate(entry)

    def read_entries_incremental(self) -> Iterator[tuple[dict[str, Any], int, int]]:
        """Yield ``(translated_entry, frame_start, frame_end)`` triples.

        Unlike :meth:`read_entries`, a truncated tail frame (a writer caught
        mid-append, or a crash) does NOT raise: iteration stops cleanly and
        the stream is seeked back to the start of the torn frame, so callers
        that persist offsets (the daemon's directory watcher) can resume from
        exactly there once more bytes arrive.
        """
        while True:
            start = self._stream.tell()
            type_data = self._stream.read(1)
            if len(type_data) < 1:
                self._stream.seek(start)
                return
            size_data = self._stream.read(4)
            if len(size_data) < 4:
                self._stream.seek(start)
                return
            size = struct.unpack(">I", size_data)[0]
            payload_bytes = self._stream.read(size)
            if len(payload_bytes) < size:
                self._stream.seek(start)
                return
            try:
                payload = msgpack.unpackb(payload_bytes, raw=False)
            except Exception:
                # A complete-length but undecodable frame: either mid-file
                # corruption or a torn write that happens to have plausible
                # length bytes. Park here — retrying later is the only safe
                # option, and matches the watcher's existing behavior.
                self._stream.seek(start)
                return
            type_byte = struct.unpack(">B", type_data)[0]
            entry_type = ENTRY_TYPES_REVERSE.get(type_byte, f"unknown_{type_byte}")
            entry = self._translate({"type": entry_type, "payload": payload})
            yield entry, start, self._stream.tell()

    def skip_next_entry(self) -> bool:
        """Skip the next entry without parsing payload. Returns False at EOF."""
        type_data = self._stream.read(1)
        if not type_data:
            return False

        size = struct.unpack(">I", self._stream.read(4))[0]
        self._stream.seek(size, 1)  # seek relative to current position
        return True

    def read_entries_raw(self) -> Iterator[dict[str, Any]]:
        """Iterate over all entries in their raw on-disk shape (no translation)."""
        while True:
            entry = self.read_next_entry_raw()
            if entry is None:
                break
            yield entry

    def read_entries(self) -> Iterator[dict[str, Any]]:
        """Iterate over all entries, translated to the in-memory shape."""
        while True:
            entry = self.read_next_entry()
            if entry is None:
                break
            yield entry
