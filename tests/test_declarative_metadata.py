"""Declarative nb.md()/nb.ui() + start_run adoption of virgin implicit runs.

The scoping rule: metadata called outside a run is script-level and applies
to every run the script opens (writing a template, materializing nothing);
metadata called inside a run applies to that run only. start_run upgrades a
materialized-but-virgin implicit run in place instead of opening a sibling,
and an implicit run that never carries a real event leaves no .nebo file.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import nebo as nb
from nebo.core.fileformat import NeboFileReader
from nebo.core.state import SessionState, get_state


def _reset():
    SessionState.reset_singleton()
    nb._auto_init_done = False


def _read_events(filepath: Path) -> list[dict]:
    with filepath.open("rb") as f:
        reader = NeboFileReader(f)
        reader.read_header()
        return list(reader.read_entries())


# ─── The orphan regression (subprocess, real files) ──────────────────────────


ORPHAN_SCRIPT = textwrap.dedent("""
    import os
    import sys

    sys.path.insert(0, {repo_root!r})

    os.environ["NEBO_QUIET"] = "1"
    os.environ.pop("NEBO_NO_STORE", None)
    os.chdir({tmp_path!r})

    import nebo as nb

    # The documented module-level idiom...
    nb.init(uri="runs")
    nb.md("Module-level workflow description.")
    nb.ui(view="flat", theme="dark")

    # ...followed by an explicit run. Pre-fix this produced TWO runs:
    # md/ui materialized an orphan implicit run, then start_run rolled it.
    with nb.start_run(name="the-run"):
        nb.log_text("text", "hi")
