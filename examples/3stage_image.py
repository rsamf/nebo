"""3-Stage Image Processing Pipeline

Processes multiple images through 3 stages: Create, Warm Tint, Sharpen,
then plots per-image color/contrast stats as a labeled scatter so each
point can be clicked to jump the rest of the UI to that image's step.

Stages per image:
  1. Create — Generate an image with per-index color/texture variation
  2. Warm Tint — Boost reds, reduce blues
  3. Sharpen — Sharpen + contrast boost
  4. Analyze — Emit one accumulating scatter point per stage per image
"""

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter
import nebo as nb


def _make_image(index: int, size: int = 96) -> Image.Image:
    """Generate a base image with strong per-index variation.

    Each image's per-channel mean is sampled from [40, 220], so analysis
    stats spread across a wide range rather than collapsing to a tight
    cluster. Texture is added via per-channel sinusoids at random
    frequency / phase / direction, giving distinct contrast too.
    """
    rng = np.random.default_rng(index)
    base = rng.integers(40, 220, size=3).astype(np.float32)
    freq = rng.uniform(0.5, 6.0, size=3)
    phase = rng.uniform(0, 2 * np.pi, size=3)
    direction = rng.uniform(0, 2 * np.pi, size=3)
    amp = rng.uniform(15, 45, size=3)

    yy, xx = np.meshgrid(np.arange(size), np.arange(size), indexing='ij')
    arr = np.empty((size, size, 3), dtype=np.float32)
    for c in range(3):
        coord = np.cos(direction[c]) * xx + np.sin(direction[c]) * yy
        arr[:, :, c] = base[c] + amp[c] * np.sin(2 * np.pi * freq[c] * coord / size + phase[c])
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


@nb.fn(ui={"default_tab": "images"})
def create_images(num_images: int = 200, size: int = 96) -> list[Image.Image]:
    """Generate base images with per-index color/texture variation."""
    images = []
    for i in nb.track(range(num_images)):
        img = _make_image(i, size)
        nb.log_text("status", f"Created image {i}", step=i)
        nb.log_image(img, name="created", step=i)
        images.append(img)
    return images


@nb.fn(ui={"default_tab": "images"})
def warm_tint(images: list[Image.Image]) -> list[Image.Image]:
    """Apply a warm tint: boost reds, reduce blues."""
    result = []
    for i, img in enumerate(images):
        arr = np.array(img, dtype=np.float32)
        arr[:, :, 0] = np.clip(arr[:, :, 0] * 1.3, 0, 255)
        arr[:, :, 2] = np.clip(arr[:, :, 2] * 0.6, 0, 255)
        out = Image.fromarray(arr.astype(np.uint8))
        nb.log_text("status", f"Applied warm tint to image {i}", step=i)
        nb.log_image(out, name="warm_tint", step=i)
        result.append(out)
    return result


@nb.fn(ui={"default_tab": "images"})
def sharpen(images: list[Image.Image]) -> list[Image.Image]:
    """Sharpen and boost contrast."""
    result = []
    for i, img in enumerate(images):
        sharpened = img.filter(ImageFilter.SHARPEN)
        out = ImageEnhance.Contrast(sharpened).enhance(1.8)
        nb.log_text("status", f"Sharpened image {i}", step=i)
        nb.log_image(out, name="sharpened", step=i)
        result.append(out)
    return result


def _stats(arr: np.ndarray) -> tuple[float, float]:
    """Return (avg red, contrast) for a HxWx3 float array."""
    return float(arr[:, :, 0].mean()), float(arr.std())


@nb.fn(ui={"default_tab": "metrics"})
def analyze(
    originals: list[Image.Image],
    warm: list[Image.Image],
    sharp: list[Image.Image],
) -> None:
    """Plot per-image color stats as an accumulating scatter.

    Each iteration calls ``log_scatter`` once with three labels — one
    point each for the original, warm-tinted, and sharpened versions of
    the same image. Calls accumulate (this is the new scatter behavior)
    and ``step`` auto-increments to match the per-image step used by
    upstream stages.

    The three labels form three visually distinct clusters in
    (avg_red, contrast) space:
      * original — spread across the full (red, contrast) range
      * warm — same images shifted right (reds boosted)
      * sharp — same images shifted up (contrast boosted)

    Click any point in the UI to filter the text/images panels to that
    exact image.
    """
    for i, (o, w, s) in enumerate(zip(originals, warm, sharp)):
        o_arr = np.array(o, dtype=np.float32)
        w_arr = np.array(w, dtype=np.float32)
        s_arr = np.array(s, dtype=np.float32)
        nb.log_scatter(
            "red_vs_contrast",
            {
                "original": [_stats(o_arr)],
                "warm": [_stats(w_arr)],
                "sharp": [_stats(s_arr)],
            },
            colors=True,
        )
        nb.log_text("status", f"Analyzed image {i}", step=i)


def run_pipeline() -> list[Image.Image]:
    """Run all 3 stages across multiple images, then analyze."""
    images = create_images()
    warm = warm_tint(images)
    sharp = sharpen(warm)
    analyze(images, warm, sharp)
    return sharp


def main():
    nb.md("# 3-Stage Image Pipeline\nCreate → Warm Tint → Sharpen → Analyze")

    result = run_pipeline()
    print(f"Processed {len(result)} images through 3 stages")


if __name__ == "__main__":
    main()
