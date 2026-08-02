"""Serializers for various data types (images, audio, tensors)."""

from __future__ import annotations

import io
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class PendingMedia:
    """A validated, isolated copy of media input whose byte encoding is
    deferred to the transport's background thread.

    The expensive part of `nb.log_image` (PNG compression, ~300 ms for a
    1080p frame) used to run on the caller's thread. `prepare_image` /
    `prepare_audio` now do only the cheap parts synchronously — type
    validation (so TypeError still raises at the call site) and a
    defensive copy (so callers can reuse their buffers) — and `encode()`
    runs in the transport flush loop via `resolve_media`.
    """

    __slots__ = ("kind", "_payload", "_sr", "_encoded")

    def __init__(self, kind: str, payload: Any, sr: int | None = None) -> None:
        self.kind = kind
        self._payload = payload  # uint8 ndarray (image) or int16 ndarray (audio)
        self._sr = sr
        self._encoded: bytes | None = None

    def encode(self) -> bytes:
        """Encode to PNG/WAV bytes. Idempotent; frees the copy after."""
        if self._encoded is None:
            if self.kind == "image":
                from nebo.logging.png import encode_png

                self._encoded = encode_png(self._payload)
            else:
                self._encoded = _wav_bytes(self._payload, self._sr or 16000)
            self._payload = None
        return self._encoded

    @property
    def nbytes(self) -> int:
        """Rough in-memory size, for transport buffer accounting."""
        if self._encoded is not None:
            return len(self._encoded)
        n = getattr(self._payload, "nbytes", None)
        return int(n) if n is not None else 0


def prepare_image(image: Any) -> PendingMedia:
    """Validate + copy an image input; PNG encoding is deferred.

    Supports: PIL.Image, numpy array, torch tensor. Pillow is not a nebo
    dependency — it is only imported when *image* is a PIL Image, in
    which case the caller's environment has it by definition.

    Raises:
        TypeError: If *image* is not a supported type (at the call site,
        exactly like the old eager path). Conversion errors from broken
        arrays also raise here — only the PNG compression is deferred.
    """
    # PIL Image (optional peer dependency, guarded exactly like torch)
    try:
        from PIL import Image as _PILImage
    except ImportError:
        _PILImage = None

    if _PILImage is not None and isinstance(image, _PILImage.Image):
        return PendingMedia("image", _pil_to_array(image))

    # Torch tensor
    try:
        import torch
    except ImportError:
        torch = None

    if torch is not None and isinstance(image, torch.Tensor):
        return PendingMedia("image", _normalize_array(image.detach().cpu().numpy()))

    # Numpy array
    try:
        import numpy as np
    except ImportError:
        np = None

    if np is not None and isinstance(image, np.ndarray):
        return PendingMedia("image", _normalize_array(image))

    raise TypeError(f"Cannot serialize image of type {type(image).__name__}")


def serialize_image(image: Any) -> bytes:
    """Eagerly serialize an image to PNG bytes (validate + copy + encode)."""
    return prepare_image(image).encode()


def _pil_to_array(img: Any) -> Any:
    """Convert a PIL image to an owned uint8 ndarray (L/LA/RGB/RGBA).

    Modes the PNG encoder can't take directly are converted with the
    caller's own Pillow install; alpha survives, everything else lands
    in RGB.
    """
    import numpy as np

    if img.mode not in ("L", "LA", "RGB", "RGBA"):
        has_alpha = (
            img.mode in ("RGBa", "La", "PA")
            or (img.mode == "P" and "transparency" in img.info)
        )
        img = img.convert("RGBA" if has_alpha else "RGB")
    # np.array copies, so the caller can keep mutating its image.
    return np.array(img, dtype=np.uint8)


