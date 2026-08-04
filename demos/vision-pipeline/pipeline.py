"""Vision inference pipeline demo: per-stage results for every sample.

Pretrained torchvision models run real inference over the bundled photo
set — preprocess -> detect -> segment -> postprocess -> embed per sample,
with a TSNE embedding scatter that grows one step-tagged point per image.

Run:  python pipeline.py             # all samples (group "vision-pipeline")
      python pipeline.py --smoke     # 4 samples
      python pipeline.py --device cuda   # GPU acceleration (auto-detected too)
"""
from __future__ import annotations

import argparse
import time
import zlib
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image, ImageOps

import nebo as nb

DATA_DIR = Path(__file__).parent / "data"
MAX_SIDE = 640
DETECT_MIN_SCORE = 0.3     # raw detections shown on the detect stage card
STAGE_CARD_EVERY = 1       # intermediate stage images every Nth sample (1 = all:
                           # every stage card is step-addressable, ~60 MB/run)
PALETTE = [
    "#22d3ee", "#a3e635", "#f87171", "#f472b6", "#fbbf24",
    "#818cf8", "#34d399", "#fb923c", "#e879f9", "#94a3b8",
]
FG_COLOR = "#818cf8"       # foreground bitmask tint
CENTER_COLOR = "#ffffff"   # box-center points

nb.md("""
# Vision inference pipeline

Three pretrained torchvision models process thirty real photos (street
scenes, animals, food, rooms — see data/ATTRIBUTION.md), one sample per
step. Scrub the tracker, or click any point in the TSNE scatter, and
every stage card jumps to that photo.

**What each stage shows**

- `preprocess` — EXIF-corrected RGB input, capped at 640 px
  (`stages/input`).
- `detect` — Faster R-CNN MobileNetV3 draws every raw detection above
  0.3 confidence: one box color per class, white dots at box centers
  (`stages/detections`). `objects` and `mean_confidence` track it per
  step; `detect/summary` says it in words ("2 person, 1 bicycle").
- `segment` — LR-ASPP labels every pixel with one of 21 VOC classes;
  class-tinted masks overlay the frame (`stages/segmentation`). Food
  and interior shots often show no mask at all — VOC has no pizza
  class — and `coverage` drops to zero there.
- `postprocess` — keeps only detections above the run's `conf`
  threshold and composites the story: background dimmed to the
  segmentation foreground, boxes, and centers (`stages/final`).
  `box_fg_agreement` measures how much the two models agree — the
  fraction of detected box area the segmenter also calls foreground.
- `embed` — a third MobileNet pools each photo into a 960-d vector;
  `embeddings/tsne` grows one point per sample (openTSNE: fit on the
  first twelve, transform after), labeled by the photo's dominant
  detected class. Animals cluster away from street scenes — click an
  outlier to see why.
- `summarize` — the dataset census: detections per class (bar),
  dominant class per photo (pie), confidence distributions (histogram).

CPU-friendly; uses CUDA automatically when available.
""")
nb.ui(view="dag", tracker="step")

# Per-run accumulators (cleared by run_pipeline). summarize() reads STATS
# via depends_on — data flows through module state, not arguments.
STATS: list[dict] = []
_TSNE: dict = {"buffer": [], "embedding": None, "perplexity": None}


def class_color(name: str) -> str:
    # crc32, not hash(): stable across processes so regenerated screenshots
    # keep the same class -> color mapping.
    return PALETTE[zlib.crc32(name.encode()) % len(PALETTE)]


def stamp(t0: float) -> float:
    return round(time.perf_counter() - t0, 3)


def _emit_tsne_point(step: int, label: str, xy) -> None:
    nb.log_scatter(
        "embeddings/tsne",
        {label: [(round(float(xy[0]), 3), round(float(xy[1]), 3))]},
        step=step,
        colors=True,
    )


def _tsne_fit_and_emit() -> None:
    """Fit once on the buffered warmup vectors and emit their points.

    The perplexity honors openTSNE's ``3 * perplexity <= n - 1`` constraint,
    and is reused by every later ``transform`` call — without it, transform
    defaults to perplexity 5 and warns on stderr once per sample.
    """
    from openTSNE import TSNE

    X = np.stack([v for _, _, v in _TSNE["buffer"]])
    _TSNE["perplexity"] = min(30.0, (len(X) - 1) / 3)
    _TSNE["embedding"] = TSNE(perplexity=_TSNE["perplexity"], random_state=0).fit(X)
    for (s, label, _), xy in zip(_TSNE["buffer"], np.asarray(_TSNE["embedding"])):
        _emit_tsne_point(s, label, xy)


