"""Tests for the workspace seam (nebo/server/workspace.py)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from nebo.server.workspace import (
    HF_SCHEME,
    HfRef,
    LocalWorkspace,
    WorkspaceError,
    _OffsetStream,
    is_remote_uri,
    normalize_workspace,
    open_workspace,
    parse_hf_uri,
    read_frame_bytes,
)


# -- URI classification ----------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "hf://datasets/acme/runs",
        "HF://datasets/acme/runs",
        "  hf://acme/runs",
    ],
)
def test_is_remote_uri_true(value):
    assert is_remote_uri(value)


@pytest.mark.parametrize(
    "value",
    ["./.nebo", "/var/lib/nebo", "s3://bucket/key", "http://localhost:7861", "", None, 5],
)
def test_is_remote_uri_false(value):
    assert not is_remote_uri(value)


# -- hf:// parsing ---------------------------------------------------------


def test_parse_dataset_uri():
    ref = parse_hf_uri("hf://datasets/acme/runs")
    assert ref == HfRef("dataset", "acme/runs", "", None)
    assert ref.fs_path == "datasets/acme/runs"
    assert ref.repo_path == ""


def test_parse_dataset_uri_with_prefix():
    ref = parse_hf_uri("hf://datasets/acme/runs/docs/demos")
    assert ref.repo_type == "dataset"
    assert ref.repo_id == "acme/runs"
    assert ref.prefix == "docs/demos"
    assert ref.fs_path == "datasets/acme/runs/docs/demos"
    assert ref.repo_path == "docs/demos"


def test_parse_bare_uri_is_a_model_repo():
    ref = parse_hf_uri("hf://acme/runs")
    assert ref.repo_type == "model"
    assert ref.fs_path == "acme/runs"


def test_parse_space_uri():
    assert parse_hf_uri("hf://spaces/acme/dash").repo_type == "space"


def test_parse_revision():
    ref = parse_hf_uri("hf://datasets/acme/runs@main/docs")
    assert ref.repo_id == "acme/runs"
    assert ref.revision == "main"
    assert ref.prefix == "docs"
    assert ref.fs_path == "datasets/acme/runs@main/docs"


def test_parse_strips_trailing_slash():
    assert parse_hf_uri("hf://datasets/acme/runs/").uri == "hf://datasets/acme/runs"


@pytest.mark.parametrize(
    "bad",
    [
        "hf://",
        "hf://datasets",
        "hf://datasets/acme",
        "hf://acme",
        "hf://datasets/acme/@main",
        "hf://datasets/acme/runs@",
    ],
)
def test_parse_rejects_incomplete(bad):
    with pytest.raises(WorkspaceError):
        parse_hf_uri(bad)


def test_parse_rejects_local_path():
    with pytest.raises(WorkspaceError):
        parse_hf_uri("/tmp/nebo")


@pytest.mark.parametrize(
    "uri",
    [
        "hf://datasets/acme/runs",
        "hf://datasets/acme/runs/docs",
        "hf://datasets/acme/runs@main/docs",
        "hf://spaces/acme/dash",
        "hf://acme/model-repo",
    ],
)
def test_uri_round_trips(uri):
    assert parse_hf_uri(parse_hf_uri(uri).uri).uri == uri


def test_join_extends_prefix():
    ref = parse_hf_uri("hf://datasets/acme/runs")
    assert ref.join("meta/tree.json").fs_path == "datasets/acme/runs/meta/tree.json"
    assert ref.join("meta/tree.json").repo_path == "meta/tree.json"
    assert ref.join("").fs_path == "datasets/acme/runs"


def test_join_preserves_revision():
    ref = parse_hf_uri("hf://datasets/acme/runs@dev")
    assert ref.join("a.nebo").fs_path == "datasets/acme/runs@dev/a.nebo"


# -- normalization ---------------------------------------------------------


def test_normalize_local_matches_path_resolve(tmp_path):
    assert normalize_workspace(str(tmp_path)) == str(Path(tmp_path).resolve())


def test_normalize_local_is_absolute_for_relative_input():
    assert normalize_workspace(".nebo") == str(Path(".nebo").resolve())


def test_normalize_none_is_empty_string():
    # Matches the cache's historical "no logdir" identity key.
    assert normalize_workspace(None) == ""


def test_normalize_remote_is_never_path_resolved():
    """`Path("hf://x").resolve()` yields `$CWD/hf:/x` — the bug this guards."""
    out = normalize_workspace("hf://datasets/acme/runs")
    assert out == "hf://datasets/acme/runs"
    assert out.startswith(HF_SCHEME)
    assert "hf:/datasets" not in out
    assert str(Path.cwd()) not in out


def test_normalize_remote_is_idempotent():
    once = normalize_workspace("hf://datasets/acme/runs/")
    assert normalize_workspace(once) == once


def test_normalize_remote_is_machine_independent(monkeypatch, tmp_path):
    """Cache identity is a sha1 of this string, so cwd must not leak in."""
    uri = "hf://datasets/acme/runs"
    first = normalize_workspace(uri)
    monkeypatch.chdir(tmp_path)
    assert normalize_workspace(uri) == first


# -- _OffsetStream ---------------------------------------------------------


def test_offset_stream_reports_absolute_positions():
    s = _OffsetStream(io.BytesIO(b"abcdef"), base=100)
    assert s.tell() == 100
    assert s.read(2) == b"ab"
    assert s.tell() == 102


def test_offset_stream_absolute_seek():
    s = _OffsetStream(io.BytesIO(b"abcdef"), base=100)
    s.read(4)
    assert s.seek(100) == 100
    assert s.read(1) == b"a"


def test_offset_stream_seek_before_base_clamps():
    s = _OffsetStream(io.BytesIO(b"abcdef"), base=100)
    assert s.seek(0) == 100


def test_offset_stream_relative_seek():
    s = _OffsetStream(io.BytesIO(b"abcdef"), base=10)
    s.read(3)
    s.seek(-1, io.SEEK_CUR)
    assert s.tell() == 12
    assert s.read(1) == b"c"


def test_offset_stream_read_past_eof_is_short():
    s = _OffsetStream(io.BytesIO(b"ab"), base=5)
    assert s.read(10) == b"ab"
    assert s.tell() == 7


def test_offset_stream_is_a_context_manager():
    with _OffsetStream(io.BytesIO(b"xy"), base=0) as s:
        assert s.read() == b"xy"


def test_reader_offsets_match_a_real_file(tmp_path):
    """The remote adapter and a real handle must be indistinguishable."""
    p = tmp_path / "a.bin"
    p.write_bytes(bytes(range(64)))

    remote = _OffsetStream(io.BytesIO(p.read_bytes()[20:]), base=20)
    with open(p, "rb") as local:
        local.seek(20)
        assert local.tell() == remote.tell()
        assert local.read(5) == remote.read(5)
        assert local.tell() == remote.tell()
        local.seek(30)
        remote.seek(30)
        assert local.read(4) == remote.read(4)


# -- LocalWorkspace --------------------------------------------------------


def test_open_workspace_picks_local(tmp_path):
    ws = open_workspace(str(tmp_path))
    assert isinstance(ws, LocalWorkspace)
    assert ws.is_remote is False
    assert ws.uri == str(tmp_path.resolve())


def test_open_workspace_picks_remote_without_importing_hub():
    ws = open_workspace("hf://datasets/acme/runs")
    assert ws.is_remote is True
    assert ws.uri == "hf://datasets/acme/runs"


def test_open_workspace_rejects_none():
    with pytest.raises(WorkspaceError):
        open_workspace(None)


def test_remote_polls_much_slower_than_local(tmp_path):
    assert open_workspace(str(tmp_path)).default_poll_interval == 0.5
    assert open_workspace("hf://datasets/a/b").default_poll_interval == 30.0


def test_poll_interval_override(tmp_path):
    assert open_workspace(str(tmp_path), poll_interval=5.0).default_poll_interval == 5.0


def test_ensure_root_creates_the_directory(tmp_path):
    root = tmp_path / "deep" / "nested"
    open_workspace(str(root)).ensure_root()
    assert root.is_dir()


def test_list_nebo_finds_only_nebo_files(tmp_path):
    (tmp_path / "a.nebo").write_bytes(b"xx")
    (tmp_path / "b.nebo").write_bytes(b"yyy")
    (tmp_path / "notes.txt").write_text("no")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.nebo").write_bytes(b"z")  # non-recursive

    ws = LocalWorkspace(tmp_path)
    found = {Path(s.uri).name: s.size for s in ws.list_nebo()}
    assert found == {"a.nebo": 2, "b.nebo": 3}


def test_list_nebo_uris_are_absolute_paths(tmp_path):
    """watch_files.path keys off these strings — they must stay absolute."""
    (tmp_path / "a.nebo").write_bytes(b"x")
    (stat,) = LocalWorkspace(tmp_path).list_nebo()
    assert stat.uri == str(tmp_path.resolve() / "a.nebo")
    assert Path(stat.uri).is_absolute()


def test_list_nebo_on_missing_root_is_empty(tmp_path):
    assert LocalWorkspace(tmp_path / "gone").list_nebo() == []


def test_local_has_no_change_token(tmp_path):
    assert LocalWorkspace(tmp_path).change_token() is None


def test_stat_reports_size_and_mtime(tmp_path):
    p = tmp_path / "a.nebo"
    p.write_bytes(b"hello")
    st = LocalWorkspace(tmp_path).stat(str(p))
    assert st is not None and st.size == 5 and st.mtime is not None


def test_stat_missing_is_none(tmp_path):
    assert LocalWorkspace(tmp_path).stat(str(tmp_path / "nope.nebo")) is None


def test_reader_seeks_to_offset(tmp_path):
    p = tmp_path / "a.nebo"
    p.write_bytes(b"0123456789")
    with LocalWorkspace(tmp_path).reader(str(p), 4) as f:
        assert f.tell() == 4
        assert f.read() == b"456789"


def test_read_range(tmp_path):
    p = tmp_path / "a.nebo"
    p.write_bytes(b"0123456789")
    assert LocalWorkspace(tmp_path).read_range(str(p), 2, 3) == b"234"


def test_read_range_missing_is_none(tmp_path):
    assert LocalWorkspace(tmp_path).read_range(str(tmp_path / "no.nebo"), 0, 1) is None


# -- LocalWorkspace meta/ side --------------------------------------------


def test_commit_writes_and_reads_back(tmp_path):
    ws = LocalWorkspace(tmp_path)
    ws.commit([("meta/tree.json", b'{"v":1}')], [])
    assert ws.read_bytes("meta/tree.json") == b'{"v":1}'


def test_commit_creates_parent_directories(tmp_path):
    ws = LocalWorkspace(tmp_path)
    ws.commit([("meta/docs/a/b/README.md", b"hi")], [])
    assert (tmp_path / "meta" / "docs" / "a" / "b" / "README.md").read_text() == "hi"


def test_commit_leaves_no_tmp_file_behind(tmp_path):
    ws = LocalWorkspace(tmp_path)
    ws.commit([("meta/tree.json", b"{}")], [])
    assert list((tmp_path / "meta").glob("*.tmp")) == []


def test_commit_deletes_files_and_directories(tmp_path):
    ws = LocalWorkspace(tmp_path)
    ws.commit(
        [("meta/docs/g/a.md", b"a"), ("meta/docs/g/b.md", b"b"), ("meta/x.json", b"{}")],
        [],
    )
    ws.commit([], ["meta/docs/g", "meta/x.json"])
    assert not (tmp_path / "meta" / "docs" / "g").exists()
    assert not (tmp_path / "meta" / "x.json").exists()


def test_commit_delete_of_missing_path_is_a_noop(tmp_path):
    LocalWorkspace(tmp_path).commit([], ["meta/nope.json"])


def test_commit_applies_deletes_before_adds(tmp_path):
    """A move is expressed as delete-old + add-new in one call."""
    ws = LocalWorkspace(tmp_path)
    ws.commit([("meta/docs/old/a.md", b"a")], [])
    ws.commit([("meta/docs/new/a.md", b"a")], ["meta/docs/old"])
    assert ws.read_bytes("meta/docs/new/a.md") == b"a"
    assert ws.read_bytes("meta/docs/old/a.md") is None


def test_read_bytes_missing_is_none(tmp_path):
    assert LocalWorkspace(tmp_path).read_bytes("meta/tree.json") is None


def test_list_dir_returns_file_names_only(tmp_path):
    ws = LocalWorkspace(tmp_path)
    ws.commit([("meta/docs/b.md", b""), ("meta/docs/a.md", b"")], [])
    (tmp_path / "meta" / "docs" / "sub").mkdir()
    assert ws.list_dir("meta/docs") == ["a.md", "b.md"]


def test_list_dir_missing_is_empty(tmp_path):
    assert LocalWorkspace(tmp_path).list_dir("meta/docs") == []


def test_list_tree_is_recursive_and_relative(tmp_path):
    ws = LocalWorkspace(tmp_path)
    ws.commit([("meta/docs/g/a.md", b""), ("meta/docs/g/h/b.md", b"")], [])
    assert ws.list_tree("meta/docs") == ["g/a.md", str(Path("g/h/b.md"))]


def test_list_tree_missing_is_empty(tmp_path):
    assert LocalWorkspace(tmp_path).list_tree("meta/docs") == []


@pytest.mark.asyncio
async def test_local_run_io_executes_inline(tmp_path):
    ws = LocalWorkspace(tmp_path)
    assert await ws.run_io(lambda a, b: a + b, 2, 3) == 5


# -- standalone frame resolution ------------------------------------------


def test_read_frame_bytes_local(tmp_path):
    p = tmp_path / "a.nebo"
    p.write_bytes(b"0123456789")
    assert read_frame_bytes(str(p), 3, 4) == b"3456"


def test_read_frame_bytes_missing_is_none(tmp_path):
    assert read_frame_bytes(str(tmp_path / "no.nebo"), 0, 4) is None


def test_read_frame_bytes_routes_remote_uris(monkeypatch):
    """An hf:// src_path must never hit builtins.open()."""
    from nebo.server import workspace as ws_mod

    seen = {}

    class FakeRemote:
        def read_range(self, uri, offset, length):
            seen["args"] = (uri, offset, length)
            return b"remote-bytes"

    monkeypatch.setattr(ws_mod, "_remote_for", lambda uri: FakeRemote())
    uri = "hf://datasets/acme/runs/a.nebo"
    assert read_frame_bytes(uri, 7, 3) == b"remote-bytes"
    assert seen["args"] == (uri, 7, 3)


