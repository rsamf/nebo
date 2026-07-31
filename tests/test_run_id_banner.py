"""Tests for the run-id banner the SDK prints on start-up."""
from __future__ import annotations

import io
import re
from contextlib import redirect_stdout

import pytest

import nebo as nb
from nebo.core.state import SessionState


FILE_BANNER_RE = re.compile(
    r"nebo: writing run \(run_id=([0-9a-fA-F]{12})\) to .+\.nebo"
)
NETWORK_BANNER_RE = re.compile(
    r"nebo: connected run \(run_id=([0-9a-fA-F]{12})\) to .+"
)
RUN_ID_RE = re.compile(r"run_id=([0-9a-fA-F]{12})")


def _reset():
    SessionState.reset_singleton()
    nb._auto_init_done = False


def test_init_prints_file_banner_with_default_uri(tmp_path, monkeypatch):
    # The banner fires when the run materializes (first emit), not at
    # nb.init() time, so emit something to trigger it.
    _reset()
    monkeypatch.delenv("NEBO_QUIET", raising=False)
    monkeypatch.delenv("NEBO_NO_STORE", raising=False)
    monkeypatch.chdir(tmp_path)
    buf = io.StringIO()
    with redirect_stdout(buf):
        nb.init()
        nb.log_text("text", "trigger materialization")
    out = buf.getvalue()
    assert FILE_BANNER_RE.search(out), repr(out)
    assert RUN_ID_RE.search(out), repr(out)
    _reset()


def test_init_suppresses_banner_when_quiet(tmp_path, monkeypatch):
    # Even when something emits, NEBO_QUIET=1 keeps the banner silent.
    _reset()
    monkeypatch.setenv("NEBO_QUIET", "1")
    monkeypatch.setenv("NEBO_NO_STORE", "1")
    monkeypatch.chdir(tmp_path)
    buf = io.StringIO()
    with redirect_stdout(buf):
        nb.init()
        nb.log_text("text", "trigger materialization")
    assert "nebo:" not in buf.getvalue()
    _reset()


def test_no_store_disables_file_write(tmp_path, monkeypatch):
    # NEBO_NO_STORE: emitting still prints the banner (with the no-store
    # suffix) but never opens a file.
    _reset()
    monkeypatch.setenv("NEBO_NO_STORE", "1")
    monkeypatch.delenv("NEBO_QUIET", raising=False)
    monkeypatch.chdir(tmp_path)
    buf = io.StringIO()
    with redirect_stdout(buf):
        nb.init()
        nb.log_text("text", "trigger materialization")
    assert "NEBO_NO_STORE=1" in buf.getvalue()
    assert not list(tmp_path.glob("**/*.nebo"))
    _reset()


def test_init_alone_creates_no_files(tmp_path, monkeypatch):
    """Regression: nb.init() with no subsequent emit must produce no
    .nebo file. Today's bug created an orphan file every time the user
    init'd and then called start_run.
    """
    _reset()
    monkeypatch.setenv("NEBO_QUIET", "1")
    monkeypatch.delenv("NEBO_NO_STORE", raising=False)
    monkeypatch.chdir(tmp_path)
    logdir = tmp_path / "runs"
    nb.init(uri=str(logdir))
    # Tear down without emitting anything.
    _reset()
    # logdir is created by resolve_uri eagerly (mkdir is fine), but no
    # *.nebo file should exist inside it.
    assert list(logdir.glob("*.nebo")) == [], list(logdir.glob("*.nebo"))
