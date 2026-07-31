"""Tests for .nebo file format."""

import io
import struct
import tempfile
import pytest
import msgpack


def test_write_header():
    """Writer should produce a valid header with magic, version, metadata."""
    from nebo.core.fileformat import NeboFileWriter

    buf = io.BytesIO()
    writer = NeboFileWriter(buf, run_id="test-run", script_path="test.py")
    writer.write_header()
    writer.close()

    buf.seek(0)
    magic = buf.read(4)
    assert magic == b"nebo"

    version = struct.unpack(">H", buf.read(2))[0]
    assert version == 4

    meta_size = struct.unpack(">I", buf.read(4))[0]
    meta = msgpack.unpackb(buf.read(meta_size), raw=False)
    assert meta["run_id"] == "test-run"
    assert meta["script_path"] == "test.py"


def test_write_and_read_entries():
    """Round-trip: write entries then read them back."""
    from nebo.core.fileformat import NeboFileWriter, NeboFileReader

    buf = io.BytesIO()
    writer = NeboFileWriter(buf, run_id="test-run", script_path="test.py")
    writer.write_header()

    writer.write_entry("text", {"node": "my_func", "message": "hello", "timestamp": 1000.0})
    writer.write_entry("metric", {"node": "my_func", "name": "loss", "value": 0.5, "step": 0, "timestamp": 1000.1})
    writer.close()

    buf.seek(0)
    reader = NeboFileReader(buf)
    meta = reader.read_header()
    assert meta["run_id"] == "test-run"

    entries = list(reader.read_entries())
    assert len(entries) == 2
    assert entries[0]["type"] == "text"
    assert entries[0]["payload"]["message"] == "hello"
    assert entries[1]["type"] == "metric"
    assert entries[1]["payload"]["value"] == 0.5


def test_write_binary_media():
    """Images and audio should be stored as raw bytes, not base64."""
    from nebo.core.fileformat import NeboFileWriter, NeboFileReader

    image_bytes = b"\x89PNG\r\n" + b"\x00" * 100

    buf = io.BytesIO()
    writer = NeboFileWriter(buf, run_id="test-run", script_path="test.py")
    writer.write_header()
    writer.write_entry("image", {"node": "my_func", "name": "out", "data": image_bytes, "timestamp": 1000.0})
    writer.close()

    buf.seek(0)
    reader = NeboFileReader(buf)
    reader.read_header()
    entries = list(reader.read_entries())
    assert len(entries) == 1
    assert entries[0]["payload"]["data"] == image_bytes


def test_skip_entry_by_size():
    """Reader should be able to skip entries using the size field."""
    from nebo.core.fileformat import NeboFileWriter, NeboFileReader

    buf = io.BytesIO()
    writer = NeboFileWriter(buf, run_id="test-run", script_path="test.py")
    writer.write_header()
    writer.write_entry("text", {"message": "first"})
    writer.write_entry("text", {"message": "second"})
    writer.write_entry("text", {"message": "third"})
    writer.close()

    buf.seek(0)
    reader = NeboFileReader(buf)
    reader.read_header()

    # Read first entry
    entry = reader.read_next_entry()
    assert entry["payload"]["message"] == "first"

    # Skip second entry
    reader.skip_next_entry()

    # Read third entry
    entry = reader.read_next_entry()
    assert entry["payload"]["message"] == "third"


def test_file_on_disk():
    """Write to a real file and read it back."""
    from nebo.core.fileformat import NeboFileWriter, NeboFileReader

    with tempfile.NamedTemporaryFile(suffix=".nebo", delete=False) as f:
        path = f.name
        writer = NeboFileWriter(f, run_id="disk-test", script_path="script.py")
        writer.write_header()
        writer.write_entry("text", {"message": "from disk"})
        writer.close()

    with open(path, "rb") as f:
        reader = NeboFileReader(f)
        meta = reader.read_header()
        assert meta["run_id"] == "disk-test"
        entries = list(reader.read_entries())
        assert len(entries) == 1
        assert entries[0]["payload"]["message"] == "from disk"

    import os
    os.unlink(path)


