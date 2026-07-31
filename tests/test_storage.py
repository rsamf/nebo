"""Tests for daemon-side --remote persistence."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nebo.core.fileformat import NeboFileReader
from nebo.server.daemon import DaemonState


@pytest.mark.asyncio
async def test_remote_mode_writes_nebo_file(tmp_path):
    state = DaemonState()
    state.mode = "remote"
    state._remote_dir = tmp_path
    run = state.create_run("test.py", [], "run-1")
    assert run._file_writer is not None
    await state.ingest_events(
        [{"type": "text", "loggable_id": "__global__", "message": "hi"}],
        run_id="run-1",
    )
    state.finalize_run("run-1")
    files = list(tmp_path.glob("*.nebo"))
    assert len(files) == 1
    with files[0].open("rb") as f:
        reader = NeboFileReader(f)
        meta = reader.read_header()
        assert meta["run_id"] == "run-1"
        msgs = [
            e["payload"].get("message")
            for e in reader.read_entries()
            if e["type"] == "text"
        ]
    assert msgs == ["hi"]


@pytest.mark.asyncio
async def test_ephemeral_writes_nothing(tmp_path):
    state = DaemonState()  # default mode is remote-ephemeral; _remote_dir None
    run = state.create_run("test.py", [], "run-2")
    assert run._file_writer is None
    assert not list(tmp_path.glob("*.nebo"))


@pytest.mark.asyncio
async def test_legacy_log_entry_file_reads_and_ingests(tmp_path):
    """Read compat for pre-rename files: a `write_entry("log", ...)` frame
    (entry code 0) still decodes, and — via the watcher's payload-type
    recovery + the daemon's log->text normalization — still lands in
    `run.texts`."""
    from nebo.core.fileformat import NeboFileWriter

    path = tmp_path / "legacy.nebo"
    with path.open("wb") as f:
        writer = NeboFileWriter(f, run_id="legacyrun001", script_path="old.py")
        writer.write_header()
        writer.write_entry("log", {
            "type": "log", "loggable_id": "__global__", "name": "text",
            "message": "old", "timestamp": 123.0,
        })
        writer.close()

    with path.open("rb") as f:
        reader = NeboFileReader(f)
        reader.read_header()
        entries = list(reader.read_entries())
    assert len(entries) == 1
    assert entries[0]["type"] == "log"  # legacy spelling preserved on read
    assert entries[0]["payload"]["message"] == "old"

    state = DaemonState()
    # Mirror the watcher's payload-type recovery shape.
    events = [{"type": e["type"], **e["payload"]} for e in entries]
    await state.ingest_events(events, run_id="legacyrun001", source="watcher")
    run = state.runs["legacyrun001"]
    assert len(run.texts) == 1
    assert run.texts[0].message == "old"
    assert run.texts[0].name == "text"
