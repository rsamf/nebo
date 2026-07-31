"""Run tree: TreeStore logic, seed-once, endpoints, and SDK group precedence."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nebo.core.groups import validate_group_path, validate_doc_name
from nebo.server.daemon import DaemonState, create_daemon_app
from nebo.server.tree import TreeConflict, TreeStore


def _tree(tmp_path) -> TreeStore:
    return TreeStore(tmp_path / "meta")


# ── path validation ──────────────────────────────────────────────────

class TestValidation:
    def test_normalizes(self):
        assert validate_group_path("  a/b/c  ") == "a/b/c"
        assert validate_group_path("/a/b/") == "a/b"
        assert validate_group_path("") == ""
        assert validate_group_path(None) == ""

    @pytest.mark.parametrize("bad", [
        "a/../b", "..", ".", "a/./b", "a//b", "a/ b", "a\\b",
        "a:b", "a*b", "a?b", 'a"b', "a<b", "a>b", "a|b", "a\x00b",
        "/".join(["x"] * 17),          # too deep
        "a" * 129,                      # component too long
    ])
    def test_rejects(self, bad):
        with pytest.raises(ValueError):
            validate_group_path(bad)

    def test_doc_name(self):
        assert validate_doc_name("README.md") == "README.md"
        for bad in ["readme", "a/b.md", "..", ".md", "a.txt", "x\x00.md"]:
            with pytest.raises(ValueError):
                validate_doc_name(bad)


# ── TreeStore logic ──────────────────────────────────────────────────

class TestTreeStore:
    def test_create_group_creates_ancestors(self, tmp_path):
        t = _tree(tmp_path)
        assert t.create_group("a/b/c") is True
        payload = t.to_payload(set())
        assert set(payload["groups"]) == {"a", "a/b", "a/b/c"}
        assert t.create_group("a/b/c") is False  # idempotent

    def test_create_root_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            _tree(tmp_path).create_group("")

    def test_seed_once(self, tmp_path):
        t = _tree(tmp_path)
        assert t.seed_run("r1", "x") is True
        assert t.seed_run("r1", "y") is False  # already placed → no override
        assert t.to_payload({"r1"})["runs"]["r1"] == "x"
        # Persisted placement wins after a "restart" (reload).
        t2 = _tree(tmp_path)
        assert t2.seed_run("r1", "x") is False
        assert t2.to_payload({"r1"})["runs"]["r1"] == "x"

    def test_move_survives_rescan(self, tmp_path):
        """The load-bearing invariant: a moved run stays moved when its file is
        re-scanned — the header's birth group never re-wins."""
        t = _tree(tmp_path)
        t.seed_run("r1", "x")
        t.set_run_group("r1", "y")               # explicit move
        t2 = _tree(tmp_path)                       # restart
        assert t2.seed_run("r1", "x") is False     # rescan does not override
        assert t2.to_payload({"r1"})["runs"]["r1"] == "y"

    def test_move_group_cascades(self, tmp_path):
        t = _tree(tmp_path)
        t.create_group("old/sub")
        t.seed_run("r1", "old")
        t.seed_run("r2", "old/sub")
        t.set_doc("old", "README.md", "hi")
        t.move_group("old", "new/here")
        payload = t.to_payload({"r1", "r2"})
        assert "new/here" in payload["groups"]
        assert "new/here/sub" in payload["groups"]
        assert "old" not in payload["groups"]
        assert payload["runs"]["r1"] == "new/here"
        assert payload["runs"]["r2"] == "new/here/sub"
        assert t.get_doc("new/here", "README.md") == "hi"
        assert t.get_doc("old", "README.md") is None

    def test_move_group_dangling_entries_follow(self, tmp_path):
        t = _tree(tmp_path)
        t.seed_run("ghost", "old")   # run_id not in any known set
        t.move_group("old", "new")
        # Dangling placement follows the rename (filtered from views, but
        # coherent so a later reappearance lands right).
        assert t._runs["ghost"] == "new"

    def test_delete_group_guards(self, tmp_path):
        t = _tree(tmp_path)
        t.create_group("g/sub")
        with pytest.raises(TreeConflict):
            t.delete_group("g", set())          # has a subgroup
        t.seed_run("r1", "g/sub")
        with pytest.raises(TreeConflict):
            t.delete_group("g/sub", {"r1"})     # has a known member run
        # Unknown member run doesn't block deletion; its placement is dropped.
        t.seed_run("ghost", "g/sub")
        t2 = _tree(tmp_path)  # fresh view; ghost known to none
        # remove the known run first
        t.set_run_group("r1", "")
        t.delete_group("g/sub", {"r1"})
        assert "g/sub" not in t.to_payload({"r1"})["groups"]
        assert "ghost" not in t._runs  # dangling entry cleaned up

    def test_placement_to_deleted_group_resolves_root(self, tmp_path):
        t = _tree(tmp_path)
        t.seed_run("r1", "g")
        del t._groups["g"]  # simulate the group vanishing under the placement
        # to_payload omits the placement (defaults to root).
        assert "r1" not in t.to_payload({"r1"})["runs"]

    def test_to_payload_filters_unknown_runs(self, tmp_path):
        t = _tree(tmp_path)
        t.seed_run("known", "g")
        t.seed_run("ghost", "g")
        payload = t.to_payload({"known"})
        assert payload["runs"] == {"known": "g"}

    def test_set_run_group_to_root(self, tmp_path):
        t = _tree(tmp_path)
        t.seed_run("r1", "g")
        assert t.set_run_group("r1", "") == ""
        assert "r1" not in t.to_payload({"r1"})["runs"]  # root omitted
        # But the explicit "" entry blocks a re-seed.
        assert t.seed_run("r1", "g") is False

    def test_docs_roundtrip(self, tmp_path):
        t = _tree(tmp_path)
        assert t.set_doc("g", "README.md", "# hello") is True
        assert t.set_doc("g", "README.md", "# again") is False  # overwrite
        assert t.set_doc("g", "notes.md", "n") is True
        assert t.get_doc("g", "README.md") == "# again"
        docs = t.to_payload(set())["groups"]["g"]["docs"]
        assert docs == ["README.md", "notes.md"]  # README first
        assert t.delete_doc("g", "notes.md") is True
        assert t.delete_doc("g", "notes.md") is False
        assert t.get_doc("g", "notes.md") is None

    def test_restart_persistence(self, tmp_path):
        t = _tree(tmp_path)
        t.create_group("a/b")
        t.seed_run("r1", "a/b")
        t.set_doc("a/b", "README.md", "x")
        t2 = _tree(tmp_path)
        payload = t2.to_payload({"r1"})
        assert "a/b" in payload["groups"]
        assert payload["runs"]["r1"] == "a/b"
        assert t2.get_doc("a/b", "README.md") == "x"

    def test_unparseable_refuses_startup(self, tmp_path):
        meta = tmp_path / "meta"
        meta.mkdir()
        (meta / "tree.json").write_text("{ not json")
        with pytest.raises(RuntimeError):
            TreeStore(meta)