@nb.fn(ui={"default_tab": "text"})
def load_models(device: str) -> dict:
    """Load the three pretrained models onto the target device."""
    from torchvision.models import MobileNet_V3_Large_Weights, mobilenet_v3_large
    from torchvision.models.detection import (
        FasterRCNN_MobileNet_V3_Large_FPN_Weights,
        fasterrcnn_mobilenet_v3_large_fpn,
    )
    from torchvision.models.segmentation import (
        LRASPP_MobileNet_V3_Large_Weights,
        lraspp_mobilenet_v3_large,
    )

    t0 = time.perf_counter()
    det_w = FasterRCNN_MobileNet_V3_Large_FPN_Weights.DEFAULT
    seg_w = LRASPP_MobileNet_V3_Large_Weights.DEFAULT
    emb_w = MobileNet_V3_Large_Weights.DEFAULT
    models = {
        "device": device,
        "detector": fasterrcnn_mobilenet_v3_large_fpn(weights=det_w).eval().to(device),
        "det_categories": det_w.meta["categories"],
        "segmenter": lraspp_mobilenet_v3_large(weights=seg_w).eval().to(device),
        "seg_transform": seg_w.transforms(),
        "seg_categories": seg_w.meta["categories"],
        "embedder": mobilenet_v3_large(weights=emb_w).eval().to(device),
        "emb_transform": emb_w.transforms(),
    }
    nb.log_text(
        "status",
        f"detector + segmenter + embedder ready on {device} "
        f"in {stamp(t0):.1f}s",
    )
    return models


@nb.fn(ui={"default_tab": "text"})
def load_dataset(limit: int | None) -> list[Path]:
    """List the bundled sample images."""
    paths = sorted(DATA_DIR.glob("*.jpg"))
    if limit is not None:
        paths = paths[:limit]
    nb.log_text("status", f"{len(paths)} samples from {DATA_DIR.name}/")
    return paths


@nb.fn(ui={"default_tab": "images"})
def preprocess(path: Path, step: int):
    """EXIF-orient, force RGB, cap the longest side at MAX_SIDE."""
    t0 = time.perf_counter()
    pil = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    if max(pil.size) > MAX_SIDE:
        pil.thumbnail((MAX_SIDE, MAX_SIDE))
    arr = np.asarray(pil)
    tensor = torch.from_numpy(arr.copy()).permute(2, 0, 1).float() / 255.0
    if step % STAGE_CARD_EVERY == 0:
        nb.log_image(arr, name="stages/input", step=step)
    nb.log_line("latency", stamp(t0), step=step)
    return pil, arr, tensor


@nb.fn(ui={"default_tab": "images"})
def detect(models: dict, arr: np.ndarray, tensor: torch.Tensor, step: int) -> dict:
    """Faster R-CNN detection; raw boxes >= DETECT_MIN_SCORE."""
    t0 = time.perf_counter()
    with torch.no_grad():
        out = models["detector"]([tensor.to(models["device"])])[0]
    keep = out["scores"] >= DETECT_MIN_SCORE
    names = [models["det_categories"][i] for i in out["labels"][keep].tolist()]
    dets = {
        "boxes": out["boxes"][keep].cpu().numpy(),
        "scores": out["scores"][keep].cpu().numpy(),
        "names": names,
    }
    nb.log_line("latency", stamp(t0), step=step)
    nb.log_line("objects", len(names), step=step)
    if len(names):
        nb.log_line("mean_confidence", round(float(dets["scores"].mean()), 3), step=step)
    counts = Counter(names)
    nb.log_text(
        "detect/summary",
        ", ".join(f"{v} {k}" for k, v in counts.most_common()) or "nothing above threshold",
        step=step,
    )
    if step % STAGE_CARD_EVERY == 0:
        by_class: dict[str, list] = defaultdict(list)
        for box, name in zip(dets["boxes"].tolist(), names):
            by_class[name].append(box)
        groups = [
            nb.labels.Boxes(boxes, class_color(name))
            for name, boxes in sorted(by_class.items())
        ]
        kwargs: dict = {}
        if groups:
            centers = [
                [(x1 + x2) / 2, (y1 + y2) / 2]
                for x1, y1, x2, y2 in dets["boxes"].tolist()
            ]
            kwargs = {"boxes": groups, "points": nb.labels.Points(centers, CENTER_COLOR)}
        # Always log the card — a step with no detections still shows the
        # frame, keeping every step scrubbable.
        nb.log_image(arr, name="stages/detections", step=step, **kwargs)
    return dets