def _normalize_array(arr: Any) -> Any:
    """Normalize a numpy array to owned uint8 pixels for the PNG encoder."""
    import numpy as np

    if arr.ndim == 3 and arr.shape[0] in (1, 3, 4):
        # CHW -> HWC
        arr = np.transpose(arr, (1, 2, 0))

    if arr.dtype == np.float32 or arr.dtype == np.float64:
        arr = (arr * 255).clip(0, 255).astype(np.uint8)

    if arr.ndim == 3 and arr.shape[2] == 1:
        arr = arr.squeeze(2)

    if (
        arr.ndim not in (2, 3)
        or (arr.ndim == 3 and arr.shape[2] not in (2, 3, 4))
        or arr.size == 0
    ):
        raise TypeError(f"Cannot serialize image array of shape {arr.shape}")

    # Explicit copy: transposes/squeezes above are views, and the caller
    # may mutate its buffer after nb.log_image returns.
    return np.array(arr, dtype=np.uint8, copy=True)


def prepare_audio(audio: Any, sr: int = 16000) -> PendingMedia:
    """Validate + copy audio input; WAV encoding is deferred."""
    import numpy as np

    if not isinstance(audio, np.ndarray):
        audio = np.array(audio, dtype=np.float32)

    if audio.dtype in (np.float32, np.float64):
        audio = (audio * 32767).clip(-32768, 32767).astype(np.int16)
    else:
        audio = np.array(audio, dtype=np.int16, copy=True)

    return PendingMedia("audio", audio, sr=sr)


def serialize_audio(audio: Any, sr: int = 16000) -> bytes:
    """Eagerly serialize audio to WAV bytes (validate + copy + encode)."""
    return prepare_audio(audio, sr).encode()


def _wav_bytes(audio: Any, sr: int) -> bytes:
    """Write an int16 ndarray as WAV bytes."""
    import wave

    buf = io.BytesIO()
    channels = 1 if audio.ndim == 1 else audio.shape[1]
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sr)
        wf.writeframes(audio.tobytes())
    return buf.getvalue()


def resolve_media(event: dict) -> Optional[dict]:
    """Encode a deferred-media event in place of its PendingMedia payload.

    Called by the transports' flush loops (background threads). Returns
    the event unchanged when there is nothing to resolve; returns None
    (drop) when encoding fails — background threads can't raise to the
    user, so the failure is logged instead.
    """
    data = event.get("data")
    if not isinstance(data, PendingMedia):
        return event
    try:
        return {**event, "data": data.encode()}
    except Exception:
        logger.warning(
            "nebo: dropping %s event %r — media encoding failed",
            event.get("type"), event.get("name"), exc_info=True,
        )
        return None


def _to_list(value: Any) -> Any:
    """Convert tensor/ndarray to nested Python lists; pass lists through."""
    if hasattr(value, "tolist"):
        return value.tolist()
    return value


def _normalize_points(value: Any) -> list[list[float]]:
    v = _to_list(value)
    if not isinstance(v, list) or len(v) == 0:
        return []
    if not isinstance(v[0], (list, tuple)):
        return [list(v)]
    return [list(p) for p in v]


def _normalize_boxes(value: Any) -> list[list[float]]:
    v = _to_list(value)
    if not isinstance(v, list) or len(v) == 0:
        return []
    if not isinstance(v[0], (list, tuple)):
        return [list(v)]
    return [list(b) for b in v]


def _normalize_circles(value: Any) -> list[list[float]]:
    v = _to_list(value)
    if not isinstance(v, list) or len(v) == 0:
        return []
    if not isinstance(v[0], (list, tuple)):
        return [list(v)]
    return [list(c) for c in v]


def _normalize_polygons(value: Any) -> list[list[list[float]]]:
    v = _to_list(value)
    if not isinstance(v, list) or len(v) == 0:
        return []
    first = v[0]
    if (
        isinstance(first, (list, tuple))
        and len(first) > 0
        and not isinstance(first[0], (list, tuple))
    ):
        # Single polygon (outer is a flat list of points).
        return [[list(p) for p in v]]
    return [[list(p) for p in poly] for poly in v]


