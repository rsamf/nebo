# night-sky — DAG data-processing pipeline demo

Real star detection and photometry over Digitized Sky Survey tiles
(bundled in `data/`, fully offline). numpy + scipy + pillow only; a run
takes well under a minute. Shows: an inferred fan-out/fan-in DAG with a
`depends_on` declared edge, **all five `nb.labels` kinds together on the
densest tiles** (points = centroids, circles = apertures, boxes = saturated
stars, polygons = extended-source outlines, bitmask = detection mask),
overlaid magnitude histograms, a flux-vs-size scatter, per-tile bars, a
source-class pie, and per-tile progress.

## Run

```bash
python -m venv ../.venv && ../.venv/bin/pip install -e ../.. -r requirements.txt
nebo serve                                  # in another terminal
../.venv/bin/python pipeline.py             # one run, k=4
../.venv/bin/python pipeline.py --populate  # 3 thresholds -> night-sky/threshold-study
```

## What to screenshot

- DAG view: load -> background -> detect -> photometry -> annotate,
  fanning into build_catalog.
- The `tiles/m13` image card (any k in `{3, 4, 5}`): all five label kinds
  in distinct colors — m13's dense globular-cluster field reliably has
  point, extended, *and* saturated sources at every threshold. Sparser
  tiles may be missing a kind or two (e.g. m67 has no `extended` source
  at k=3/4/5, so its card only shows four of the five kinds).
- The overlaid `photometry/magnitudes` histogram across tiles.
- The `threshold-study` group with its `comparison` doc.