@nb.fn(ui={"default_tab": "images"})
def segment(models: dict, pil: Image.Image, arr: np.ndarray, step: int) -> np.ndarray:
    """LR-ASPP semantic segmentation, upsampled to the image resolution."""
    t0 = time.perf_counter()
    batch = models["seg_transform"](pil).unsqueeze(0).to(models["device"])
    with torch.no_grad():
        logits = models["segmenter"](batch)["out"]
    h, w = arr.shape[:2]
    logits = F.interpolate(logits, size=(h, w), mode="bilinear", align_corners=False)
    seg = logits[0].argmax(0).cpu().numpy().astype(np.uint8)
    coverage = float((seg != 0).mean())
    nb.log_line("latency", stamp(t0), step=step)
    nb.log_line("coverage", round(coverage, 3), step=step)
    if step % STAGE_CARD_EVERY == 0:
        masks = [
            nb.labels.Bitmasks(seg == c, class_color(models["seg_categories"][c]))
            for c in np.unique(seg)
            if c != 0 and (seg == c).mean() >= 0.01
        ]
        # Always log the card — LR-ASPP's 21 VOC classes cover none of e.g.
        # the food shots, and a maskless frame still belongs in the scrub.
        nb.log_image(arr, name="stages/segmentation", step=step,
                     **({"bitmasks": masks} if masks else {}))
    return seg


@nb.fn(ui={"default_tab": "images"})
def postprocess(
    arr: np.ndarray, dets: dict, seg: np.ndarray, conf: float, step: int
) -> str:
    """Filter to confident detections and build the annotated composite."""
    t0 = time.perf_counter()
    keep = dets["scores"] >= conf
    boxes = dets["boxes"][keep]
    scores = dets["scores"][keep]
    names = [n for n, k in zip(dets["names"], keep) if k]

    fg = seg != 0
    agreements = []
    centers = []
    for x1, y1, x2, y2 in boxes:
        region = fg[max(int(y1), 0):int(y2), max(int(x1), 0):int(x2)]
        agreements.append(float(region.mean()) if region.size else 0.0)
        centers.append([float((x1 + x2) / 2), float((y1 + y2) / 2)])

    composite = np.where(fg[..., None], arr, (arr * 0.35).astype(np.uint8))
    by_class: dict[str, list] = defaultdict(list)
    for box, name in zip(boxes.tolist(), names):
        by_class[name].append(box)
    kwargs: dict = {"bitmasks": nb.labels.Bitmasks(fg, FG_COLOR)}
    if by_class:
        kwargs["boxes"] = [
            nb.labels.Boxes(bxs, class_color(name))
            for name, bxs in sorted(by_class.items())
        ]
        kwargs["points"] = nb.labels.Points(centers, CENTER_COLOR)
    nb.log_image(composite, name="stages/final", step=step, **kwargs)

    nb.log_line("latency", stamp(t0), step=step)
    nb.log_line("kept_objects", len(names), step=step)
    if agreements:
        nb.log_line("box_fg_agreement", round(float(np.mean(agreements)), 3), step=step)

    dominant = names[int(np.argmax(scores))] if len(names) else "none"
    confidences: dict[str, list] = defaultdict(list)
    for name, score in zip(names, scores.tolist()):
        confidences[name].append(round(score, 3))
    STATS.append({"dominant": dominant, "classes": Counter(names),
                  "confidences": confidences})
    return dominant


