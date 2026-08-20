"""URDF -> body/geom extraction via yourdfpy + trimesh.

URDF states its visual and collision geometry explicitly, so unlike MJCF
there is nothing to infer: ``<visual>`` becomes visual geometry and
``<collision>`` becomes collision geometry, each with the link-local
``origin`` the file declares.

Bodies are the URDF's links, in document order — that is the order
``BodyModelRef.body_names`` records and the order every logged pose array
must follow.
"""

from __future__ import annotations

import io
import os
from typing import Any

from nebo.extras.robotics.gltf import (
    COLLISION, DEFAULT_COLOR, VISUAL, Body, Geom, classify,
)

_INSTALL_HINT = (
    "URDF support requires yourdfpy and trimesh. Install them with: "
    "pip install 'nebo[robotics]'"
)


def _load_yourdfpy():
    try:
        import yourdfpy
    except ImportError as exc:  # pragma: no cover - exercised via message
        raise ModuleNotFoundError(_INSTALL_HINT) from exc
    return yourdfpy


def load_robot(source: Any) -> tuple[Any, str]:
    """Return ``(URDF, mesh_search_dir)`` for a path or inline XML string."""
    yourdfpy = _load_yourdfpy()
    if not isinstance(source, (str, os.PathLike)):
        raise TypeError(
            "urdf= must be a path or an inline XML string, "
            f"got {type(source).__name__}"
        )
    text = os.fspath(source)
    kwargs = dict(
        load_meshes=True, build_scene_graph=False,
        load_collision_meshes=True, build_collision_scene_graph=False,
    )
    if "<" in text:
        # Inline documents have no file of their own, so package:// and
        # relative mesh paths resolve against the working directory.
        return yourdfpy.URDF.load(io.StringIO(text), **kwargs), os.getcwd()
    return yourdfpy.URDF.load(text, **kwargs), os.path.dirname(
        os.path.abspath(text)
    )


def _geometry_to_mesh(geometry: Any, search_dir: str):
    import numpy as np
    import trimesh
    import yourdfpy

    if geometry.box is not None:
        return trimesh.creation.box(
            extents=np.asarray(geometry.box.size, dtype=np.float64)
        )
    if geometry.sphere is not None:
        return trimesh.creation.icosphere(
            subdivisions=2, radius=float(geometry.sphere.radius)
        )
    if geometry.cylinder is not None:
        return trimesh.creation.cylinder(
            radius=float(geometry.cylinder.radius),
            height=float(geometry.cylinder.length),
        )
    if geometry.mesh is not None:
        path = yourdfpy.filename_handler_magic(
            geometry.mesh.filename, dir=search_dir,
        )
        try:
            mesh = trimesh.load(path, force="mesh", process=False)
        except Exception:
            # A missing or unreadable mesh loses one visual, never a body.
            return None
        scale = getattr(geometry.mesh, "scale", None)
        if scale is not None:
            mesh.apply_scale(np.asarray(scale, dtype=np.float64))
        return mesh
    return None


def _color(robot: Any, material: Any):
    if material is not None:
        if material.color is not None:
            return tuple(float(c) for c in material.color.rgba)
        # A <material name="..."/> reference resolves against the
        # robot-level material table.
        named = getattr(robot.robot, "materials", None) or []
        for candidate in named:
            if candidate.name == material.name and candidate.color is not None:
                return tuple(float(c) for c in candidate.color.rgba)
    return DEFAULT_COLOR


def compile_urdf(source: Any) -> list[Body]:
    """Compile a URDF source into ordered bodies with their geometry."""
    import numpy as np

    robot, search_dir = load_robot(source)
    bodies: list[Body] = []
    pending: list[tuple[int, Any, Any, str, tuple]] = []

    for link in robot.robot.links:
        body_index = len(bodies)
        bodies.append(Body(name=link.name))
        for element, kind in (
            *(( v, VISUAL) for v in link.visuals),
            *((c, COLLISION) for c in link.collisions),
        ):
            mesh = _geometry_to_mesh(element.geometry, search_dir)
            if mesh is None:
                continue
            origin = element.origin
            transform = (
                np.eye(4) if origin is None
                else np.asarray(origin, dtype=np.float64)
            )
            color = (
                _color(robot, getattr(element, "material", None))
                if kind == VISUAL else DEFAULT_COLOR
            )
            pending.append((body_index, mesh, transform, kind, color))

    for (body_index, mesh, transform, _, color), resolved in zip(
        pending, classify([p[3] for p in pending])
    ):
        bodies[body_index].geoms.append(
            Geom(mesh=mesh, transform=transform, kind=resolved, color=color)
        )
    return bodies