def _write_v1_nebo_file(buf, *, run_id: str, script_path: str, entries):
    """Hand-craft a v1-format .nebo file into ``buf``.

    The repo no longer has a v1 writer (v2 is passthrough), so we synthesize
    the v1 header + entries directly to exercise the reader's v1 translation
    path. ``entries`` is a list of ``(entry_type_byte, payload_dict)`` tuples.
    """
    from nebo.core.fileformat import MAGIC

    # Header: magic + version=1 + meta_size + meta
    buf.write(MAGIC)
    buf.write(struct.pack(">H", 1))
    meta_bytes = msgpack.packb(
        {
            "run_id": run_id,
            "script_path": script_path,
            "started_at": 0.0,
            "nebo_version": "0.1.0",
            "args": [],
        },
        use_bin_type=True,
    )
    buf.write(struct.pack(">I", len(meta_bytes)))
    buf.write(meta_bytes)

    # Entries: type_byte (u8) + size (u32 BE) + msgpack payload
    for type_byte, payload in entries:
        payload_bytes = msgpack.packb(payload, use_bin_type=True)
        buf.write(struct.pack(">B", type_byte))
        buf.write(struct.pack(">I", len(payload_bytes)))
        buf.write(payload_bytes)


def test_fileformat_v1_reader_translates_node_to_loggable_id():
    """v1 reader path: on-disk node / node_register -> in-memory loggable_*.

    v2 writes the in-memory shape natively, so the v1 translation only runs
    when the reader encounters an older (v1) file. We craft a v1 file by hand
    to exercise that path.
    """
    from nebo.core.fileformat import NeboFileReader

    # type_byte 0 = "log", type_byte 4 = "node_register" (v1 name)
    buf = io.BytesIO()
    _write_v1_nebo_file(
        buf,
        run_id="translate-test",
        script_path="t.py",
        entries=[
            (0, {"type": "log", "node": "x", "message": "hi", "timestamp": 1.0}),
            (
                4,
                {
                    "type": "node_register",
                    "node": "x",
                    "data": {"node_id": "x", "kind": "fn", "func_name": "x"},
                },
            ),
        ],
    )

    # Raw read preserves the on-disk v1 shape (no translation).
    buf.seek(0)
    raw_reader = NeboFileReader(buf)
    raw_reader.read_header()
    raw_entries = list(raw_reader.read_entries_raw())
    assert raw_entries[0]["type"] == "log"
    assert raw_entries[0]["payload"].get("node") == "x"
    assert "loggable_id" not in raw_entries[0]["payload"]
    assert raw_entries[1]["type"] == "node_register"
    assert raw_entries[1]["payload"].get("node") == "x"
    assert raw_entries[1]["payload"]["type"] == "node_register"
    assert raw_entries[1]["payload"]["data"].get("node_id") == "x"
    assert "loggable_id" not in raw_entries[1]["payload"]["data"]

    # High-level read translates back to loggable_id / loggable_register.
    buf.seek(0)
    reader = NeboFileReader(buf)
    reader.read_header()
    entries = list(reader.read_entries())
    assert entries[0]["type"] == "log"
    assert entries[0]["payload"]["loggable_id"] == "x"
    assert "node" not in entries[0]["payload"]
    assert entries[1]["type"] == "loggable_register"
    assert entries[1]["payload"]["loggable_id"] == "x"
    assert entries[1]["payload"]["type"] == "loggable_register"
    assert entries[1]["payload"]["data"]["loggable_id"] == "x"
    assert "node_id" not in entries[1]["payload"]["data"]
    assert "node" not in entries[1]["payload"]


def test_fileformat_v2_writes_loggable_id_natively():
    """v2 writer stores `loggable_id` on disk (no node translation)."""
    from nebo.core.fileformat import NeboFileWriter, NeboFileReader

    buf = io.BytesIO()
    writer = NeboFileWriter(buf, run_id="r1", script_path="s.py")
    writer.write_header()
    writer.write_entry("text", {"loggable_id": "x", "message": "hi", "timestamp": 1.0})
    writer.close()

    buf.seek(0)
    reader = NeboFileReader(buf)
    reader.read_header()
    raw = list(reader.read_entries_raw())
    assert raw[0]["type"] == "text"
    assert raw[0]["payload"].get("loggable_id") == "x"
    assert "node" not in raw[0]["payload"]


