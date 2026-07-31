"""Tests for run-lifecycle event payloads and atexit emission."""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from nebo.core.fileformat import NeboFileReader


def _read_events(filepath: Path) -> list[dict]:
    with filepath.open("rb") as f:
        reader = NeboFileReader(f)
        reader.read_header()
        return list(reader.read_entries())


def test_run_start_payload_has_timestamp(tmp_path, monkeypatch):
    import nebo as nb
    from nebo.core.state import SessionState

    monkeypatch.setenv("NEBO_QUIET", "1")
    monkeypatch.delenv("NEBO_NO_STORE", raising=False)
    monkeypatch.chdir(tmp_path)

    SessionState.reset_singleton()
    nb._auto_init_done = False
    try:
        nb.init(uri=str(tmp_path / "runs"))
        # The run materializes on first emit; trigger it before flushing.
        nb.log_text("text", "trigger materialization")
        nb.get_state()._transport.flush(timeout=2.0)
    finally:
        if nb.get_state()._transport is not None:
            nb.get_state()._transport.close()
        SessionState.reset_singleton()
        nb._auto_init_done = False

    file = next((tmp_path / "runs").glob("*.nebo"))
    events = _read_events(file)
    run_start = next(e for e in events if e["type"] == "run_start")
    assert "timestamp" in run_start["payload"]["data"], run_start
    assert isinstance(run_start["payload"]["data"]["timestamp"], (int, float))


def test_explicit_start_run_completed_carries_timestamp(tmp_path, monkeypatch):
    import nebo as nb
    from nebo.core.state import SessionState

    monkeypatch.setenv("NEBO_QUIET", "1")
    monkeypatch.delenv("NEBO_NO_STORE", raising=False)
    monkeypatch.chdir(tmp_path)

    SessionState.reset_singleton()
    nb._auto_init_done = False
    try:
        nb.init(uri=str(tmp_path / "runs"))
        with nb.start_run():
            nb.log_text("text", "hi")
        nb.get_state()._transport.flush(timeout=2.0)
    finally:
        if nb.get_state()._transport is not None:
            nb.get_state()._transport.close()
        SessionState.reset_singleton()
        nb._auto_init_done = False

    files = list((tmp_path / "runs").glob("*.nebo"))
    # Find the file that contains a run_completed event.
    found = False
    for f in files:
        events = _read_events(f)
        completed = [e for e in events if e["type"] == "run_completed"]
        if completed:
            data = completed[0]["payload"]["data"]
            assert "timestamp" in data, data
            found = True
            break
    assert found, f"no run_completed event found across files: {[f.name for f in files]}"


ATEXIT_SCRIPT = textwrap.dedent("""
    import os
    import sys

    sys.path.insert(0, {repo_root!r})

    os.environ["NEBO_QUIET"] = "1"
    os.environ.pop("NEBO_NO_STORE", None)
    os.chdir({tmp_path!r})

    import nebo as nb
    nb.init(uri="runs")
    nb.log_text("text", "hi from subprocess")
    # No nb.start_run() — this is the implicit-run case.
    # Process exits normally; atexit must emit run_completed.
""")


def test_filetransport_emits_run_completed_on_normal_exit(tmp_path):
    repo_root = str(Path(__file__).parent.parent)
    script = ATEXIT_SCRIPT.format(repo_root=repo_root, tmp_path=str(tmp_path))
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

    files = list((tmp_path / "runs").glob("*.nebo"))
    assert len(files) == 1
    events = _read_events(files[0])
    completed = [e for e in events if e["type"] == "run_completed"]
    assert len(completed) == 1, [e["type"] for e in events]
    data = completed[0]["payload"]["data"]
    assert isinstance(data["timestamp"], (int, float))


CRASH_SCRIPT = textwrap.dedent("""
    import os
    import sys

    sys.path.insert(0, {repo_root!r})
    os.environ["NEBO_QUIET"] = "1"
    os.environ.pop("NEBO_NO_STORE", None)
    os.chdir({tmp_path!r})

    import nebo as nb
    nb.init(uri="runs")
    nb.log_text("text", "about to crash")
    raise RuntimeError("intentional crash")
""")


def test_filetransport_emits_run_completed_on_crash(tmp_path):
    repo_root = str(Path(__file__).parent.parent)
    script = CRASH_SCRIPT.format(repo_root=repo_root, tmp_path=str(tmp_path))
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    # Process should exit non-zero because of the unhandled exception.
    assert result.returncode != 0, result.stdout

    files = list((tmp_path / "runs").glob("*.nebo"))
    assert len(files) == 1
    events = _read_events(files[0])
    # The file still closes cleanly with a run_completed marker; no
    # crash/exit-code semantics are recorded (run states were removed).
    completed = [e for e in events if e["type"] == "run_completed"]
    assert len(completed) == 1
