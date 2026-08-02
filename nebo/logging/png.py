"""Pure-stdlib PNG encoder (numpy + zlib) — nebo's replacement for Pillow
on the image write path.

nebo only ever encodes 8-bit L/LA/RGB/RGBA arrays, so a full imaging
library is overkill: a PNG is a signature plus three chunks, with zlib
doing the heavy lifting in C. Scanlines are filtered per row (adaptive
over None/Sub/Up via the standard minimum-sum-of-absolute-differences
heuristic) so compressed sizes land near Pillow's on typical images.
"""

from __future__ import annotations

import struct
import zlib

import numpy as np

_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# channels -> PNG color type: L=0, LA=4, RGB=2, RGBA=6
_COLOR_TYPES = {1: 0, 2: 4, 3: 2, 4: 6}


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data))
    )


def _filter_scanlines(raw: np.ndarray, bpp: int) -> np.ndarray:
    """Prepend each row's filter byte, choosing None/Sub/Up per row.

    uint8 wraparound is exactly the mod-256 arithmetic the PNG filter
    spec calls for. Rows are scored by the sum of their bytes read as
    signed values — smaller means more compressible.
    """
    height, stride = raw.shape
    left = np.zeros_like(raw)
    left[:, bpp:] = raw[:, :-bpp]
    above = np.zeros_like(raw)
    above[1:] = raw[:-1]
    # Index in this stack == the PNG filter-type byte (0/1/2).
    candidates = np.stack([raw, raw - left, raw - above])
    scores = np.abs(candidates.view(np.int8).astype(np.int16)).sum(axis=2)
    choice = scores.argmin(axis=0)
    out = np.empty((height, stride + 1), dtype=np.uint8)
    out[:, 0] = choice
    out[:, 1:] = candidates[choice, np.arange(height)]
    return out


def encode_png(arr: np.ndarray) -> bytes:
    """Encode a uint8 ndarray as PNG bytes.

    Accepts ``(H, W)`` grayscale or ``(H, W, C)`` with C in {1, 2, 3, 4}
    (L / LA / RGB / RGBA). Raises ValueError for anything else.
    """
    if arr.dtype != np.uint8:
        raise ValueError(f"encode_png needs uint8 data, got {arr.dtype}")
    if arr.ndim == 2:
        arr = arr[:, :, None]
    if arr.ndim != 3 or arr.shape[2] not in _COLOR_TYPES:
        raise ValueError(f"encode_png cannot handle shape {arr.shape}")
    height, width, channels = arr.shape
    if height == 0 or width == 0:
        raise ValueError("encode_png cannot handle empty images")

    raw = np.ascontiguousarray(arr).reshape(height, width * channels)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, _COLOR_TYPES[channels], 0, 0, 0)
    idat = zlib.compress(_filter_scanlines(raw, channels).tobytes(), 6)
    return (
        _SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )
