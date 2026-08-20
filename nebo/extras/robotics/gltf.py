"""trimesh scene assembly and GLB export for compiled body models.

This module owns the **node-naming contract** that the web UI's
``ui/src/components/actions/sceneNodes.ts`` parses. The two are twin
parsers and must be kept in lockstep — exactly like ``nebo/core/refs.py``
and ``ui/src/lib/refs.ts``.

Layout of an exported GLB::

    nebo:body:0:world              one node per body, in model body order
    nebo:body:1:pelvis
      nebo:geom:visual:7           child geoms, posed in the body frame
      nebo:geom:collision:8

Body nodes are exported at identity: they are placeholders whose world
transform the UI overwrites on every frame from the logged
``pos_quat_xyzw`` array. Geom nodes carry the geometry's fixed pose
*relative to its body*, which never changes at runtime.

Names are used rather than glTF ``extras`` because names survive every
exporter and loader reliably, while ``extras`` do not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

NODE_BODY_PREFIX = "nebo:body:"
NODE_GEOM_PREFIX = "nebo:geom:"

VISUAL = "visual"
COLLISION = "collision"

# MuJoCo's own default geom color, used whenever a source model leaves a
# geometry's appearance unspecified.
DEFAULT_COLOR = (0.5, 0.5, 0.5, 1.0)


@dataclass
class Geom:
    """One renderable primitive, posed relative to its parent body."""

    mesh: Any                      # trimesh.Trimesh
    transform: Any                 # (4, 4) float ndarray, body-local
    kind: str = VISUAL             # VISUAL | COLLISION
    color: tuple[float, float, float, float] = DEFAULT_COLOR


@dataclass
class Body:
    """One rigid body: a name plus the geometry rigidly attached to it."""

    name: str
    geoms: list[Geom] = field(default_factory=list)


def body_node_name(index: int, name: str) -> str:
    return f"{NODE_BODY_PREFIX}{index}:{name}"


def geom_node_name(kind: str, index: int) -> str:
    return f"{NODE_GEOM_PREFIX}{kind}:{index}"


def classify(kinds: Sequence[str]) -> list[str]:
    """Promote collision-only geometry to visual when nothing else exists.

    A model that never separates its visual and collision geometry (a
    hand-written MJCF, a URDF with only ``<collision>`` tags) would
    otherwise render as an empty scene, since collision display is off by
    default. When at least one true visual geom exists the split is
    honored as authored.
    """
    kinds = list(kinds)
    if any(k == VISUAL for k in kinds):
        return kinds
    return [VISUAL] * len(kinds)


def build_glb(bodies: Sequence[Body]) -> bytes:
    """Flatten bodies into a single self-contained GLB."""
    import numpy as np
    import trimesh
    from trimesh.visual import TextureVisuals
    from trimesh.visual.material import PBRMaterial

    scene = trimesh.Scene()
    geom_index = 0

    for body_index, body in enumerate(bodies):
        node = body_node_name(body_index, body.name)
        # Placeholder frame: the UI rewrites this every frame. Bodies with
        # no geometry still get a node so pose indices stay aligned with
        # the model's body order.
        scene.graph.update(
            frame_to=node, frame_from=scene.graph.base_frame,
            matrix=np.eye(4),
        )
        for geom in body.geoms:
            mesh = geom.mesh
            r, g, b, a = geom.color
            mesh.visual = TextureVisuals(
                material=PBRMaterial(
                    name=f"m{geom_index}",
                    baseColorFactor=[
                        float(r), float(g), float(b), float(a),
                    ],
                    metallicFactor=0.05,
                    roughnessFactor=0.75,
                    # Robot meshes are frequently open shells; double-siding
                    # them avoids holes when the camera orbits inside one.
                    doubleSided=True,
                )
            )
            scene.add_geometry(
                mesh,
                node_name=geom_node_name(geom.kind, geom_index),
                geom_name=f"g{geom_index}",
                parent_node_name=node,
                transform=np.asarray(geom.transform, dtype=np.float64),
            )
            geom_index += 1

    if geom_index == 0:
        raise ValueError(
            "the compiled model contains no renderable geometry "
            "(no visual or collision geoms were found)"
        )
    return scene.export(file_type="glb")