def _normalize_bitmask(value: Any) -> list:
    """Return a list of 2D mask objects (original dtype preserved)."""
    if isinstance(value, list):
        return list(value)
    if hasattr(value, "shape"):
        if len(value.shape) == 2:
            return [value]
        if len(value.shape) == 3:
            return [value[i] for i in range(value.shape[0])]
    return [value]


_NORMALIZERS = {
    "Points": _normalize_points,
    "Boxes": _normalize_boxes,
    "Circles": _normalize_circles,
    "Polygons": _normalize_polygons,
}


def _coerce_groups(value: Any, expected_cls_name: str, kwarg_name: str) -> list:
    """Validate the user passed a label dataclass (or list of them).

    Returns a list of the matching dataclass instances. Raises TypeError
    on raw lists/tensors so users get a clear pointer at nb.labels.X
    instead of a downstream serialization error.
    """
    from nebo import labels as _labels

    expected_cls = getattr(_labels, expected_cls_name)
    if isinstance(value, expected_cls):
        return [value]
    if isinstance(value, list) and all(isinstance(g, expected_cls) for g in value):
        return list(value)
    raise TypeError(
        f"nb.log_image(..., {kwarg_name}=...) expects nb.labels.{expected_cls_name} "
        f"(or list[nb.labels.{expected_cls_name}]); got {type(value).__name__}. "
        f"Wrap your data: {kwarg_name}=nb.labels.{expected_cls_name}(data, color=\"#hex\")."
    )


def _encode_bitmask_group(bitmasks_obj: Any) -> dict:
    """Encode a Bitmasks(data=..., color=...) instance to wire form."""
    import base64
    import numpy as np

    from nebo.logging.png import encode_png

    masks = []
    for m in _normalize_bitmask(bitmasks_obj.data):
        arr = np.asarray(m)
        # Binarize: any nonzero → 255. Works for bool, uint8, float, etc.
        binary = (arr > 0).astype(np.uint8) * 255
        masks.append({
            "width": int(binary.shape[1]),
            "height": int(binary.shape[0]),
            "data": base64.b64encode(encode_png(binary)).decode("ascii"),
        })
    return {"data": masks, "color": bitmasks_obj.color}


def _serialize_labels(
    *, points=None, boxes=None, circles=None, polygons=None, bitmasks=None,
) -> dict:
    """Build the wire-event `labels` dict from label dataclass instances.

    Each kwarg accepts either a single dataclass instance from
    nb.labels.* or a list of them, so an image can carry multiple
    groups of the same kind in different colors. Geometric coords are
    normalized to plain nested lists; bitmasks are binarized + PNG +
    base64'd. Each group serializes to ``{"data": <normalized>,
    "color": <css>}``. Missing (None) kwargs are omitted.
    """
    out: dict = {}

    geometric = (
        ("points", points, "Points"),
        ("boxes", boxes, "Boxes"),
        ("circles", circles, "Circles"),
        ("polygons", polygons, "Polygons"),
    )
    for key, value, cls_name in geometric:
        if value is None:
            continue
        groups = _coerce_groups(value, cls_name, key)
        if cls_name == "Polygons":
            # Polygons carry an extra `fill` flag so the UI can stroke
            # the outline only (fill=False) instead of filling the
            # interior. Other label kinds don't have an analogue.
            out[key] = [
                {"data": _NORMALIZERS[cls_name](g.data), "color": g.color, "fill": bool(g.fill)}
                for g in groups
            ]
        else:
            out[key] = [
                {"data": _NORMALIZERS[cls_name](g.data), "color": g.color}
                for g in groups
            ]

    if bitmasks is not None:
        groups = _coerce_groups(bitmasks, "Bitmasks", "bitmasks")
        out["bitmasks"] = [_encode_bitmask_group(g) for g in groups]

    return out
