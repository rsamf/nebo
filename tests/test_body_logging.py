"""SDK surface for the action modality: log_body_model / log_body_transform."""

from __future__ import annotations

import numpy as np
import pytest

import nebo as nb
from nebo.extras.robotics import CompiledModel
from nebo.logging.bodies import BodyModelRef, normalize_instances

FAKE_GLB = b"glTF\x02\x00\x00\x00fake-model-bytes"


@pytest.fixture
def stub_compile(monkeypatch):
    """Swap the real MJCF/URDF compiler for a deterministic stub.

    Keeps the SDK tests fast and independent of the robotics extra; the
    real compilation path is covered by tests/test_robotics_compile.py.
    """
    def _compile(*, mjcf=None, urdf=None):
        return CompiledModel(
            glb=FAKE_GLB, body_names=("world", "link1"),
            source_format="mjcf" if mjcf is not None else "urdf",
        )

    monkeypatch.setattr("nebo.extras.robotics.compile_model", _compile)
    return _compile


# --- pose normalization ----------------------------------------------------


def test_bare_array_becomes_the_default_instance():
    poses = np.zeros((3, 7))
    poses[:, 6] = 1.0
    out = normalize_instances(poses, n_bodies=3)
    assert list(out) == ["default"]
    assert len(out["default"]) == 21


def test_dict_keys_become_instance_labels():
    out = normalize_instances(
        {"policy": np.zeros((2, 7)), "reference": np.zeros((2, 7))},
        n_bodies=2,
    )
    assert sorted(out) == ["policy", "reference"]


def test_4x4_transforms_are_decomposed_to_pos_and_xyzw_quat():
    t = np.tile(np.eye(4), (2, 1, 1))
    t[1, :3, 3] = [1.0, 2.0, 3.0]
    # 90 degrees about Z -> xyzw quaternion (0, 0, sin45, cos45)
    t[1, :3, :3] = [[0, -1, 0], [1, 0, 0], [0, 0, 1]]
    out = normalize_instances(t, n_bodies=2)["default"]
    assert out[:7] == [0, 0, 0, 0, 0, 0, 1]
    np.testing.assert_allclose(out[7:10], [1.0, 2.0, 3.0])
    np.testing.assert_allclose(
        out[10:14], [0.0, 0.0, np.sqrt(0.5), np.sqrt(0.5)], atol=1e-12
    )


def test_body_count_mismatch_names_both_counts():
    with pytest.raises(ValueError, match="3 bodies but 2 poses"):
        normalize_instances(np.zeros((2, 7)), n_bodies=3)


def test_wrong_trailing_dimension_is_a_type_error():
    with pytest.raises(TypeError, match=r"\(N, 7\)"):
        normalize_instances(np.zeros((2, 3)), n_bodies=2)


def test_non_finite_poses_are_rejected():
    poses = np.zeros((2, 7))
    poses[1, 0] = np.nan
    with pytest.raises(ValueError, match="NaN or infinite"):
        normalize_instances(poses, n_bodies=2)


def test_empty_instance_dict_is_rejected():
    with pytest.raises(ValueError, match="instance dict is empty"):
        normalize_instances({}, n_bodies=2)


# --- log_body_model --------------------------------------------------------


def test_log_body_model_emits_the_glb_and_returns_a_ref(
    capturing_client, stub_compile,
):
    ref = nb.log_body_model("arm", mjcf="<mujoco/>")
    assert isinstance(ref, BodyModelRef)
    assert ref.body_names == ("world", "link1")
    assert len(ref) == 2

    (event,) = capturing_client.by_type("body_model")
    assert event["name"] == "arm"
    assert event["data"] == FAKE_GLB
    assert event["model_id"] == ref.model_id
    assert event["body_names"] == ["world", "link1"]
    assert event["source_format"] == "mjcf"


def test_model_id_is_the_content_address(capturing_client, stub_compile):
    import hashlib

    ref = nb.log_body_model("arm", mjcf="<mujoco/>")
    assert ref.model_id == hashlib.sha256(FAKE_GLB).hexdigest()[:16]


def test_republishing_identical_bytes_emits_once(
    capturing_client, stub_compile,
):
    first = nb.log_body_model("arm", mjcf="<mujoco/>")
    second = nb.log_body_model("arm", mjcf="<mujoco/>")
    assert first is second
    assert len(capturing_client.by_type("body_model")) == 1


# --- log_body_transform ----------------------------------------------------


def test_log_body_transform_emits_a_default_instance(
    capturing_client, stub_compile,
):
    ref = nb.log_body_model("arm", mjcf="<mujoco/>")
    nb.log_body_transform("scene", ref, np.zeros((2, 7)), step=0)

    (frame,) = capturing_client.by_type("body_transform")
    assert frame["name"] == "scene"
    assert frame["step"] == 0
    assert list(frame["instances"]) == ["default"]
    assert frame["instances"]["default"]["model"] == ref.model_id
    assert len(frame["instances"]["default"]["pos_quat_xyzw"]) == 14


def test_instances_share_one_frame(capturing_client, stub_compile):
    ref = nb.log_body_model("arm", mjcf="<mujoco/>")
    nb.log_body_transform("scene", ref, {
        "policy": np.zeros((2, 7)),
        "reference": np.ones((2, 7)),
    }, step=4)

    (frame,) = capturing_client.by_type("body_transform")
    assert sorted(frame["instances"]) == ["policy", "reference"]
    assert frame["instances"]["reference"]["pos_quat_xyzw"][0] == 1.0


def test_step_auto_increments_per_scene(capturing_client, stub_compile):
    ref = nb.log_body_model("arm", mjcf="<mujoco/>")
    for _ in range(3):
        nb.log_body_transform("a", ref, np.zeros((2, 7)))
    nb.log_body_transform("b", ref, np.zeros((2, 7)))

    frames = capturing_client.by_type("body_transform")
    assert [f["step"] for f in frames if f["name"] == "a"] == [0, 1, 2]
    assert [f["step"] for f in frames if f["name"] == "b"] == [0]


def test_explicit_step_advances_the_cursor_past_it(
    capturing_client, stub_compile,
):
    ref = nb.log_body_model("arm", mjcf="<mujoco/>")
    nb.log_body_transform("a", ref, np.zeros((2, 7)), step=10)
    nb.log_body_transform("a", ref, np.zeros((2, 7)))
    assert [f["step"] for f in capturing_client.by_type("body_transform")] == [10, 11]


def test_model_can_be_named_by_string(capturing_client, stub_compile):
    ref = nb.log_body_model("arm", mjcf="<mujoco/>")
    nb.log_body_transform("scene", "arm", np.zeros((2, 7)))
    (frame,) = capturing_client.by_type("body_transform")
    assert frame["instances"]["default"]["model"] == ref.model_id


def test_unknown_model_name_raises(capturing_client, stub_compile):
    nb.log_body_model("arm", mjcf="<mujoco/>")
    with pytest.raises(ValueError, match="no body model named 'nope'"):
        nb.log_body_transform("scene", "nope", np.zeros((2, 7)))


def test_pose_count_must_match_the_model(capturing_client, stub_compile):
    ref = nb.log_body_model("arm", mjcf="<mujoco/>")
    with pytest.raises(ValueError, match="2 bodies but 5 poses"):
        nb.log_body_transform("scene", ref, np.zeros((5, 7)))


def test_body_model_is_structural_and_transform_is_not():
    from nebo.core.client import STRUCTURAL_TYPES

    assert "body_model" in STRUCTURAL_TYPES
    assert "body_transform" not in STRUCTURAL_TYPES