# ── daemon seed + endpoints ──────────────────────────────────────────

class TestEndpoints:
    def _client(self, tmp_path):
        state = DaemonState()
        state.tree = TreeStore(tmp_path / "meta")
        return state, TestClient(create_daemon_app(state))

    def test_run_start_seeds_group(self, tmp_path):
        state, client = self._client(tmp_path)
        client.post("/events?run_id=r1", json=[
            {"type": "run_start", "data": {"script_path": "s.py", "group": "vision/detr"}},
        ])
        tree = client.get("/tree").json()
        assert "vision/detr" in tree["groups"]
        assert tree["runs"]["r1"] == "vision/detr"

    def test_group_crud_endpoints(self, tmp_path):
        state, client = self._client(tmp_path)
        assert client.post("/groups", json={"path": "a/b"}).status_code == 201
        assert client.post("/groups", json={"path": "a/b"}).status_code == 200
        assert client.post("/groups", json={"path": "a/../x"}).status_code == 422
        # move
        assert client.patch("/groups/a/b", json={"new_path": "a/c"}).status_code == 200
        tree = client.get("/tree").json()
        assert "a/c" in tree["groups"] and "a/b" not in tree["groups"]
        # delete empty ok
        assert client.request("DELETE", "/groups/a/c").status_code == 200

    def test_delete_nonempty_group_409(self, tmp_path):
        state, client = self._client(tmp_path)
        client.post("/events?run_id=r1", json=[
            {"type": "run_start", "data": {"group": "g"}},
        ])
        assert client.request("DELETE", "/groups/g").status_code == 409

    def test_move_run_endpoint(self, tmp_path):
        state, client = self._client(tmp_path)
        client.post("/events?run_id=r1", json=[{"type": "run_start", "data": {}}])
        resp = client.put("/runs/r1/group", json={"group": "team/exp"})
        assert resp.status_code == 200
        assert resp.json()["group"] == "team/exp"
        assert client.get("/tree").json()["runs"]["r1"] == "team/exp"

    def test_docs_endpoints(self, tmp_path):
        state, client = self._client(tmp_path)
        client.post("/groups", json={"path": "g"})
        assert client.put(
            "/groups/g/docs/README.md", content=b"# findings",
            headers={"Content-Type": "text/markdown"},
        ).status_code == 201
        resp = client.get("/groups/g/docs/README.md")
        assert resp.status_code == 200 and resp.text == "# findings"
        assert "README.md" in client.get("/tree").json()["groups"]["g"]["docs"]
        assert client.request("DELETE", "/groups/g/docs/README.md").status_code == 204
        assert client.get("/groups/g/docs/README.md").status_code == 404

    def test_tree_filters_unknown_runs(self, tmp_path):
        # A placement whose run the daemon doesn't know is filtered from /tree.
        state, client = self._client(tmp_path)
        state.tree.seed_run("ghost", "g")
        assert client.get("/tree").json()["runs"] == {}