@nb.fn(ui={"default_tab": "metrics"})
def embed(models: dict, pil: Image.Image, dominant: str, step: int, warmup: int) -> None:
    """Pool mobilenet features; TSNE fit-once then transform per sample."""
    t0 = time.perf_counter()
    batch = models["emb_transform"](pil).unsqueeze(0).to(models["device"])
    with torch.no_grad():
        feats = models["embedder"].features(batch)
        vec = torch.flatten(F.adaptive_avg_pool2d(feats, 1), 1)[0].cpu().numpy()

    if _TSNE["embedding"] is None:
        _TSNE["buffer"].append((step, dominant, vec))
        if len(_TSNE["buffer"]) >= warmup:
            _tsne_fit_and_emit()
    else:
        xy = np.asarray(
            _TSNE["embedding"].transform(vec[None, :], perplexity=_TSNE["perplexity"])
        )[0]
        _emit_tsne_point(step, dominant, xy)
    nb.log_line("latency", stamp(t0), step=step)


@nb.fn(depends_on=[postprocess], ui={"default_tab": "metrics"})
def summarize() -> dict:
    """Dataset-level rollups from the per-sample accumulator."""
    class_totals: Counter = Counter()
    dominants: Counter = Counter()
    confidences: dict[str, list] = defaultdict(list)
    for s in STATS:
        class_totals.update(s["classes"])
        dominants[s["dominant"]] += 1
        for name, vals in s["confidences"].items():
            confidences[name].extend(vals)

    top = dict(class_totals.most_common(12))
    if top:
        nb.log_bar("dataset/detections_per_class", top)
    if dominants:
        nb.log_pie("dataset/dominant_classes", dict(dominants))
    hist = {
        name: confidences[name]
        for name, _ in class_totals.most_common(5)
        if confidences.get(name)
    }
    if hist:
        nb.log_histogram("dataset/confidence_by_class", hist, colors=True)
    total = sum(class_totals.values())
    nb.log_text(
        "dataset/summary",
        f"{len(STATS)} samples, {total} kept detections across "
        f"{len(class_totals)} classes; most common: "
        f"{', '.join(f'{n} ({c})' for n, c in class_totals.most_common(3))}",
    )
    return {"samples": len(STATS), "detections": total}


@nb.fn()
def run_pipeline(cfg: dict) -> dict:
    nb.log_cfg(cfg)
    STATS.clear()
    _TSNE["buffer"] = []
    _TSNE["embedding"] = None
    _TSNE["perplexity"] = None
    models = load_models(cfg["device"])
    paths = load_dataset(cfg["limit"])
    warmup = min(12, max(3, len(paths) - 1))
    for step, path in enumerate(nb.track(paths, name="samples")):
        pil, arr, tensor = preprocess(path, step)
        dets = detect(models, arr, tensor, step)
        seg = segment(models, pil, arr, step)
        dominant = postprocess(arr, dets, seg, cfg["conf"], step)
        embed(models, pil, dominant, step, warmup)
    # A run shorter than the warmup threshold never triggers the in-loop
    # fit: flush what buffered, or say so instead of an empty scatter.
    if _TSNE["embedding"] is None:
        if len(_TSNE["buffer"]) >= 3:
            _tsne_fit_and_emit()
        else:
            nb.log_text(
                "embeddings/status",
                f"TSNE skipped: only {len(_TSNE['buffer'])} sample(s) buffered, need 3+",
            )
    return summarize()


def main(argv=None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None, help="max samples (default: all)")
    ap.add_argument("--conf", type=float, default=0.5, help="postprocess confidence cut")
    ap.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    ap.add_argument("--smoke", action="store_true", help="4 samples only")
    args = ap.parse_args(argv)
    device = args.device
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    if args.conf < DETECT_MIN_SCORE:
        print(
            f"note: --conf {args.conf:g} is below the detect stage's raw floor "
            f"({DETECT_MIN_SCORE}); detections below that are never produced."
        )
    limit = 4 if args.smoke else args.limit
    cfg = {
        "detector": "fasterrcnn_mobilenet_v3_large_fpn",
        "segmenter": "lraspp_mobilenet_v3_large",
        "embedder": "mobilenet_v3_large",
        "conf": args.conf,
        "device": device,
        "limit": limit,
    }
    with nb.start_run(name=f"conf={args.conf:g}", config=cfg, group="vision-pipeline"):
        return run_pipeline(cfg)


if __name__ == "__main__":
    main()
