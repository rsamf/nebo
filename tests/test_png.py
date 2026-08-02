"""Tests for the pure-stdlib PNG encoder and nebo's Pillow-independence.

Pillow is a dev-only dependency: production code must encode PNGs without
it. Tests here use PIL only as a *decoder* to verify the encoder's output,
and `blocked_import("PIL")` to prove the production paths never import it.
"""

from __future__ import annotations

import base64
import io

import numpy as np
import pytest

def _decode(png_bytes: bytes) -> np.ndarray:
    """Decode PNG bytes to an ndarray with PIL (dev dependency)."""
    from PIL import Image

    return np.array(Image.open(io.BytesIO(png_bytes)))


class TestEncodePng:
    """Round-trip every supported layout through a reference decoder."""

    def test_rgb_round_trip(self) -> None:
        from nebo.logging.png import encode_png

        rng = np.random.default_rng(0)
        arr = rng.integers(0, 256, (37, 23, 3), dtype=np.uint8)
        assert np.array_equal(_decode(encode_png(arr)), arr)

    def test_rgba_round_trip(self) -> None:
        from nebo.logging.png import encode_png

        rng = np.random.default_rng(1)
        arr = rng.integers(0, 256, (16, 9, 4), dtype=np.uint8)
        assert np.array_equal(_decode(encode_png(arr)), arr)

    def test_grayscale_2d_round_trip(self) -> None:
        from nebo.logging.png import encode_png

        arr = np.arange(64, dtype=np.uint8).reshape(8, 8)
        assert np.array_equal(_decode(encode_png(arr)), arr)

    def test_grayscale_alpha_round_trip(self) -> None:
        from nebo.logging.png import encode_png

        rng = np.random.default_rng(2)
        arr = rng.integers(0, 256, (12, 7, 2), dtype=np.uint8)
        assert np.array_equal(_decode(encode_png(arr)), arr)

    def test_single_channel_3d_round_trip(self) -> None:
        from nebo.logging.png import encode_png

        arr = np.full((5, 6, 1), 40, dtype=np.uint8)
        assert np.array_equal(_decode(encode_png(arr)), arr.squeeze(2))

    def test_single_pixel(self) -> None:
        from nebo.logging.png import encode_png

        arr = np.array([[[1, 2, 3]]], dtype=np.uint8)
        assert np.array_equal(_decode(encode_png(arr)), arr)

    def test_smooth_gradient_compresses(self) -> None:
        """Scanline filtering must beat storing raw bytes on smooth images."""
        from nebo.logging.png import encode_png

        x = np.linspace(0, 255, 256, dtype=np.uint8)
        arr = np.stack([np.tile(x, (256, 1))] * 3, axis=2)
        assert len(encode_png(arr)) < arr.nbytes / 10

    def test_rejects_non_uint8(self) -> None:
        from nebo.logging.png import encode_png

        with pytest.raises(ValueError):
            encode_png(np.zeros((4, 4, 3), dtype=np.float32))

    def test_rejects_bad_channel_count(self) -> None:
        from nebo.logging.png import encode_png

        with pytest.raises(ValueError):
            encode_png(np.zeros((4, 4, 5), dtype=np.uint8))

    def test_rejects_empty_image(self) -> None:
        from nebo.logging.png import encode_png

        with pytest.raises(ValueError):
            encode_png(np.zeros((0, 0, 3), dtype=np.uint8))


class TestPillowIndependence:
    """The image write path must work with Pillow absent from the env."""

    def test_serialize_uint8_numpy_without_pillow(self, block_import) -> None:
        from nebo.logging.serializers import serialize_image

        rng = np.random.default_rng(3)
        arr = rng.integers(0, 256, (10, 10, 3), dtype=np.uint8)
        with block_import("PIL"):
            png = serialize_image(arr)
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        assert np.array_equal(_decode(png), arr)

    def test_float_chw_normalization_without_pillow(self, block_import) -> None:
        """CHW transpose + float scaling happen in numpy, not PIL."""
        from nebo.logging.serializers import serialize_image

        arr = np.linspace(0, 1, 3 * 4 * 5, dtype=np.float32).reshape(3, 4, 5)
        with block_import("PIL"):
            png = serialize_image(arr)
        expected = (np.transpose(arr, (1, 2, 0)) * 255).clip(0, 255).astype(np.uint8)
        assert np.array_equal(_decode(png), expected)

    def test_grayscale_without_pillow(self, block_import) -> None:
        from nebo.logging.serializers import serialize_image

        arr = np.arange(64, dtype=np.uint8).reshape(8, 8)
        with block_import("PIL"):
            png = serialize_image(arr)
        assert np.array_equal(_decode(png), arr)

    def test_bitmask_labels_without_pillow(self, block_import) -> None:
        from nebo import labels
        from nebo.logging.serializers import _serialize_labels

        mask = np.zeros((8, 8), dtype=np.uint8)
        mask[2:5, 2:5] = 7  # any nonzero binarizes to 255
        with block_import("PIL"):
            out = _serialize_labels(bitmasks=labels.Bitmasks(mask, color="#a78bfa"))
        entry = out["bitmasks"][0]["data"][0]
        assert entry["width"] == 8 and entry["height"] == 8
        decoded = _decode(base64.b64decode(entry["data"]))
        assert np.array_equal(decoded, (mask > 0).astype(np.uint8) * 255)

    def test_bad_type_still_raises_at_call_site_without_pillow(self, block_import) -> None:
        from nebo.logging.serializers import serialize_image

        with block_import("PIL"):
            with pytest.raises(TypeError, match="Cannot serialize"):
                serialize_image("not an image")


class TestPilInputPath:
    """PIL Images stay accepted when the *user* has Pillow installed."""

    def test_palette_mode_converts_and_round_trips(self) -> None:
        from PIL import Image

        from nebo.logging.serializers import serialize_image

        rgb = np.zeros((8, 8, 3), dtype=np.uint8)
        rgb[..., 0] = 200
        img = Image.fromarray(rgb).convert("P", palette=Image.ADAPTIVE)
        decoded = _decode(serialize_image(img))
        assert np.array_equal(decoded, rgb)

    def test_la_mode_keeps_alpha(self) -> None:
        from PIL import Image

        from nebo.logging.serializers import serialize_image

        img = Image.new("LA", (6, 4), color=(80, 130))
        decoded = _decode(serialize_image(img))
        assert decoded.shape == (4, 6, 2)
        assert np.array_equal(decoded, np.array(img))

    def test_pil_input_isolated_from_mutation(self) -> None:
        from PIL import Image

        from nebo.logging.serializers import prepare_image

        img = Image.new("RGB", (4, 4), color=(0, 0, 0))
        pending = prepare_image(img)
        img.paste((255, 255, 255), (0, 0, 4, 4))  # caller reuses the image
        assert _decode(pending.encode()).max() == 0