# ── SDK group precedence (file-mode header) ──────────────────────────

class TestSdkPrecedence:
    def _read_group(self, logdir: Path) -> str:
        from nebo.core.fileformat import NeboFileReader
        (fp,) = logdir.glob("*.nebo")
        with fp.open("rb") as f:
            return NeboFileReader(f).read_header().get("group", "<none>")

    def _run(self, monkeypatch, logdir, *, init_group, start_group, env_group):
        import nebo as nb
        from nebo.core.state import SessionState

        monkeypatch.delenv("NEBO_NO_STORE", raising=False)  # allow the writer
        monkeypatch.delenv("NEBO_URI", raising=False)
        if env_group is None:
            monkeypatch.delenv("NEBO_GROUP", raising=False)
        else:
            monkeypatch.setenv("NEBO_GROUP", env_group)
        SessionState.reset_singleton()
        nb._auto_init_done = False
        try:
            nb.init(uri=str(logdir), group=init_group)
            with nb.start_run(name="r", group=start_group):
                nb.log_text("text", "hi")
        finally:
            SessionState.reset_singleton()
            nb._auto_init_done = False

    def test_start_run_overrides_init(self, tmp_path, monkeypatch):
        self._run(monkeypatch, tmp_path, init_group="from/init",
                  start_group="from/start", env_group=None)
        assert self._read_group(tmp_path) == "from/start"

    def test_env_overrides_all(self, tmp_path, monkeypatch):
        self._run(monkeypatch, tmp_path, init_group="from/init",
                  start_group="from/start", env_group="from/env")
        assert self._read_group(tmp_path) == "from/env"

    def test_init_group_used_when_no_override(self, tmp_path, monkeypatch):
        self._run(monkeypatch, tmp_path, init_group="from/init",
                  start_group=None, env_group=None)
        assert self._read_group(tmp_path) == "from/init"
