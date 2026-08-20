"""Robotics extras for nebo.

Install with: ``pip install 'nebo[robotics]'``

Turns MJCF and URDF descriptions into the GLB assets ``nb.log_body_model``
publishes, and converts simulator state into nebo's pose layout. Every
robotics dependency (mujoco, yourdfpy, trimesh) is imported lazily inside
a function, so importing this module — or nebo itself — never requires
the extra.
"""

from __future__ import annotations

from typing import Any

from nebo.extras.robotics.compile import CompiledModel, compile_model


def mj_pose(mj_model: Any, mj_data: Any) -> Any:
    """MuJoCo state -> nebo's ``(nbody, 7)`` ``[x,y,z, qx,qy,qz,qw]`` layout.

    MuJoCo stores quaternions scalar-first (``wxyz``); nebo accepts exactly
    one convention, vector-scalar (``xyzw``), and names it in every
    parameter and wire field. Doing the reorder by hand is the single
    easiest way to log a subtly wrong rotation, so this helper exists.

    ``mj_model`` is accepted (and currently unused) so the call reads like
    the rest of the MuJoCo API, where model and data travel together.

    Usage::

        mujoco.mj_step(m, d)
        nb.log_body_transform("rollout", ref, mj_pose(m, d), step=t)
    """
    import numpy as np

    xpos = np.asarray(mj_data.xpos, dtype=np.float64)
    xquat = np.asarray(mj_data.xquat, dtype=np.float64)
    return np.concatenate([xpos, xquat[:, [1, 2, 3, 0]]], axis=1)


__all__ = ["CompiledModel", "compile_model", "mj_pose"]
