"""Night-sky source extraction: real star detection + photometry with nebo.

Run:  python pipeline.py                # one run at k=4 sigma (group "night-sky")
      python pipeline.py --k 3
      python pipeline.py --populate     # k in {3,4,5} -> night-sky/threshold-study
      python pipeline.py --smoke        # 2 tiles only
"""
from __future__ import annotations

import argparse
import glob
import math
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

import nebo as nb

DATA_DIR = Path(__file__).parent / "data"

nb.md("""
# Night-sky source extraction

A real astronomy pipeline over DSS2 survey tiles: background estimation,
threshold detection, photometry, and a catalog — with every detection
annotated on the tile (points, circles, boxes, polygons, bitmask).
""")
nb.ui(view="dag", layout="horizontal", minimap=True, tracker="step")

# Module-level accumulators (cleared per run). build_catalog reads CATALOG
# via depends_on rather than an argument — the declared-edge demo.
CATALOG: list[dict] = []
MAG_HIST: dict[str, list[float]] = {}


def source_outline(mask: np.ndarray, cy: float, cx: float, offy: float, offx: float, n: int = 12) -> list:
    """Coarse n-gon outline of a labeled blob, in full-image coordinates."""
    ys, xs = np.nonzero(mask)
    ang = np.arctan2(ys - cy, xs - cx)
    rad = np.hypot(ys - cy, xs - cx)
    verts = []
    for a in np.linspace(-np.pi, np.pi, n, endpoint=False):
        d = np.abs(((ang - a + np.pi) % (2 * np.pi)) - np.pi)
        sel = d < (np.pi / n)
        r = float(rad[sel].max()) if sel.any() else float(rad.max())
        verts.append([float(offx + cx + (r + 1.5) * np.cos(a)),
                      float(offy + cy + (r + 1.5) * np.sin(a))])
    return verts


@nb.fn()
def load_tile(path: str, step: int) -> np.ndarray:
    """Load one survey tile as grayscale float."""
    img = np.asarray(Image.open(path).convert("L"), dtype=np.float64)
    nb.log_text("load/status", f"{Path(path).name}: {img.shape[1]}x{img.shape[0]} px", step=step)
    return img


@nb.fn()
def estimate_background(img: np.ndarray, step: int):
    """Median-filter sky background + robust noise estimate (MAD)."""
    bg = ndimage.median_filter(img, size=31)
    resid = img - bg
    sigma = float(1.4826 * np.median(np.abs(resid - np.median(resid))))
    nb.log_line("background/sigma", sigma, step=step)
    return bg, max(sigma, 0.5)


@nb.fn()
def detect_sources(img: np.ndarray, bg: np.ndarray, sigma: float, k: float,
                   tile_name: str, step: int):
    """Label pixels above k*sigma; classify point / extended / saturated."""
    resid = img - bg
    mask = ndimage.binary_opening(resid > k * sigma)
    lbl, _n = ndimage.label(mask)
    sources = []
    for i, sl in enumerate(ndimage.find_objects(lbl), start=1):
        if sl is None:
            continue
        sub = lbl[sl] == i
        area = int(sub.sum())
        if area < 3:
            continue
        ys, xs = np.nonzero(sub)
        cy = float(ys.mean() + sl[0].start)
        cx = float(xs.mean() + sl[1].start)
        flux = float(resid[sl][sub].sum())
        peak = float(img[sl][sub].max())
        if peak >= 250:
            kind = "saturated"
        elif area >= 80:
            kind = "extended"
        else:
            kind = "point"
        sources.append({
            "x": cx, "y": cy, "flux": max(flux, 1e-3), "area": area,
            "peak": peak, "kind": kind,
            "bbox": [float(sl[1].start), float(sl[0].start),
                     float(sl[1].stop), float(sl[0].stop)],
            "outline": source_outline(sub, ys.mean(), xs.mean(), sl[0].start, sl[1].start)
                       if kind == "extended" else None,
        })
    nb.log_line("detect/sources", len(sources), step=step)
    nb.log_text("detect/summary", f"{tile_name}: {len(sources)} sources at k={k:g}", step=step)
    return sources, mask


@nb.fn()
def photometry(tile_name: str, sources: list[dict], step: int) -> list[dict]:
    """Instrumental magnitudes + aperture radii; feeds the shared catalog."""
    for s in sources:
        s["mag"] = round(25.0 - 2.5 * math.log10(s["flux"]), 2)
        s["r"] = round(1.5 * math.sqrt(s["area"] / math.pi), 2)
        s["tile"] = tile_name
    MAG_HIST[tile_name] = [s["mag"] for s in sources]
    nb.log_histogram("photometry/magnitudes", MAG_HIST, colors=True)
    nb.log_scatter(
        "photometry/flux_vs_size",
        {tile_name: [(s["r"], s["mag"]) for s in sources]},
        colors=True,
    )
    if sources:
        nb.log_line("photometry/mean_mag",
                    float(np.mean([s["mag"] for s in sources])), step=step)
    CATALOG.extend(sources)
    return sources


