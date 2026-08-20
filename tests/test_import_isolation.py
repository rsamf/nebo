"""The robotics extra must stay optional.

`mujoco`, `trimesh` and `yourdfpy` are SDK-side, opt-in dependencies. A
daemon (including the Space image) and a plain `import nebo` must work
without them, exactly as Pillow and httpx do — see tests/test_png.py for
the same guard on the imaging path.
"""

from __future__ import annotations

import contextlib

import pytest

ROBOTICS_DEPS = ("mujoco", "trimesh", "yourdfpy")


@contextlib.contextmanager
def _block_all(block_import):
    with contextlib.ExitStack() as stack:
        for name in ROBOTICS_DEPS:
            stack.enter_context(block_import(name))
        yield


def test_import_nebo_without_robotics(block_import):
    with _block_all(block_import):
        import importlib

        import nebo
        importlib.reload(nebo)
        assert hasattr(nebo, "log_body_model")
        assert hasattr(nebo, "log_body_transform")


def test_robotics_module_imports_without_its_dependencies(block_import):
    """The extras package itself is import-safe; only its functions aren't."""
    with _block_all(block_import):
        import importlib

        from nebo.extras import robotics
        importlib.reload(robotics)
        assert callable(robotics.compile_model)
        assert callable(robotics.mj_pose)


def test_daemon_never_imports_robotics(block_import):
    with _block_all(block_import):
        from nebo.server.daemon import create_daemon_app

        create_daemon_app()


def test_daemon_ingests_body_events_without_robotics(block_import):
    """A GLB is opaque bytes to the daemon — it never parses one."""
    import asyncio

    from nebo.server.daemon import DaemonState

    with _block_all(block_import):
        state = DaemonState()
        state.create_run("sim.py", run_id="r1")
        asyncio.new_event_loop().run_until_complete(state.ingest_events([
            {
                "type": "body_model", "loggable_id": "__global__",
                "name": "arm", "model_id": "abc123",
                "body_names": ["world"], "source_format": "mjcf",
                "data": b"glTF\x02\x00\x00\x00",
            },
            {
                "type": "body_transform", "loggable_id": "__global__",
                "name": "scene", "step": 0, "timestamp": 1.0,
                "instances": {
                    "default": {"model": "abc123", "pos_quat_xyzw": [0.0] * 7},
                },
            },
        ], "r1"))
        payload = state.run_actions("r1")
        assert payload["body_models"]["abc123"]["name"] == "arm"
        assert len(payload["actions"]["__global__"]) == 1


def test_compile_model_names_the_extra_when_mujoco_is_missing(block_import):
    from nebo.extras.robotics import compile_model

    with _block_all(block_import):
        with pytest.raises(ModuleNotFoundError, match=r"nebo\[robotics\]"):
            compile_model(mjcf="<mujoco/>")
        with pytest.raises(ModuleNotFoundError, match=r"nebo\[robotics\]"):
            compile_model(urdf="<robot/>")
