"""Example 3: Data Processing Pipeline with Run-level + Per-step Config

Demonstrates two complementary ways to surface configuration:

- A single ``CONFIG`` dict at the top of the file is threaded into every
  step so the run is parameterized from one place. The dict is also
  passed to ``nb.start_run(config=CONFIG)`` so the entire config is
  visible in the run header (top-left ``config`` tab) before any node
  fires.
- Inside each step, ``nb.log_cfg(...)`` records the slice of config the
  step actually used, which lands as chips on that node's card.

Also shown:
- ``@nb.fn()`` to register pipeline steps
- ``nb.log_text()`` for text and tensor-like object logging
- Multiple source nodes and branching DAG
"""

import time
import numpy as np
import nebo as nb


# ─────────────────────────────────────────────────────────────────────────────
# Run-wide config. Every parameter every step takes lives here, so the run is
# fully described by this single dict. The same values get passed into
# ``nb.start_run(config=CONFIG)`` and threaded into the step functions below.
# ─────────────────────────────────────────────────────────────────────────────
CONFIG = {
    "num_samples": 100,
    "noise_level": 0.2,
    "seed": 0,
    "normalize": {
        "method": "standard",
        "clip_min": -5.0,
        "clip_max": 5.0,
    },
    "filter_label": "A",
    "stats": {
        "top_k": 10,
        "threshold": 0.0,
    },
}


nb.md("""
# Data Processing Pipeline

This pipeline generates synthetic data, processes it through normalization
and filtering, then runs statistical analysis. The whole run is parameterized
by a single ``CONFIG`` dict passed to ``nb.start_run(config=...)``; each step
also calls ``nb.log_cfg()`` to record the slice it actually used.
""")


@nb.fn()
def generate_data(num_samples: int, noise_level: float, seed: int) -> np.ndarray:
    """Generate synthetic time-series data with configurable noise level."""
    nb.log_cfg({"num_samples": num_samples, "noise_level": noise_level, "seed": seed})
    np.random.seed(seed)
    t = np.linspace(0, 4 * np.pi, num_samples)
    signal = np.sin(t) + noise_level * np.random.randn(num_samples)
    nb.log_text("status", f"Generated {num_samples} samples with noise_level={noise_level}, seed={seed}")
    nb.log_text("signal", signal)
    time.sleep(0.5)
    return signal


@nb.fn()
def generate_metadata(num_samples: int, seed: int) -> dict:
    """Generate metadata labels for each data sample."""
    nb.log_cfg({"num_samples": num_samples, "seed": seed})
    np.random.seed(seed + 1)
    labels = np.random.choice(["A", "B", "C"], size=num_samples)
    timestamps = np.arange(num_samples, dtype=np.float64)
    metadata = {"labels": labels, "timestamps": timestamps}
    nb.log_text("status", f"Generated metadata for {num_samples} samples")
    nb.log_text("labels", labels)
    time.sleep(0.5)
    return metadata


@nb.fn()
def normalize_data(
    data: np.ndarray,
    method: str,
    clip_min: float,
    clip_max: float,
) -> np.ndarray:
    """Normalize data using the configured method and clip to range."""
    nb.log_cfg({"method": method, "clip_min": clip_min, "clip_max": clip_max})
    if method == "standard":
        mean, std = data.mean(), data.std()
        normalized = (data - mean) / (std + 1e-8)
        nb.log_text("status", f"Standard normalization: mean={mean:.4f}, std={std:.4f}")
    elif method == "minmax":
        dmin, dmax = data.min(), data.max()
        normalized = (data - dmin) / (dmax - dmin + 1e-8)
        nb.log_text("status", f"MinMax normalization: min={dmin:.4f}, max={dmax:.4f}")
    else:
        normalized = data
        nb.log_text("status", f"No normalization (unknown method: {method})")

    clipped = np.clip(normalized, clip_min, clip_max)
    nb.log_text("status", f"Clipped to [{clip_min}, {clip_max}]")
    nb.log_text("normalized", clipped)
    time.sleep(0.5)
    return clipped


