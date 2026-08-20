"""CLI and MCP read surfaces for the action modality."""

from __future__ import annotations

import json

import pytest

GLB = b"glTF\x02\x00\x00\x00body-model-bytes"

LISTING = {
    "actions": {
        "__global__": [
            {
                "loggable_id": "__global__", "name": "rollout",
                "step": step, "timestamp": 1.0 + step,
                "instances": {
                    "policy": {"model": "abc123", "pos_quat_xyzw": [0.0] * 14},
                    "reference": {"model": "abc123", "pos_quat_xyzw": [0.0] * 14},
                },
            }
            for step in range(3)
        ],
    },
    "body_models": {
        "abc123": {
            "model_id": "abc123", "name": "arm", "media_id": "m1",
            "body_names": ["world", "link1"], "source_format": "mjcf",
        },
    },
}


@pytest.fixture
def stub_client(monkeypatch):
    from nebo import client

    monkeypatch.setattr(
        client, "list_actions", lambda run_id, **kw: LISTING,
    )
    monkeypatch.setattr(
        client, "get_media",
        lambda run_id, media_id, **kw: (GLB, "model/gltf-binary"),
    )
    return client


def _run_cli(argv):
    from unittest.mock import patch

    from nebo.cli import main

    with patch("sys.argv", ["nebo"] + argv):
        return main()


def test_actions_ls_prints_scene_instances_and_steps(stub_client, capsys):
    _run_cli(["actions", "ls", "--run", "r1"])
    out = capsys.readouterr().out
    assert "rollout" in out
    assert "policy,reference" in out
    assert "0-2" in out
    # The model manifest is listed so `actions get` has an id to use.
    assert "model_id=abc123" in out
    assert "2 bodies" in out


def test_actions_ls_json_is_the_raw_payload(stub_client, capsys):
    _run_cli(["actions", "ls", "--run", "r1", "--json"])
    assert json.loads(capsys.readouterr().out) == LISTING


def test_actions_ls_handles_a_run_with_no_scenes(monkeypatch, capsys):
    from nebo import client

    monkeypatch.setattr(
        client, "list_actions",
        lambda run_id, **kw: {"actions": {}, "body_models": {}},
    )
    _run_cli(["actions", "ls", "--run", "r1"])
    assert "No action scenes found." in capsys.readouterr().out


def test_actions_get_writes_the_glb_and_prints_its_path(
    stub_client, tmp_path, capsys,
):
    out = tmp_path / "arm.glb"
    _run_cli(["actions", "get", "abc123", "--run", "r1", "-o", str(out)])
    assert out.read_bytes() == GLB
    assert str(out.resolve()) in capsys.readouterr().out


def test_actions_get_rejects_an_unknown_model(stub_client, capsys):
    with pytest.raises(SystemExit) as exc:
        _run_cli(["actions", "get", "nope", "--run", "r1"])
    assert exc.value.code == 1
    assert "no body model 'nope'" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_mcp_list_actions_summarizes_scenes(monkeypatch):
    from nebo.mcp import tools

    monkeypatch.setattr(
        tools._client, "list_actions", lambda run_id, **kw: LISTING,
    )
    out = await tools.list_actions("r1")
    (scene,) = out["scenes"]
    assert scene["name"] == "rollout"
    assert sorted(scene["instances"]) == ["policy", "reference"]
    assert scene["frames"] == 3
    assert scene["steps"] == [0, 2]
    assert scene["models"] == ["abc123"]
    assert out["body_models"]["abc123"]["body_names"] == ["world", "link1"]


@pytest.mark.asyncio
async def test_mcp_list_actions_reports_an_unreachable_daemon(monkeypatch):
    from nebo.mcp import tools

    def _boom(*a, **kw):
        raise ConnectionError("refused")

    monkeypatch.setattr(tools._client, "list_actions", _boom)
    out = await tools.list_actions("r1")
    assert "error" in out


def test_mcp_exposes_the_tool_in_its_schema():
    from nebo.mcp.server import MCP_TOOLS

    assert any(t["name"] == "nebo_list_actions" for t in MCP_TOOLS)
