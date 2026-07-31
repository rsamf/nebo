"""Unit tests for nebo.logging.serializers label normalizers."""
from __future__ import annotations

import numpy as np
import pytest

from nebo.logging.serializers import (
    _normalize_points,
    _normalize_boxes,
    _normalize_circles,
    _normalize_polygons,
    _normalize_bitmask,
)


def test_normalize_points_single_point_wraps():
    assert _normalize_points([45, 80]) == [[45, 80]]


def test_normalize_points_list_of_points_untouched():
    assert _normalize_points([[45, 80], [55, 60]]) == [[45, 80], [55, 60]]


def test_normalize_points_accepts_ndarray():
    arr = np.array([[1, 2], [3, 4]])
    assert _normalize_points(arr) == [[1, 2], [3, 4]]


def test_normalize_boxes_single_wraps():
    assert _normalize_boxes([10, 10, 50, 50]) == [[10, 10, 50, 50]]


def test_normalize_boxes_list_untouched():
    assert _normalize_boxes([[10, 10, 50, 50], [60, 60, 100, 100]]) == [
        [10, 10, 50, 50],
        [60, 60, 100, 100],
    ]


def test_normalize_circles_single_wraps():
    assert _normalize_circles([30, 30, 10]) == [[30, 30, 10]]


def test_normalize_circles_list_untouched():
    assert _normalize_circles([[30, 30, 10], [50, 50, 5]]) == [
        [30, 30, 10],
        [50, 50, 5],
    ]


def test_normalize_polygons_single_polygon_wraps():
    # One polygon with three points.
    assert _normalize_polygons([[5, 5], [20, 5], [20, 20]]) == [
        [[5, 5], [20, 5], [20, 20]]
    ]


def test_normalize_polygons_list_of_polygons_untouched():
    value = [[[5, 5], [20, 5], [20, 20]], [[0, 0], [1, 0], [1, 1]]]
    assert _normalize_polygons(value) == value


def test_normalize_bitmask_single_2d_wraps():
    mask = np.zeros((8, 8), dtype=np.uint8)
    mask[2:5, 2:5] = 1
    out = _normalize_bitmask(mask)
    assert len(out) == 1
    assert out[0].shape == (8, 8)


def test_normalize_bitmask_3d_splits():
    masks = np.zeros((3, 8, 8), dtype=np.uint8)
    out = _normalize_bitmask(masks)
    assert len(out) == 3
    assert all(m.shape == (8, 8) for m in out)


def test_normalize_bitmask_list_of_2d_untouched():
    m1 = np.zeros((4, 4), dtype=np.uint8)
    m2 = np.ones((4, 4), dtype=np.uint8)
    out = _normalize_bitmask([m1, m2])
    assert len(out) == 2


class TestPendingMedia:
    """Deferred media encoding: copy+validate at call site, encode off-thread."""

    def test_prepare_image_encodes_same_png(self):
        import numpy as np
        from nebo.logging.serializers import (
            PendingMedia,
            prepare_image,
            serialize_image,
        )

        arr = (np.arange(300, dtype=np.uint8).reshape(10, 10, 3))
        pending = prepare_image(arr)
        assert isinstance(pending, PendingMedia)
        assert pending.kind == "image"
        assert pending.encode() == serialize_image(arr)
        # encode() is idempotent (cached).
        assert pending.encode() is pending.encode()

    def test_prepare_image_isolated_from_mutation(self):
        import numpy as np
        from nebo.logging.serializers import prepare_image

        arr = np.zeros((8, 8, 3), dtype=np.uint8)
        pending = prepare_image(arr)
        arr[:] = 255  # caller reuses the buffer
        from PIL import Image
        import io

        decoded = np.asarray(Image.open(io.BytesIO(pending.encode())))
        assert decoded.max() == 0  # original pixels, not the mutation

    def test_prepare_image_rejects_bad_type_at_call_site(self):
        import pytest as _pytest

        from nebo.logging.serializers import prepare_image

        with _pytest.raises(TypeError):
            prepare_image([[1, 2], [3, 4]])

    def test_prepare_audio_encodes_same_wav(self):
        import numpy as np
        from nebo.logging.serializers import prepare_audio, serialize_audio

        audio = np.linspace(-1, 1, 160, dtype=np.float32)
        pending = prepare_audio(audio, 16000)
        assert pending.kind == "audio"
        assert pending.encode() == serialize_audio(audio, 16000)

    def test_resolve_media_replaces_pending(self):
        import numpy as np
        from nebo.logging.serializers import prepare_image, resolve_media

        pending = prepare_image(np.zeros((4, 4, 3), dtype=np.uint8))
        event = {"type": "image", "data": pending}
        out = resolve_media(event)
        assert isinstance(out["data"], bytes)
        assert out["data"].startswith(b"\x89PNG")
        # Non-media events pass through untouched.
        plain = {"type": "text", "message": "x"}
        assert resolve_media(plain) is plain