@nb.fn()
def filter_by_label(data: np.ndarray, metadata: dict, label: str) -> np.ndarray:
    """Filter data points to only include those matching a specific label."""
    nb.log_cfg({"label": label})
    mask = metadata["labels"] == label
    filtered = data[mask]
    nb.log_text("status", f"Filtered to label='{label}': {mask.sum()}/{len(data)} samples")
    nb.log_text("filtered", filtered)
    time.sleep(0.5)
    return filtered


@nb.fn()
def compute_statistics(
    data: np.ndarray,
    top_k: int,
    threshold: float,
) -> dict:
    """Compute descriptive statistics and find top-K values above threshold."""
    nb.log_cfg({"top_k": top_k, "threshold": threshold})
    stats = {
        "mean": float(data.mean()),
        "std": float(data.std()),
        "min": float(data.min()),
        "max": float(data.max()),
        "median": float(np.median(data)),
        "count": int(len(data)),
        "above_threshold": int(np.sum(data > threshold)),
    }

    # Find top-K values
    if len(data) >= top_k:
        top_indices = np.argsort(np.abs(data))[-top_k:]
        stats["top_k_values"] = data[top_indices].tolist()
    else:
        stats["top_k_values"] = sorted(data.tolist(), key=abs, reverse=True)

    nb.log_text("stats", f"Statistics: mean={stats['mean']:.4f}, std={stats['std']:.4f}, "
                         f"{stats['above_threshold']}/{stats['count']} above threshold={threshold}")

    nb.log_text(
        "stats",
        f"Statistical Summary — "
        f"samples={stats['count']}, "
        f"mean={stats['mean']:.4f}, std={stats['std']:.4f}, "
        f"range=[{stats['min']:.4f}, {stats['max']:.4f}], "
        f"above_threshold({threshold})={stats['above_threshold']}"
    )

    time.sleep(0.5)
    return stats


@nb.fn()
def generate_report(all_stats: dict, filtered_stats: dict) -> str:
    """Generate a final comparison report between full and filtered datasets."""
    report_lines = [
        "=" * 50,
        "ANALYSIS REPORT",
        "=" * 50,
        "",
        f"Full dataset:     mean={all_stats['mean']:.4f}, std={all_stats['std']:.4f}, n={all_stats['count']}",
        f"Filtered (label A): mean={filtered_stats['mean']:.4f}, std={filtered_stats['std']:.4f}, n={filtered_stats['count']}",
        "",
        f"Difference in mean: {abs(all_stats['mean'] - filtered_stats['mean']):.4f}",
        f"Top-K values (full): {[f'{v:.2f}' for v in all_stats['top_k_values'][:3]]}",
        f"Top-K values (filtered): {[f'{v:.2f}' for v in filtered_stats['top_k_values'][:3]]}",
    ]
    report = "\n".join(report_lines)
    nb.log_text("report", report)
    time.sleep(0.5)
    return report


@nb.fn()
def run_analysis(config: dict) -> str:
    """Top-level analysis runner that orchestrates data generation, processing, and reporting.

    This is the source node. Calling other @fn functions from here creates
    DAG edges automatically: run_analysis -> generate_data, run_analysis -> normalize_data, etc.
    """
    # Data generation
    data = generate_data(
        num_samples=config["num_samples"],
        noise_level=config["noise_level"],
        seed=config["seed"],
    )
    metadata = generate_metadata(
        num_samples=config["num_samples"],
        seed=config["seed"],
    )

    # Processing
    normalized = normalize_data(data, **config["normalize"])

    # Filtering
    filtered = filter_by_label(normalized, metadata, label=config["filter_label"])

    # Analysis — two calls to compute_statistics
    all_stats = compute_statistics(normalized, **config["stats"])
    filtered_stats = compute_statistics(filtered, **config["stats"])

    # Final report
    return generate_report(all_stats, filtered_stats)


def main():
    """Run the data processing pipeline."""
    # Pass the run-wide config to nb.start_run so it shows up in the UI's
    # config tab alongside the workflow description.
    with nb.start_run(config=CONFIG):
        report = run_analysis(CONFIG)
        print(report)


if __name__ == "__main__":
    main()