@nb.fn()
def annotate_tile(img: np.ndarray, sources: list[dict], mask: np.ndarray,
                  tile_name: str, step: int) -> None:
    """One image carrying all five nb.labels kinds, each in its own color."""
    pts = [[s["x"], s["y"]] for s in sources]
    circ = [[s["x"], s["y"], s["r"] + 2.0] for s in sources if s["kind"] == "point"]
    boxes = [s["bbox"] for s in sources if s["kind"] == "saturated"]
    polys = [s["outline"] for s in sources if s["outline"]]
    kwargs: dict = {
        "points": nb.labels.Points(pts, "#22d3ee"),
        "bitmasks": nb.labels.Bitmasks(mask, "#818cf8"),
    }
    if circ:
        kwargs["circles"] = nb.labels.Circles(circ, "#a3e635")
    if boxes:
        kwargs["boxes"] = nb.labels.Boxes(boxes, "#f87171")
    if polys:
        kwargs["polygons"] = nb.labels.Polygons(polys, "#f472b6", fill=False)
    nb.log_image(img.astype(np.uint8), name=f"tiles/{tile_name}", step=step, **kwargs)


@nb.fn(depends_on=[photometry])
def build_catalog() -> list[dict]:
    """Aggregate every tile's sources (reads CATALOG — declared edge)."""
    kinds = Counter(s["kind"] for s in CATALOG)
    per_tile = Counter(s["tile"] for s in CATALOG)
    nb.log_pie("catalog/source_classes", dict(kinds))
    nb.log_bar("catalog/sources_per_tile", dict(per_tile))
    nb.log_text("catalog/summary",
                f"{len(CATALOG)} sources across {len(per_tile)} tiles")
    return list(CATALOG)


@nb.fn()
def run_pipeline(tile_paths: list[str], k: float) -> list[dict]:
    nb.log_cfg({"k_sigma": k, "tiles": len(tile_paths)})
    CATALOG.clear()
    MAG_HIST.clear()
    for step, path in enumerate(nb.track(tile_paths, name="tiles")):
        tile_name = Path(path).stem
        img = load_tile(path, step)
        bg, sigma = estimate_background(img, step)
        sources, mask = detect_sources(img, bg, sigma, k, tile_name, step)
        sources = photometry(tile_name, sources, step)
        annotate_tile(img, sources, mask, tile_name, step)
    return build_catalog()


def publish_doc(results: list[tuple[str, float, int]]) -> None:
    lines = [
        "# Detection-threshold study",
        "",
        "The same six tiles at three detection thresholds.",
        "",
        "| run | k (sigma) | sources |",
        "| --- | --- | --- |",
    ]
    for run_id, k, n in results:
        lines.append(f"| [k={k:g}](nebo://run/{run_id}) | {k:g} | {n} |")
    lines += ["", "Lower k finds more (and noisier) sources — compare"
              " [sources per tile](nebo://run/" + results[0][0] + "/build_catalog/catalog/sources_per_tile)."]
    doc = "\n".join(lines)
    group = "night-sky/threshold-study"
    try:
        from nebo.client import set_group_doc
        set_group_doc(group, "comparison.md", doc)
        print(f"group doc written to {group}/comparison.md")
    except Exception as exc:
        fallback = Path(__file__).parent / "threshold_study.md"
        fallback.write_text(doc)
        print(
            f"daemon unreachable ({exc}); doc saved to {fallback}\n"
            f"publish with: nebo groups doc set {group} comparison.md --file {fallback}"
        )


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--k", type=float, default=4.0, help="detection threshold (sigma)")
    ap.add_argument("--populate", action="store_true", help="run k in {3,4,5} into a group")
    ap.add_argument("--smoke", action="store_true", help="first 2 tiles only")
    args = ap.parse_args(argv)
    tiles = sorted(glob.glob(str(DATA_DIR / "*.png")))
    assert tiles, f"no tiles in {DATA_DIR} — run data/fetch_tiles.py first"
    if args.smoke:
        tiles = tiles[:2]
    if args.populate:
        results = []
        for k in (3.0, 4.0, 5.0):
            with nb.start_run(name=f"k={k:g}", config={"k_sigma": k},
                              group="night-sky/threshold-study") as run:
                catalog = run_pipeline(tiles, k)
            results.append((run.run_id, k, len(catalog)))
        publish_doc(results)
    else:
        with nb.start_run(name=f"k={args.k:g}", config={"k_sigma": args.k},
                          group="night-sky"):
            run_pipeline(tiles, args.k)


if __name__ == "__main__":
    main()
