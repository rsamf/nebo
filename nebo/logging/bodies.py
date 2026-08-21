"""Pose payload normalization for the action modality.

``nb.log_body_transform`` accepts per-body **world** poses — never joint
coordinates — so the web UI can place bodies without implementing forward
kinematics for two different robot description formats.

One instance's poses arrive as either

* ``(N, 7)`` — ``[x, y, z, qx, qy, qz, qw]`` per body, or
* ``(N, 4, 4)`` — homogeneous transforms, decomposed here,

and a dict of either keyed by instance label puts several bodies in one
scene, mirroring the ``{label: value}`` convention of ``log_bar`` /
``log_pie`` / ``log_scatter``.

Everything leaves this module as ``7 * N`` **little-endian float32 values
packed as raw bytes**. msgpack has no float32 for Python floats, so a
list of them costs 9 B each; positions are millimetre-scale and
quaternions are unit, so float32 loses nothing that matters and halves
the largest non-model cost in a scene run. Consumers accept a plain list
too — see :func:`decode_poses` — so files written before this change
still read.

Quaternions are vector-scalar (``xyzw``). There is exactly one accepted
convention and it is named in the parameter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

#: Instance label used when a bare array is logged rather than a dict.
DEFAULT_INSTANCE = "default"


@dataclass(frozen=True)
class BodyModelRef:
    """Handle to a body model published by ``nb.log_body_model``.

    ``model_id`` is the content address of the compiled GLB
    (``sha256(glb)[:16]``), which is also the media id the daemon serves
    it under — so the same model logged by many runs is stored once.
    """

    name: str
    model_id: str
    body_names: tuple[str, ...]
    source_format: str

    def __len__(self) -> int:
        return len(self.body_names)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"BodyModelRef(name={self.name!r}, model_id={self.model_id!r}, "
            f"bodies={len(self.body_names)}, format={self.source_format!r})"
        )


def _mat_to_quat_xyzw(m: Any) -> tuple[float, float, float, float]:
    """Rotation matrix -> xyzw quaternion (branch-on-trace, numerically safe)."""
    import numpy as np

    m = np.asarray(m, dtype=np.float64)
    trace = m[0, 0] + m[1, 1] + m[2, 2]
    if trace > 0.0:
        s = np.sqrt(trace + 1.0) * 2.0
        w = 0.25 * s
        x = (m[2, 1] - m[1, 2]) / s
        y = (m[0, 2] - m[2, 0]) / s
        z = (m[1, 0] - m[0, 1]) / s
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = np.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2.0
        w = (m[2, 1] - m[1, 2]) / s
        x = 0.25 * s
        y = (m[0, 1] + m[1, 0]) / s
        z = (m[0, 2] + m[2, 0]) / s
    elif m[1, 1] > m[2, 2]:
        s = np.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2.0
        w = (m[0, 2] - m[2, 0]) / s
        x = (m[0, 1] + m[1, 0]) / s
        y = 0.25 * s
        z = (m[1, 2] + m[2, 1]) / s
    else:
        s = np.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2.0
        w = (m[1, 0] - m[0, 1]) / s
        x = (m[0, 2] + m[2, 0]) / s
        y = (m[1, 2] + m[2, 1]) / s
        z = 0.25 * s
    return float(x), float(y), float(z), float(w)


def _as_array(value: Any):
    """Array-ify a pose payload, tolerating framework tensors.

    Robotics poses usually live on the GPU, and `np.asarray` on a CUDA
    tensor raises a bare numpy TypeError that never mentions nebo. Bring
    the tensor home instead — this is the same courtesy `prepare_image`
    extends to torch images.
    """
    import numpy as np

    if hasattr(value, "detach") and hasattr(value, "cpu"):
        value = value.detach().cpu()
    return np.asarray(value, dtype=np.float64)


#: Wire dtype for pose payloads: little-endian float32.
POSE_DTYPE = "<f4"


def decode_poses(value: Any) -> list[float]:
    """Wire pose payload -> a flat list of floats.

    Accepts the float32 bytes writers emit today, a base64 string (the
    JSON wire boundary), or a plain list (pre-float32 files, and anything
    hand-authored). Consumers must go through this rather than assuming a
    shape, exactly as media accepts both bytes and base64.
    """
    import numpy as np

    if isinstance(value, (bytes, bytearray, memoryview)):
        return np.frombuffer(bytes(value), dtype=POSE_DTYPE).tolist()
    if isinstance(value, str):
        import base64

        raw = base64.b64decode(value)
        return np.frombuffer(raw, dtype=POSE_DTYPE).tolist()
    return [float(v) for v in (value or ())]


def _flatten_one(value: Any, n_bodies: int, label: str) -> bytes:
    """One instance's poses -> ``7 * n_bodies`` float32 values as bytes."""
    import numpy as np

    arr = _as_array(value)
    where = f"instance {label!r}" if label != DEFAULT_INSTANCE else "poses"

    if arr.ndim == 3 and arr.shape[1:] == (4, 4):
        out = np.empty((arr.shape[0], 7), dtype=np.float64)
        out[:, :3] = arr[:, :3, 3]
        for i in range(arr.shape[0]):
            out[i, 3:] = _mat_to_quat_xyzw(arr[i, :3, :3])
        arr = out
    elif arr.ndim != 2 or arr.shape[1] != 7:
        raise TypeError(
            f"{where}: expected shape (N, 7) as [x, y, z, qx, qy, qz, qw] "
            f"or (N, 4, 4) homogeneous transforms, got shape {arr.shape}"
        )

    if arr.shape[0] != n_bodies:
        raise ValueError(
            f"{where}: the model has {n_bodies} bodies but {arr.shape[0]} "
            f"poses were given. Poses must be in the model's body order "
            f"(see BodyModelRef.body_names)."
        )
    if not np.isfinite(arr).all():
        raise ValueError(f"{where}: poses contain NaN or infinite values")

    # A non-unit quaternion renders a sheared, silently-wrong body. The
    # tolerance is far looser than float32 round-trip error, so it only
    # catches real mistakes: an unnormalized quaternion, a scaled one, or
    # a rotation matrix flattened into the wrong slots. (It cannot catch a
    # wxyz/xyzw swap — that preserves the norm — which is why `mj_pose`
    # exists for the framework that gets it wrong.)
    norms = np.linalg.norm(arr[:, 3:], axis=1)
    bad = np.abs(norms - 1.0) > 1e-3
    if bad.any():
        i = int(np.argmax(bad))
        raise ValueError(
            f"{where}: quaternion for body {i} has norm {norms[i]:.6g}, "
            f"expected 1. Poses are [x, y, z, qx, qy, qz, qw] with a unit "
            f"quaternion in xyzw order."
        )
    return arr.reshape(-1).astype(POSE_DTYPE, copy=False).tobytes()


def normalize_instances(
    value: Any, n_bodies: int,
) -> dict[str, bytes]:
    """Normalize a pose payload to ``{instance_label: float32 bytes}``.

    A bare array is shorthand for a single ``"default"`` instance; a dict
    names one instance per key, exactly like ``log_bar``'s
    ``{label: value}``.
    """
    if isinstance(value, Mapping):
        if not value:
            raise ValueError(
                "pos_quat_xyzw: the instance dict is empty; pass at least "
                "one {label: poses} entry"
            )
        out: dict[str, bytes] = {}
        for label, poses in value.items():
            if not isinstance(label, str) or not label:
                raise TypeError(
                    f"pos_quat_xyzw: instance labels must be non-empty "
                    f"strings, got {label!r}"
                )
            out[label] = _flatten_one(poses, n_bodies, label)
        return out
    return {DEFAULT_INSTANCE: _flatten_one(value, n_bodies, DEFAULT_INSTANCE)}