def test_fileformat_v2_preserves_image_labels():
    """Labels round-trip through v2 unchanged."""
    from nebo.core.fileformat import NeboFileWriter, NeboFileReader

    buf = io.BytesIO()
    writer = NeboFileWriter(buf, run_id="r1", script_path="s.py")
    writer.write_header()
    writer.write_entry(
        "image",
        {
            "loggable_id": "x",
            "name": "im",
            "data": "AA==",
            "labels": {"boxes": [[1, 2, 3, 4]], "points": [[5, 6]]},
        },
    )
    writer.close()

    buf.seek(0)
    reader = NeboFileReader(buf)
    reader.read_header()
    events = list(reader.read_entries())
    assert events[0]["payload"]["labels"] == {
        "boxes": [[1, 2, 3, 4]],
        "points": [[5, 6]],
    }
    assert events[0]["payload"]["loggable_id"] == "x"
    assert "node" not in events[0]["payload"]


def test_fileformat_v2_writes_loggable_register_entry_type():
    """v2 writer emits `loggable_register` entry type; raw read sees it."""
    from nebo.core.fileformat import NeboFileWriter, NeboFileReader

    buf = io.BytesIO()
    writer = NeboFileWriter(buf, run_id="r1", script_path="s.py")
    writer.write_header()
    writer.write_entry(
        "loggable_register",
        {
            "loggable_id": "x",
            "data": {"loggable_id": "x", "kind": "node", "func_name": "f"},
        },
    )
    writer.close()

    buf.seek(0)
    reader = NeboFileReader(buf)
    reader.read_header()
    raw = list(reader.read_entries_raw())
    assert raw[0]["type"] == "loggable_register"
    assert raw[0]["payload"].get("loggable_id") == "x"
    assert "node" not in raw[0]["payload"]


def test_fileformat_v3_preserves_metric_type_and_tags():
    """v3 writer + reader round-trip preserves metric_type and tags."""
    from nebo.core.fileformat import NeboFileWriter, NeboFileReader

    buf = io.BytesIO()
    writer = NeboFileWriter(buf, run_id="r1", script_path="s.py")
    writer.write_header()
    writer.write_entry(
        "metric",
        {
            "loggable_id": "n",
            "name": "loss",
            "metric_type": "line",
            "value": 0.5,
            "step": 0,
            "tags": ["warmup"],
        },
    )
    writer.close()

    buf.seek(0)
    reader = NeboFileReader(buf)
    reader.read_header()
    events = list(reader.read_entries())
    assert events[0]["type"] == "metric"
    assert events[0]["payload"]["metric_type"] == "line"
    assert events[0]["payload"]["tags"] == ["warmup"]
    assert events[0]["payload"]["value"] == 0.5


def test_fileformat_v2_metric_upgrades_to_line_with_empty_tags():
    """A v2 metric entry with no metric_type/tags is synthesized to line + []."""
    from nebo.core.fileformat import (
        NeboFileWriter,
        NeboFileReader,
        MAGIC,
    )

    # Write a v3 file, then patch the version field in the header to 2 so the
    # reader takes the v2-upgrade path. The on-disk entry layout did not change
    # between v2 and v3 -- only which payload fields metric entries carry --
    # so the rest of the file is byte-identical.
    buf = io.BytesIO()
    writer = NeboFileWriter(buf, run_id="r1", script_path="s.py")
    writer.write_header()
    writer.write_entry(
        "metric",
        {
            "loggable_id": "n",
            "name": "loss",
            "value": 0.5,
            "step": 0,
        },
    )
    writer.close()

    data = buf.getvalue()
    # Version is a u16 big-endian immediately after MAGIC (see write_header).
    version_offset = len(MAGIC)
    patched = (
        data[:version_offset]
        + struct.pack(">H", 2)
        + data[version_offset + 2:]
    )

    buf2 = io.BytesIO(patched)
    reader = NeboFileReader(buf2)
    meta = reader.read_header()
    assert meta["run_id"] == "r1"

    events = list(reader.read_entries())
    assert events[0]["type"] == "metric"
    # v2 metric record is upgraded to v3 shape by the reader.
    assert events[0]["payload"]["metric_type"] == "line"
    assert events[0]["payload"]["tags"] == []
    # Existing v2 fields survive the upgrade.
    assert events[0]["payload"]["value"] == 0.5
    assert events[0]["payload"]["loggable_id"] == "n"


