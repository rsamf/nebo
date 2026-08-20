"""MJCF -> body/geom extraction via MuJoCo.

MuJoCo owns the hard parts of MJCF (defaults and classes, ``<include>``,
``meshdir``/``strippath``, mesh scaling, inertial recentering), so nebo
compiles the model and reads the resulting ``MjModel`` arrays rather than
parsing XML itself.

Note on mesh frames: MuJoCo recenters mesh vertices on the mesh's center
of mass and bakes the compensating offset into ``geom_pos`` / ``geom_quat``.
Reading both from the compiled model therefore reproduces the authored
pose exactly.
"""

from __future__ import annotations

import os
from typing import Any

from nebo.extras.robotics.gltf import (
    COLLISION, DEFAULT_COLOR, VISUAL, Body, Geom, classify,
)

_INSTALL_HINT = (
    "MJCF support requires MuJoCo. Install it with: "
    "pip install 'nebo[robotics]'"
)


def _load_mujoco():
    try:
        import mujoco
    except ImportError as exc:  # pragma: no cover - exercised via message
        raise ModuleNotFoundError(_INSTALL_HINT) from exc
    return mujoco


def load_model(source: Any):
    """Return an ``MjModel`` for a path, an inline XML string, or a model."""
    mujoco = _load_mujoco()
    if isinstance(source, mujoco.MjModel):
        return source
    if isinstance(source, (str, os.PathLike)):
        text = os.fspath(source)
        # An inline document is unambiguous: a filesystem path never
        # contains '<'.
        if "<" in text:
            return mujoco.MjModel.from_xml_string(text)
        return mujoco.MjModel.from_xml_path(text)
    raise TypeError(
        "mjcf= must be a path, an inline XML string, or a mujoco.MjModel, "
        f"got {type(source).__name__}"
    )


def _primitive(mujoco, model, gid: int):
    """Build a trimesh primitive for a non-mesh geom, or None to skip."""
    import numpy as np
    import trimesh

    gtype = int(model.geom_type[gid])
    size = np.asarray(model.geom_size[gid], dtype=np.float64)
    T = mujoco.mjtGeom

    if gtype == T.mjGEOM_PLANE:
        # MuJoCo planes are infinite when a half-size is 0; render a large
        # but finite slab so the scene has a visible ground.
        sx = size[0] if size[0] > 0 else 10.0
        sy = size[1] if size[1] > 0 else 10.0
        mesh = trimesh.creation.box(extents=[2 * sx, 2 * sy, 0.002])
        mesh.apply_translation([0.0, 0.0, -0.001])
        return mesh
    if gtype == T.mjGEOM_SPHERE:
        return trimesh.creation.icosphere(subdivisions=2, radius=float(size[0]))
    if gtype == T.mjGEOM_CAPSULE:
        return trimesh.creation.capsule(
            radius=float(size[0]), height=float(2 * size[1]),
        )
    if gtype == T.mjGEOM_ELLIPSOID:
        mesh = trimesh.creation.icosphere(subdivisions=2, radius=1.0)
        mesh.apply_scale([float(size[0]), float(size[1]), float(size[2])])
        return mesh
    if gtype == T.mjGEOM_CYLINDER:
        return trimesh.creation.cylinder(
            radius=float(size[0]), height=float(2 * size[1]),
        )
    if gtype == T.mjGEOM_BOX:
        return trimesh.creation.box(extents=(2 * size).tolist())
    # Heightfields, SDFs and the decorative geom types carry no exportable
    # surface; skipping one loses a visual, never a body.
    return None


def _mesh(model, gid: int):
    import numpy as np
    import trimesh

    data_id = int(model.geom_dataid[gid])
    if data_id < 0:
        return None
    v0 = int(model.mesh_vertadr[data_id])
    vn = int(model.mesh_vertnum[data_id])
    f0 = int(model.mesh_faceadr[data_id])
    fn = int(model.mesh_facenum[data_id])
    verts = np.asarray(model.mesh_vert[v0:v0 + vn], dtype=np.float64)
    faces = np.asarray(model.mesh_face[f0:f0 + fn], dtype=np.int64)
    if len(verts) == 0 or len(faces) == 0:
        return None
    return trimesh.Trimesh(vertices=verts, faces=faces, process=False)


def _color(model, gid: int):
    mat_id = int(model.geom_matid[gid])
    if mat_id >= 0 and model.nmat > 0:
        rgba = model.mat_rgba[mat_id]
    else:
        rgba = model.geom_rgba[gid]
    return tuple(float(c) for c in rgba) or DEFAULT_COLOR


def compile_mjcf(source: Any) -> list[Body]:
    """Compile an MJCF source into ordered bodies with their geometry."""
    import numpy as np
    import trimesh

    mujoco = _load_mujoco()
    model = load_model(source)

    bodies = [
        Body(name=mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, i)
             or f"body{i}")
        for i in range(model.nbody)
    ]

    raw: list[tuple[int, Any, Any, str, tuple]] = []
    for gid in range(model.ngeom):
        gtype = int(model.geom_type[gid])
        if gtype == mujoco.mjtGeom.mjGEOM_MESH:
            mesh = _mesh(model, gid)
        else:
            mesh = _primitive(mujoco, model, gid)
        if mesh is None:
            continue
        transform = trimesh.transformations.quaternion_matrix(
            np.asarray(model.geom_quat[gid], dtype=np.float64)  # wxyz
        )
        transform[:3, 3] = np.asarray(model.geom_pos[gid], dtype=np.float64)
        # The physically meaningful split: a geom that collides is
        # collision geometry, a geom that cannot is there to be looked at.
        # MuJoCo Menagerie's visual/collision classes follow exactly this.
        #
        # Ground planes are the standing exception. A floor collides by
        # default and is almost never given a visual-only twin, so the
        # rule above would hide the ground in every scene. A plane is
        # always something the user meant to see.
        contact = (
            int(model.geom_contype[gid]) != 0
            or int(model.geom_conaffinity[gid]) != 0
        )
        is_plane = gtype == mujoco.mjtGeom.mjGEOM_PLANE
        kind = VISUAL if (is_plane or not contact) else COLLISION
        raw.append((int(model.geom_bodyid[gid]), mesh, transform, kind,
                    _color(model, gid), is_plane))

    # Promotion (see gltf.classify) is decided by the *articulated*
    # geometry only: a floor forced to visual must not convince a model
    # whose bodies are all collision-only that it has visuals already.
    articulated = [r[3] for r in raw if not r[5]]
    promoted = iter(classify(articulated))
    for body_id, mesh, transform, kind, color, is_plane in raw:
        resolved = kind if is_plane else next(promoted)
        bodies[body_id].geoms.append(
            Geom(mesh=mesh, transform=transform, kind=resolved, color=color)
        )
    return bodies
