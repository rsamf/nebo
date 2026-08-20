"""SDK surface for the action modality: log_body_model / log_body_transform."""

from __future__ import annotations

import numpy as np
import pytest

import nebo as nb
from nebo.extras.robotics import CompiledModel
from nebo.logging.bodies import BodyModelRef, normalize_instances

FAKE_GLB = b"glTF\x02\x00\x00\x00fake-model-bytes"


def identity_poses(n: int) -> np.ndarray:
    """`n` bodies at the origin with unit (xyzw) quaternions.

    Not `np.zeros((n, 7))`: an all-zero quaternion is not a rotation, and
    normalize_instances rejects it.
    """
    poses = np.zeros((n, 7))
    poses[:, 6] = 1.0
    return poses


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
    poses = identity_poses(3)
    out = normalize_instances(poses, n_bodies=3)
    assert list(out) == ["default"]
    assert len(out["default"]) == 21


def test_dict_keys_become_instance_labels():
    out = normalize_instances(
        {"policy": identity_poses(2), "reference": identity_poses(2)},
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
        normalize_instances(identity_poses(2), n_bodies=3)


def test_wrong_trailing_dimension_is_a_type_error():
    with pytest.raises(TypeError, match=r"\(N, 7\)"):
        normalize_instances(np.zeros((2, 3)), n_bodies=2)


def test_non_finite_poses_are_rejected():
    poses = identity_poses(2)
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
    nb.log_body_transform("scene", ref, identity_poses(2), step=0)

    (frame,) = capturing_client.by_type("body_transform")
    assert frame["name"] == "scene"
    assert frame["step"] == 0
    assert list(frame["instances"]) == ["default"]
    assert frame["instances"]["default"]["model"] == ref.model_id
    assert len(frame["instances"]["default"]["pos_quat_xyzw"]) == 14


def test_instances_share_one_frame(capturing_client, stub_compile):
    ref = nb.log_body_model("arm", mjcf="<mujoco/>")
    nb.log_body_transform("scene", ref, {
        "policy": identity_poses(2),
        "reference": identity_poses(2) + np.array([1.0, 1, 1, 0, 0, 0, 0]),
    }, step=4)

    (frame,) = capturing_client.by_type("body_transform")
    assert sorted(frame["instances"]) == ["policy", "reference"]
    assert frame["instances"]["reference"]["pos_quat_xyzw"][0] == 1.0


def test_step_auto_increments_per_scene(capturing_client, stub_compile):
    ref = nb.log_body_model("arm", mjcf="<mujoco/>")
    for _ in range(3):
        nb.log_body_transform("a", ref, identity_poses(2))
    nb.log_body_transform("b", ref, identity_poses(2))

    frames = capturing_client.by_type("body_transform")
    assert [f["step"] for f in frames if f["name"] == "a"] == [0, 1, 2]
    assert [f["step"] for f in frames if f["name"] == "b"] == [0]


def test_explicit_step_advances_the_cursor_past_it(
    capturing_client, stub_compile,
):
    ref = nb.log_body_model("arm", mjcf="<mujoco/>")
    nb.log_body_transform("a", ref, identity_poses(2), step=10)
    nb.log_body_transform("a", ref, identity_poses(2))
    assert [f["step"] for f in capturing_client.by_type("body_transform")] == [10, 11]


def test_model_can_be_named_by_string(capturing_client, stub_compile):
    ref = nb.log_body_model("arm", mjcf="<mujoco/>")
    nb.log_body_transform("scene", "arm", identity_poses(2))
    (frame,) = capturing_client.by_type("body_transform")
    assert frame["instances"]["default"]["model"] == ref.model_id


def test_unknown_model_name_raises(capturing_client, stub_compile):
    nb.log_body_model("arm", mjcf="<mujoco/>")
    with pytest.raises(ValueError, match="no body model named 'nope'"):
        nb.log_body_transform("scene", "nope", identity_poses(2))


def test_pose_count_must_match_the_model(capturing_client, stub_compile):
    ref = nb.log_body_model("arm", mjcf="<mujoco/>")
    with pytest.raises(ValueError, match="2 bodies but 5 poses"):
        nb.log_body_transform("scene", ref, identity_poses(5))


def test_body_model_is_structural_and_transform_is_not():
    from nebo.core.client import STRUCTURAL_TYPES

    assert "body_model" in STRUCTURAL_TYPES
    assert "body_transform" not in STRUCTURAL_TYPES


# --- regressions from dogfooding on a 27-body humanoid ---------------------


def test_same_model_can_be_published_under_two_names(
    capturing_client, stub_compile,
):
    """Comparing two policies means publishing one robot twice by name.

    The registry is keyed by content address, so the second call is a cache
    hit — but it must still register the requested name, or referring to
    that name raises "no body model named ...".
    """
    a = nb.log_body_model("robot_A", mjcf="<mujoco/>")
    b = nb.log_body_model("robot_B", mjcf="<mujoco/>")

    assert b.name == "robot_B"
    assert b.model_id == a.model_id
    # Identical bytes are still only sent once.
    assert len(capturing_client.by_type("body_model")) == 1

    nb.log_body_transform("scene", "robot_B", identity_poses(2))
    (frame,) = capturing_client.by_type("body_transform")
    assert frame["instances"]["default"]["model"] == a.model_id


def test_unnormalized_quaternion_is_rejected():
    poses = identity_poses(2)
    poses[1, 3:] = [0.0, 0.0, 0.0, 2.0]
    with pytest.raises(ValueError, match="body 1 has norm 2"):
        normalize_instances(poses, n_bodies=2)


def test_float32_rounding_is_within_tolerance():
    """A float32 round-trip must not trip the unit-quaternion check."""
    poses = identity_poses(2)
    poses[:, 3:] = np.array([0.5, 0.5, 0.5, 0.5])
    out = normalize_instances(poses.astype(np.float32), n_bodies=2)
    assert len(out["default"]) == 14


def test_gpu_style_tensors_are_brought_home(capturing_client, stub_compile):
    """A tensor exposing .detach()/.cpu() must not surface a numpy error."""
    class FakeCudaTensor:
        def __init__(self, arr):
            self._arr = arr
            self.detached = False

        def detach(self):
            self.detached = True
            return self

        def cpu(self):
            return self._arr

        def __array__(self, *a, **kw):
            raise TypeError(
                "can't convert cuda:0 device type tensor to numpy."
            )

    ref = nb.log_body_model("arm", mjcf="<mujoco/>")
    nb.log_body_transform("scene", ref, FakeCudaTensor(identity_poses(2)))
    (frame,) = capturing_client.by_type("body_transform")
    assert len(frame["instances"]["default"]["pos_quat_xyzw"]) == 14