""")


def test_module_level_md_ui_then_start_run_single_file(tmp_path):
    repo_root = str(Path(__file__).parent.parent)
    script = ORPHAN_SCRIPT.format(repo_root=repo_root, tmp_path=str(tmp_path))
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr

    files = list((tmp_path / "runs").glob("*.nebo"))
    assert len(files) == 1, [f.name for f in files]

    events = _read_events(files[0])
    types = [e["type"] for e in events]
    run_starts = [e for e in events if e["type"] == "run_start"]
    assert len(run_starts) == 1
    assert run_starts[0]["payload"]["data"]["run_name"] == "the-run"

    # The template rides the stream after run_start.
    desc = [e for e in events if e["type"] == "description"]
    assert len(desc) == 1
    assert "Module-level workflow" in desc[0]["payload"]["data"]["description"]
    ui = [e for e in events if e["type"] == "ui_config"]
    assert len(ui) == 1
    assert ui[0]["payload"]["data"] == {"view": "flat", "theme": "dark"}
    assert types.index("run_start") < types.index("description")


def test_md_ui_alone_create_no_files(tmp_path, monkeypatch):
    """Declarative calls with no subsequent emit persist nothing."""
    _reset()
    monkeypatch.setenv("NEBO_QUIET", "1")
    monkeypatch.delenv("NEBO_NO_STORE", raising=False)
    monkeypatch.chdir(tmp_path)
    logdir = tmp_path / "runs"
    try:
        nb.init(uri=str(logdir))
        nb.md("described but never run")
        nb.ui(view="flat")
        assert get_state()._run_materialized is False
    finally:
        _reset()
    assert list(logdir.glob("*.nebo")) == [], list(logdir.glob("*.nebo"))


def test_first_log_materializes_and_carries_template(tmp_path, monkeypatch):
    """Pure-implicit flow: the run appears at first log and carries the md."""
    monkeypatch.setenv("NEBO_QUIET", "1")
    monkeypatch.delenv("NEBO_NO_STORE", raising=False)
    monkeypatch.chdir(tmp_path)
    _reset()
    try:
        nb.init(uri=str(tmp_path / "runs"))
        nb.md("template")
        nb.log_text("text", "materialize")
        state = get_state()
        assert state._run_materialized is True
        assert state.workflow_description == "template"
        state._transport.flush(timeout=2.0)
    finally:
        if get_state()._transport is not None:
            get_state()._transport.close()
        _reset()

    (file,) = (tmp_path / "runs").glob("*.nebo")
    types = [e["type"] for e in _read_events(file)]
    assert types.index("run_start") < types.index("description") < types.index("text")


# ─── Template semantics (NO_STORE, state-level) ──────────────────────────────


class TestTemplateScoping:
    def setup_method(self) -> None:
        _reset()

    def teardown_method(self) -> None:
        _reset()

    def test_template_applied_to_every_run_in_loop(self) -> None:
        nb.md("shared template")
        nb.ui(view="flat")
        state = get_state()
        for i in range(2):
            with nb.start_run(name=f"run-{i}"):
                assert state.workflow_description == "shared template"
                assert state.ui_config == {"view": "flat"}

    def test_md_inside_run_applies_to_that_run_only(self) -> None:
        nb.md("template")
        state = get_state()
        with nb.start_run(name="a"):
            nb.md("extra for A")
            assert state.workflow_description == "template\n\nextra for A"
        with nb.start_run(name="b"):
            assert state.workflow_description == "template"

    def test_resume_does_not_reapply_template(self) -> None:
        nb.md("T")
        state = get_state()
        ctx = nb.start_run(name="a")
        run_a = ctx.run_id
        with ctx:
            nb.md("extra")
            assert state.workflow_description == "T\n\nextra"
        with nb.start_run(run_id=run_a):
            # Restored from snapshot, template NOT re-applied (no "T\n\nT").
            assert state.workflow_description == "T\n\nextra"

    def test_reset_clears_template_clear_run_state_does_not(self) -> None:
        nb.md("T")
        nb.ui(view="flat")
        state = get_state()
        state.clear_run_state()
        assert state._script_description == "T"
        assert state._script_ui_config == {"view": "flat"}
        state.reset()
        assert state._script_description is None
        assert state._script_ui_config is None


# ─── Adoption (capturing_client, wire-level) ─────────────────────────────────


class TestVirginRunAdoption:
    def test_start_run_adopts_virgin_implicit_run(self, capturing_client) -> None:
        """A materialized implicit run with no real events is upgraded in
        place: same run_id, same transport, no run_completed, no sibling."""
        nb._ensure_run()
        state = get_state()
        implicit_id = state._active_run_id
        assert implicit_id is not None
        transport = state._transport

        ctx = nb.start_run(name="adopted", config={"lr": 0.1})

        assert ctx.run_id == implicit_id
        assert state._active_run_id == implicit_id
        assert state._transport is transport
        assert state._run_origin == "explicit"
        types = [e.get("type") for e in capturing_client.events]
        assert "run_completed" not in types
        run_starts = [e for e in capturing_client.events if e.get("type") == "run_start"]
        assert run_starts[-1]["data"]["run_name"] == "adopted"
        run_configs = [e for e in capturing_client.events if e.get("type") == "run_config"]
        assert run_configs[-1]["data"] == {"lr": 0.1}

    def test_start_run_after_real_events_opens_sibling(self, capturing_client) -> None:
        """A stray real event before start_run means the implicit run is
        genuine — keep today's close-and-roll semantics."""
        nb.log_text("text", "real event at import time")
        state = get_state()
        implicit_id = state._active_run_id

        ctx = nb.start_run(name="second")

        assert ctx.run_id != implicit_id
        types = [e.get("type") for e in capturing_client.events]
        assert "run_completed" in types

    def test_explicit_run_id_never_adopts(self, capturing_client) -> None:
        """start_run(run_id=...) states resume/interleave intent."""
        nb._ensure_run()
        state = get_state()
        implicit_id = state._active_run_id

        ctx = nb.start_run(run_id="deadbeef0123")

        assert ctx.run_id == "deadbeef0123"
        assert ctx.run_id != implicit_id
