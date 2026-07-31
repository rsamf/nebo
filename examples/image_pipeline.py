"""Example 6: Image Processing Pipeline with log_image()

Demonstrates:
- @nb.fn() for registering pipeline steps
- nb.log_image(img, step=i) for logging images with step indices
- nb.log_cfg() for logging transform configuration
- nb.log_line() for tracking image statistics
- Processing a batch of 10 images through a series of transforms
"""

import numpy as np
from PIL import Image, ImageFilter, ImageEnhance
import nebo as nb


nb.md("""
# Image Processing Pipeline

Generates 10 synthetic images and processes each through a series of
transforms: grayscale conversion, Gaussian blur, contrast enhancement,
and edge detection. Each stage logs its output images with `nb.log_image()`.
""")


def _make_synthetic_image(index: int, size: int = 128) -> Image.Image:
    """Generate a colorful synthetic image based on the index."""
    arr = np.zeros((size, size, 3), dtype=np.uint8)
    freq = 1 + index * 0.5
    for y in range(size):
        for x in range(size):
            arr[y, x, 0] = int(127 + 127 * np.sin(2 * np.pi * freq * x / size))
            arr[y, x, 1] = int(127 + 127 * np.sin(2 * np.pi * freq * y / size + index))
            arr[y, x, 2] = int(127 + 127 * np.cos(2 * np.pi * freq * (x + y) / size))
    return Image.fromarray(arr)


@nb.fn(ui={"default_tab": "images"})
def generate_images(num_images: int = 10, size: int = 128) -> list[Image.Image]:
    """Generate a batch of synthetic test images."""
    nb.log_cfg({"num_images": num_images, "size": size})
    images = []
    for i in range(num_images):
        img = _make_synthetic_image(i, size)
        nb.log_image(img, name="generated", step=i)
        images.append(img)
    nb.log_text("status", f"Generated {num_images} images at {size}x{size}")
    return images


@nb.fn(ui={"default_tab": "images"})
def to_grayscale(images: list[Image.Image]) -> list[Image.Image]:
    """Convert all images to grayscale."""
    nb.log_cfg({"mode": "L"})
    result = []
    for i, img in enumerate(images):
        gray = img.convert("L").convert("RGB")
        nb.log_image(gray, name="grayscale", step=i)
        result.append(gray)
    nb.log_text("status", f"Converted {len(images)} images to grayscale")
    return result


@nb.fn(ui={"default_tab": "images"})
def apply_blur(images: list[Image.Image], radius: float = 2.0) -> list[Image.Image]:
    """Apply Gaussian blur to each image."""
    nb.log_cfg({"blur_radius": radius})
    result = []
    for i, img in enumerate(images):
        blurred = img.filter(ImageFilter.GaussianBlur(radius=radius))
        nb.log_image(blurred, name="blurred", step=i)
        result.append(blurred)
    nb.log_text("status", f"Applied Gaussian blur (radius={radius}) to {len(images)} images")
    return result


@nb.fn(ui={"default_tab": "images"})
def enhance_contrast(images: list[Image.Image], factor: float = 1.8) -> list[Image.Image]:
    """Enhance contrast of each image."""
    nb.log_cfg({"contrast_factor": factor})
    result = []
    for i, img in enumerate(images):
        enhanced = ImageEnhance.Contrast(img).enhance(factor)
        nb.log_image(enhanced, name="contrast_enhanced", step=i)
        result.append(enhanced)
    nb.log_text("status", f"Enhanced contrast (factor={factor}) on {len(images)} images")
    return result


@nb.fn(ui={"default_tab": "images"})
def detect_edges(images: list[Image.Image]) -> list[Image.Image]:
    """Detect edges in each image using a Laplacian filter."""
    nb.log_cfg({"method": "FIND_EDGES"})
    result = []
    for i, img in enumerate(images):
        edges = img.filter(ImageFilter.FIND_EDGES)

        arr = np.array(edges)
        h = arr.shape[0]
        w = arr.shape[1]
        cx, cy = w // 2, h // 2
        nb.log_image(
            edges,
            name="edges",
            step=i,
            boxes=nb.labels.Boxes(
                [[cx - w // 4, cy - h // 4, cx + w // 4, cy + h // 4]],
                color="#22d3ee",
            ),
            points=nb.labels.Points([[cx, cy]], color="#facc15"),
        )

        mean_intensity = float(arr.mean())
        nb.log_line("edge_intensity", mean_intensity, step=i)
        result.append(edges)
    nb.log_text("status", f"Detected edges in {len(images)} images")
    return result


@nb.fn(ui={"default_tab": "metrics"})
def compute_stats(images: list[Image.Image]) -> dict:
    """Compute per-image brightness statistics."""
    stats = []
    for i, img in enumerate(images):
        arr = np.array(img).astype(np.float32)
        brightness = float(arr.mean())
        nb.log_line("brightness", brightness, step=i)
        stats.append({"index": i, "brightness": brightness})
    avg = sum(s["brightness"] for s in stats) / len(stats)
    nb.log_text("status", f"Average brightness across {len(images)} images: {avg:.1f}")
    return {"per_image": stats, "average_brightness": avg}


def run_pipeline() -> dict:
    """Run the full image processing pipeline."""
    images = generate_images()
    gray = to_grayscale(images)
    blurred = apply_blur(gray)
    enhanced = enhance_contrast(blurred)
    edges = detect_edges(enhanced)
    stats = compute_stats(edges)
    return stats


def main():
    nb.ui(tracker="step")
    result = run_pipeline()
    print(f"\nProcessed {len(result['per_image'])} images")
    print(f"Average edge brightness: {result['average_brightness']:.1f}")


if __name__ == "__main__":
    main()
