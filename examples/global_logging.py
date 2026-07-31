"""Example: Global logging — nb.log_* outside any @nb.fn() context.

Calls made outside a decorated function land on the Global loggable, which
appears as its own card at the top of the flat view (never in the DAG).
This example emits text streams, metrics, and an image at module /
main-function scope so you can see each tab populated under the Global card.
"""

import math
import numpy as np
from PIL import Image

import nebo as nb


def main() -> None:
    nb.md(
        "# Global logging demo\n\n"
        "This run emits text streams, metrics, and an image from outside any "
        "`@nb.fn()` function. They all land on the Global loggable."
    )
    nb.ui(view="flat")
    # ── Text streams at the Global level ─────────────────────────────────────
    nb.log_text("status", "pipeline starting")
    nb.log_text("status", "loading config from env")
    nb.log_text("status", "env looks healthy — proceeding")

    # ── Scalar metrics at the Global level ────────────────────────────────────
    # Auto-step counter, one series per name.
    for step in range(50):
        nb.log_line("global_heartbeat", math.sin(step / 5.0))
        nb.log_line("global_cost", 1.0 / (1 + step))

    # ── Multi-line text snippet on the Global loggable ───────────────────────
    nb.log_text("env_report", "env report:\n- python: OK\n- gpu: absent (CPU path)\n- cache: warm")

    # ── Image on the Global loggable ──────────────────────────────────────────
    xx, yy = np.meshgrid(np.linspace(-3, 3, 128), np.linspace(-3, 3, 128))
    z = np.exp(-(xx ** 2 + yy ** 2) / 2.0)
    rgb = np.stack(
        [(z * 255).astype(np.uint8)] * 3,
        axis=-1,
    )
    nb.log_image(Image.fromarray(rgb), name="warmup_heatmap")

    values = [float(i) * 0.1 for i in range(32)]
    for i,v in enumerate(values):
        nb.log_line("random values", v, step=i)


if __name__ == "__main__":
    main()
