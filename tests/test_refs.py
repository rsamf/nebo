"""nebo:// reference parsing/formatting + the daemon /resolve endpoint."""

from __future__ import annotations

import pytest

from nebo.core.refs import GroupRef, RunRef, format_ref, parse_ref


class TestParseRef:
    def test_run(self) -> None:
        assert parse_ref("nebo://run/1bec76d1111c") == RunRef(run_id="1bec76d1111c")

    def test_loggable(self) -> None:
        assert parse_ref("nebo://run/abc123/train") == RunRef(
            run_id="abc123", loggable_id="train",
        )

    def test_qualname_loggable(self) -> None:
        assert parse_ref("nebo://run/abc123/Trainer.train") == RunRef(
            run_id="abc123", loggable_id="Trainer.train",
        )

    def test_stream_name_with_slashes(self) -> None:
        ref = parse_ref("nebo://run/abc123/__global__/train/time/rollout_s")
        assert ref == RunRef(
            run_id="abc123", loggable_id="__global__", name="train/time/rollout_s",
        )

    def test_step_suffix(self) -> None:
        ref = parse_ref("nebo://run/abc123/train/loss@120")
        assert ref == RunRef(
            run_id="abc123", loggable_id="train", name="loss", step=120,
        )

    def test_step_on_bare_run(self) -> None:
        assert parse_ref("nebo://run/abc123@7") == RunRef(run_id="abc123", step=7)

    def test_legacy_query_step(self) -> None:
        assert parse_ref("nebo://run/abc123?step=42") == RunRef(run_id="abc123", step=42)

    def test_at_inside_name_without_numeric_tail(self) -> None:
        ref = parse_ref("nebo://run/abc123/train/user@host")
        assert ref == RunRef(run_id="abc123", loggable_id="train", name="user@host")

    def test_group(self) -> None:
        assert parse_ref("nebo://group/experiments/ablations") == GroupRef(
            path="experiments/ablations",
        )

    @pytest.mark.parametrize("bad", [
        "https://example.com",
        "nebo://run/",
        "nebo://group/",
        "nebo://what/abc",
        "run/abc123",
    ])
    def test_rejects(self, bad: str) -> None:
        assert parse_ref(bad) is None


class TestFormatRef:
    @pytest.mark.parametrize("uri", [
        "nebo://run/abc123",
        "nebo://run/abc123/train",
        "nebo://run/abc123/train/loss@120",
        "nebo://run/abc123/__global__/train/time/rollout_s",
        "nebo://group/experiments/ablations",
    ])
    def test_round_trip(self, uri: str) -> None:
        ref = parse_ref(uri)
        assert ref is not None
        assert format_ref(ref) == uri
        assert parse_ref(format_ref(ref)) == ref


class TestResolveEndpoint:
    @pytest.fixture()
    def client(self):
        from fastapi.testclient import TestClient
        from nebo.server.daemon import DaemonState, create_daemon_app

        state = DaemonState()
        app = create_daemon_app(state)
        with TestClient(app) as c:
            c.post("/events?run_id=r1", json=[
                {"type": "run_start", "data": {"run_id": "r1", "script_path": "t.py"}},
                {"type": "loggable_register",
                 "data": {"loggable_id": "train", "kind": "node", "func_name": "train"}},
                {"type": "metric", "loggable_id": "train", "name": "loss",
                 "metric_type": "line", "value": 0.5, "step": 0, "tags": []},
                {"type": "text", "loggable_id": "train", "name": "status",
                 "message": "hi"},
            ])
            yield c

    def test_run_exists(self, client) -> None:
        out = client.get("/resolve", params={"ref": "nebo://run/r1"}).json()
        assert out["resource"] == "run" and out["exists"] is True

    def test_missing_run(self, client) -> None:
        out = client.get("/resolve", params={"ref": "nebo://run/nope"}).json()
        assert out["exists"] is False

    def test_loggable_and_streams(self, client) -> None:
        out = client.get("/resolve", params={"ref": "nebo://run/r1/train"}).json()
        assert out["resource"] == "loggable" and out["exists"] is True
        out = client.get("/resolve", params={"ref": "nebo://run/r1/train/loss@3"}).json()
        assert out["resource"] == "stream" and out["exists"] is True
        assert out["step"] == 3
        out = client.get("/resolve", params={"ref": "nebo://run/r1/train/status"}).json()
        assert out["exists"] is True  # text streams resolve too
        out = client.get("/resolve", params={"ref": "nebo://run/r1/train/nope"}).json()
        assert out["exists"] is False

    def test_malformed_is_400(self, client) -> None:
        assert client.get("/resolve", params={"ref": "gopher://x"}).status_code == 400


class TestNodeIdCollisions:
    def test_different_functions_same_qualname_get_distinct_ids(self) -> None:
        from nebo.core import decorators as deco

        def make(module: str):
            def step():
                return 1
            step.__module__ = module
            step.__qualname__ = "step"
            return step

        f1, f2 = make("pkg_a.train"), make("pkg_b.eval")
        base = "collision_test_step"
        first = deco._resolve_node_id(f1, base)
        assert first == base
        # Same function again: idempotent, no qualification.
        assert deco._resolve_node_id(f1, base) == base
        with pytest.warns(UserWarning, match="already used by a different"):
            second = deco._resolve_node_id(f2, base)
        assert second == f"pkg_b.eval.{base}"
        # And the qualified id is stable on re-decoration too.
        assert deco._resolve_node_id(f2, base) == second
