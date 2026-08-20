"""Model compilation front door: MJCF or URDF in, one GLB out."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Optional

from nebo.extras.robotics.gltf import build_glb

#: Modules each source format needs, checked up front so a missing one
#: names the extra instead of surfacing a bare ImportError from three
#: frames deep.
_REQUIREMENTS = {
    "mjcf": ("mujoco", "trimesh"),
    "urdf": ("yourdfpy", "trimesh"),
}


def _require(source_format: str) -> None:
    import importlib

    missing = []
    for module in _REQUIREMENTS[source_format]:
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(module)
    if missing:
        raise ModuleNotFoundError(
            f"{source_format.upper()} support requires "
            f"{', '.join(missing)}. Install with: "
            f"pip install 'nebo[robotics]'"
        )


@dataclass(frozen=True)
class CompiledModel:
    """A body model flattened to a single self-contained GLB.

    ``model_id`` is ``sha256(glb)[:16]`` — the same content address the
    daemon derives for any other media, so a model logged by ten runs is
    stored once.
    """

    glb: bytes
    body_names: tuple[str, ...]
    source_format: str

    @property
    def model_id(self) -> str:
        return hashlib.sha256(self.glb).hexdigest()[:16]


def compile_model(
    *, mjcf: Optional[Any] = None, urdf: Optional[Any] = None,
) -> CompiledModel:
    """Compile exactly one of ``mjcf=`` / ``urdf=`` into a ``CompiledModel``."""
    if (mjcf is None) == (urdf is None):
        raise TypeError(
            "log_body_model() requires exactly one of mjcf= or urdf="
        )
    if mjcf is not None:
        _require("mjcf")
        from nebo.extras.robotics.mjcf import compile_mjcf
        bodies, source_format = compile_mjcf(mjcf), "mjcf"
    else:
        _require("urdf")
        from nebo.extras.robotics.urdf import compile_urdf
        bodies, source_format = compile_urdf(urdf), "urdf"

    return CompiledModel(
        glb=build_glb(bodies),
        body_names=tuple(b.name for b in bodies),
        source_format=source_format,
    )