class TestFormatV4:
    def test_format_version_is_4(self):
        from nebo.core.fileformat import ENTRY_TYPES, FORMAT_VERSION

        assert FORMAT_VERSION == 4
        assert ENTRY_TYPES["metric_batch"] == 20

    def test_metric_batch_roundtrip(self, tmp_path):
        from nebo.core.fileformat import NeboFileReader, NeboFileWriter

        payload = {
            "type": "metric_batch",
            "loggable_id": "a",
            "name": "loss",
            "metric_type": "line",
            "steps": [0, 1, 2],
            "timestamps": [1.0, 2.0, 3.0],
            "values": [0.5, 0.4, 0.3],
            "tags": ["train"],
        }
        path = tmp_path / "v4.nebo"
        with path.open("wb") as f:
            w = NeboFileWriter(f, run_id="r", script_path="s.py")
            w.write_header()
            w.write_entry("metric_batch", payload)
        with path.open("rb") as f:
            r = NeboFileReader(f)
            meta = r.read_header()
            entries = list(r.read_entries())
        assert meta["run_id"] == "r"
        assert entries == [{"type": "metric_batch", "payload": payload}]

    def test_media_bytes_roundtrip(self, tmp_path):
        from nebo.core.fileformat import NeboFileReader, NeboFileWriter

        raw = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
        path = tmp_path / "bin.nebo"
        with path.open("wb") as f:
            w = NeboFileWriter(f, run_id="r", script_path="s.py")
            w.write_header()
            w.write_entry("image", {
                "type": "image", "loggable_id": "a", "name": "f",
                "data": raw, "step": None, "timestamp": 1.0,
            })
        with path.open("rb") as f:
            r = NeboFileReader(f)
            r.read_header()
            (entry,) = list(r.read_entries())
        assert entry["payload"]["data"] == raw
        assert isinstance(entry["payload"]["data"], bytes)

    def test_expand_reexported(self):
        from nebo.core.fileformat import expand_metric_batch

        out = expand_metric_batch({
            "type": "metric_batch", "loggable_id": "a", "name": "n",
            "metric_type": "line", "steps": [0], "timestamps": [1.0],
            "values": [2.0], "tags": [],
        })
        assert out[0]["value"] == 2.0


class TestTextEntryRename:
    """logs -> text rename at the file-format layer: writers emit entry
    code 9 ("text"); code 0 ("log") survives as a legacy read spelling."""

    def test_entry_codes(self):
        from nebo.core.fileformat import ENTRY_TYPES

        assert ENTRY_TYPES["text"] == 9   # what writers emit now
        assert ENTRY_TYPES["log"] == 0    # legacy spelling, read compat only

    def test_text_entry_frame_starts_with_type_byte_9(self):
        from nebo.core.fileformat import NeboFileWriter

        buf = io.BytesIO()
        writer = NeboFileWriter(buf, run_id="r", script_path="s.py")
        writer.write_header()
        header_end = buf.tell()
        writer.write_entry("text", {
            "type": "text", "loggable_id": "__global__", "name": "status",
            "message": "hi", "step": None, "timestamp": 1.0,
        })
        frame = buf.getvalue()[header_end:]
        assert frame[0] == 9

    def test_legacy_log_entry_frame_starts_with_type_byte_0(self):
        from nebo.core.fileformat import NeboFileWriter

        buf = io.BytesIO()
        writer = NeboFileWriter(buf, run_id="r", script_path="s.py")
        writer.write_header()
        header_end = buf.tell()
        writer.write_entry("log", {
            "type": "log", "loggable_id": "__global__", "name": "text",
            "message": "old", "timestamp": 1.0,
        })
        frame = buf.getvalue()[header_end:]
        assert frame[0] == 0

    def test_log_text_shaped_payload_roundtrips(self):
        """The exact wire shape nb.log_text emits round-trips unchanged."""
        from nebo.core.fileformat import NeboFileReader, NeboFileWriter

        payload = {
            "type": "text", "loggable_id": "__global__", "name": "status",
            "message": "msg", "step": 3, "timestamp": 1.5,
        }
        buf = io.BytesIO()
        writer = NeboFileWriter(buf, run_id="r", script_path="s.py")
        writer.write_header()
        writer.write_entry("text", dict(payload))
        writer.close()

        buf.seek(0)
        reader = NeboFileReader(buf)
        reader.read_header()
        (entry,) = list(reader.read_entries())
        assert entry["type"] == "text"
        assert entry["payload"] == payload
