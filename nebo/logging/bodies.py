"""Pose payload normalization for the action modality.

``nb.log_body_transform`` accepts per-body **world** poses — never joint
coordinates — so the web UI can place bodies without implementing forward
kinematics for two different robot description formats.

One instance's poses arrive as either

* ``(N, 7)`` — ``[x, y, z, qx, qy, qz, qw]`` per body, or
* ``(N, 4, 4)`` — homogeneous transforms, decomposed here,

and a dict of either keyed by instance label puts several bodies in one
scene, mirroring the ``{label: value}`` convention of ``log_bar`` /
``log_pie`` / ``log_scatter``. Everything leaves this module as a flat
list of ``7 * N`` floats, which costs roughly half of what nested lists
cost in msgpack.

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


def _flatten_one(value: Any, n_bodies: int, label: str) -> list[float]:
    """One instance's poses -> a flat list of ``7 * n_bodies`` floats."""
    import numpy as np

    arr = np.asarray(value, dtype=np.float64)
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
    return [float(v) for v in arr.reshape(-1)]


def normalize_instances(
    value: Any, n_bodies: int,
) -> dict[str, list[float]]:
    """Normalize a pose payload to ``{instance_label: flat 7N floats}``.

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
        out: dict[str, list[float]] = {}
        for label, poses in value.items():
            if not isinstance(label, str) or not label:
                raise TypeError(
                    f"pos_quat_xyzw: instance labels must be non-empty "
                    f"strings, got {label!r}"
                )
            out[label] = _flatten_one(poses, n_bodies, label)
        return out
    return {DEFAULT_INSTANCE: _flatten_one(value, n_bodies, DEFAULT_INSTANCE)}