def test_remote_backends_are_memoized_per_repo():
    from nebo.server import workspace as ws_mod

    ws_mod.reset_remote_cache()
    try:
        a = ws_mod._remote_for("hf://datasets/acme/runs/a.nebo")
        b = ws_mod._remote_for("hf://datasets/acme/runs/deep/b.nebo")
        c = ws_mod._remote_for("hf://datasets/other/runs/c.nebo")
        assert a is b
        assert a is not c
        # Memoized at the repo root, so any file in the repo shares it.
        assert a.uri == "hf://datasets/acme/runs"
    finally:
        ws_mod.reset_remote_cache()


# -- optional-dependency isolation ----------------------------------------


def test_local_workspace_never_imports_huggingface_hub(tmp_path, block_import):
    """A plain local daemon must work with huggingface_hub uninstalled."""
    with block_import("huggingface_hub"):
        ws = open_workspace(str(tmp_path))
        ws.ensure_root()
        (tmp_path / "a.nebo").write_bytes(b"data")
        assert len(ws.list_nebo()) == 1
        ws.commit([("meta/tree.json", b"{}")], [])
        assert ws.read_bytes("meta/tree.json") == b"{}"


def test_remote_workspace_reports_missing_dependency_clearly(block_import):
    with block_import("huggingface_hub"):
        ws = open_workspace("hf://datasets/acme/runs")
        with pytest.raises(WorkspaceError, match="nebo\\[deploy\\]"):
            _ = ws.fs
