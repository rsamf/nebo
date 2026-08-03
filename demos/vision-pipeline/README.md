# vision-pipeline — inference image-processing demo

A pure-inference pipeline over a bundled set of CC-licensed photos (see
`data/ATTRIBUTION.md`): preprocessing, Faster R-CNN object detection,
LR-ASPP semantic segmentation, and a postprocessing composite for
**every sample** — scrubbing the tracker steps through the dataset
image by image. Per-sample embeddings feed a TSNE scatter that grows
one step-tagged point per image (fit once on the first 12 samples via
openTSNE, then transform), labeled by dominant detected class.

Shows: an inferred multi-stage DAG (`preprocess → detect → segment →
postprocess → embed`, models fanning out from `load_models`,
`depends_on` on the summary), Boxes/Bitmasks/Points overlays, per-stage
latency and count lines, coverage/agreement lines, dataset-level
bar/pie/histogram rollups.

## Run

```bash
python -m venv ../.venv && ../.venv/bin/pip install -e ../.. -r requirements.txt
../.venv/bin/nebo serve   # from this directory, in another terminal
../.venv/bin/python pipeline.py            # ~3-5 min CPU, 28 samples
../.venv/bin/python pipeline.py --smoke    # 4 samples
```

First run downloads ~110 MB of pretrained torchvision weights into the
torch hub cache. `--limit N` bounds the sample count; `--conf` sets the
postprocess confidence cut (run name is `conf=<value>`).

## GPU acceleration

The pipeline auto-selects CUDA when available (`--device` to force).
Note the shared `demos/.venv` installs CPU-only torch wheels (via the
cifar10/llm-lab requirements); for GPU, install a CUDA torch build in a
separate venv: `pip install torch torchvision` from the default PyPI
index on a CUDA machine, then the remaining requirements.

## What to screenshot

- A `stages/final` card: background-dimmed composite with per-class
  boxes, foreground bitmask, and box-center points.
- The `embeddings/tsne` scatter mid-run, with the step filter active.
- The DAG view: load_models fanning into detect/segment/embed.
- `dataset/confidence_by_class` overlaid histograms after a full run.
