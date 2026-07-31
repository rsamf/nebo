import numpy as np
import nebo as nb

nb.md("# Data Processing Pipeline\nGenerate, normalize, filter, and analyze data.")


@nb.fn()
def generate(num_samples: int = 200, noise: float = 0.1, seed: int = 42):
    """Generate synthetic signal data."""
    nb.log_cfg({"num_samples": num_samples, "noise": noise, "seed": seed})
    rng = np.random.default_rng(seed)
    t = np.linspace(0, 4 * np.pi, num_samples)
    signal = np.sin(t) + noise * rng.standard_normal(num_samples)
    nb.log_text("status", f"Generated {num_samples} samples")
    return signal


@nb.fn()
def normalize(data, method: str = "standard", clip_min: float = -3.0, clip_max: float = 3.0):
    """Normalize and clip the signal."""
    nb.log_cfg({"method": method, "clip_min": clip_min, "clip_max": clip_max})
    if method == "standard":
        data = (data - data.mean()) / (data.std() + 1e-8)
    data = np.clip(data, clip_min, clip_max)
    nb.log_text("status", f"Normalized with method={method}")
    return data


@nb.fn()
def analyze(data):
    """Compute statistics on the processed data."""
    nb.log_text("stats", f"Stats: mean={data.mean():.4f}, std={data.std():.4f}")
    nb.log_line("mean", float(data.mean()))
    nb.log_line("std", float(data.std()))
    return {"mean": float(data.mean()), "std": float(data.std()), "n": len(data)}


def run():
    data = generate()
    normed = normalize(data)
    return analyze(normed)


run()
