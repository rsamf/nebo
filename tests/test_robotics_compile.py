"""Model compilation for the action modality (nebo[robotics])."""

from __future__ import annotations

import json
import struct

import numpy as np
import pytest

from nebo.extras.robotics import mj_pose
from nebo.extras.robotics.gltf import (
    Body, COLLISION, Geom, VISUAL, body_node_name, build_glb, classify,
    geom_node_name,
)

# --- mj_pose ---------------------------------------------------------------


class _FakeData:
    # MuJoCo stores quaternions scalar-first (wxyz).
    xpos = np.array([[0.0, 0.0, 0.0], [1.0, 2.0, 3.0]])
    xquat = np.array([[1.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 1.0]])


def test_mj_pose_reorders_quaternion_to_xyzw():
    out = mj_pose(None, _FakeData())
    assert out.shape == (2, 7)
    np.testing.assert_allclose(out[0], [0, 0, 0, 0, 0, 0, 1])
    np.testing.assert_allclose(out[1], [1, 2, 3, 0, 0, 1, 0])


def test_mj_pose_does_not_alias_simulator_buffers():
    data = _FakeData()
    out = mj_pose(None, data)
    out[0, 0] = 99.0
    assert data.xpos[0, 0] == 0.0


# --- node naming (twin of ui/src/components/actions/sceneNodes.ts) ---------


def test_node_names_follow_the_documented_contract():
    assert body_node_name(3, "pelvis") == "nebo:body:3:pelvis"
    assert geom_node_name(COLLISION, 8) == "nebo:geom:collision:8"


def test_collision_only_models_are_promoted_to_visual():
    # Nothing else exists to render, so hiding it would show an empty scene.
    assert classify([COLLISION, COLLISION]) == [VISUAL, VISUAL]


def test_authored_split_is_preserved():
    assert classify([VISUAL, COLLISION]) == [VISUAL, COLLISION]


# --- GLB assembly ----------------------------------------------------------


def _glb_nodes(glb: bytes) -> list[dict]:
    length = struct.unpack("<I", glb[12:16])[0]
    return json.loads(glb[20:20 + length])["nodes"]


def test_build_glb_emits_one_node_per_body_in_order():
    trimesh = pytest.importorskip("trimesh")
    box = trimesh.creation.box(extents=[0.1, 0.1, 0.1])
    bodies = [
        Body("world"),
        Body("link1", [Geom(box.copy(), np.eye(4), VISUAL, (1, 0, 0, 1))]),
        Body("link2", [Geom(box.copy(), np.eye(4), COLLISION, (0, 1, 0, 1))]),
    ]
    names = [n.get("name") for n in _glb_nodes(build_glb(bodies))]
    assert "nebo:body:0:world" in names
    assert "nebo:body:1:link1" in names
    assert "nebo:body:2:link2" in names
    assert any(n and n.startswith("nebo:geom:visual:") for n in names)
    assert any(n and n.startswith("nebo:geom:collision:") for n in names)


def test_build_glb_rejects_a_model_with_no_geometry():
    pytest.importorskip("trimesh")
    with pytest.raises(ValueError, match="no renderable geometry"):
        build_glb([Body("world")])


# --- real compilation (requires the extra) ---------------------------------

INLINE_MJCF = """
<mujoco model="arm">
  <worldbody>
    <geom name="floor" type="plane" size="1 1 0.1" rgba="0.3 0.3 0.3 1"/>
    <body name="link1" pos="0 0 0.1">
      <joint name="j1" type="hinge" axis="0 1 0"/>
      <geom name="g1" type="capsule" size="0.02 0.1" rgba="0.2 0.6 1 1"
            contype="0" conaffinity="0"/>
      <geom name="g1c" type="capsule" size="0.03 0.1"/>
      <body name="link2" pos="0 0 0.2">
        <geom name="g2" type="box" size=".02 .02 .1" rgba="1 .4 0 1"
              contype="0" conaffinity="0"/>
      </body>
    </body>
  </worldbody>
</mujoco>
"""

INLINE_URDF = """
<robot name="arm">
  <link name="base">
    <visual><origin xyz="0 0 0.05"/>
      <geometry><box size="0.2 0.2 0.1"/></geometry>
      <material name="blue"><color rgba="0.2 0.4 0.9 1"/></material></visual>
    <collision><origin xyz="0 0 0.05"/>
      <geometry><cylinder radius="0.12" length="0.1"/></geometry></collision>
  </link>
  <link name="arm1">
    <visual><origin xyz="0 0 0.15"/>
      <geometry><cylinder radius="0.03" length="0.3"/></geometry></visual>
  </link>
  <joint name="j1" type="revolute">
    <parent link="base"/><child link="arm1"/><origin xyz="0 0 0.1"/>
    <axis xyz="0 1 0"/><limit lower="-1" upper="1" effort="1" velocity="1"/>
  </joint>
</robot>
"""


def test_compile_mjcf_produces_glb_and_body_names():
    pytest.importorskip("mujoco")
    from nebo.extras.robotics import compile_model

    out = compile_model(mjcf=INLINE_MJCF)
    assert out.glb[:4] == b"glTF"
    assert out.source_format == "mjcf"
    assert out.body_names == ("world", "link1", "link2")
    assert len(out.model_id) == 16


def test_compile_mjcf_is_content_addressed():
    pytest.importorskip("mujoco")
    from nebo.extras.robotics import compile_model

    assert (
        compile_model(mjcf=INLINE_MJCF).model_id
        == compile_model(mjcf=INLINE_MJCF).model_id
    )


def test_mjcf_ground_plane_stays_visible():
    """A floor collides but is never given a visual twin — see mjcf.py."""
    pytest.importorskip("mujoco")
    from nebo.extras.robotics.mjcf import compile_mjcf

    world = compile_mjcf(INLINE_MJCF)[0]
    assert [g.kind for g in world.geoms] == [VISUAL]


def test_compile_urdf_produces_glb_and_link_names():
    pytest.importorskip("yourdfpy")
    from nebo.extras.robotics import compile_model

    out = compile_model(urdf=INLINE_URDF)
    assert out.glb[:4] == b"glTF"
    assert out.source_format == "urdf"
    assert out.body_names == ("base", "arm1")


def test_compile_urdf_keeps_the_authored_visual_collision_split():
    pytest.importorskip("yourdfpy")
    from nebo.extras.robotics.urdf import compile_urdf

    base = compile_urdf(INLINE_URDF)[0]
    assert sorted(g.kind for g in base.geoms) == [COLLISION, VISUAL]


def test_compile_model_requires_exactly_one_source():
    from nebo.extras.robotics import compile_model

    with pytest.raises(TypeError, match="exactly one of mjcf= or urdf="):
        compile_model()
    with pytest.raises(TypeError, match="exactly one of mjcf= or urdf="):
        compile_model(mjcf="<mujoco/>", urdf="<robot/>")
