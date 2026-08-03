# cifar10 — ML training demo

A small CNN trained on real CIFAR-10, instrumented with nebo. CPU-only,
~4 min per run. Shows: tagged train/val line charts, per-class accuracy
bar (re-emitted each epoch), per-layer weight histograms, a PCA embedding
scatter, a class-distribution pie, misclassified-image streams, progress
tracking, config capture, and a 4-run sweep group for comparison views.

## Run

```bash
python -m venv ../.venv && ../.venv/bin/pip install -e ../.. -r requirements.txt
nebo serve            # in another terminal; the UI is at localhost:7861
../.venv/bin/python train.py           # one run (group "cifar10")
../.venv/bin/python sweep.py           # 4 runs into cifar10/sweep + results doc
```

`--smoke` on either script runs a tiny subset in seconds. The first run
downloads CIFAR-10 (~170 MB) into `data/`.

## Alert rules to try

```bash
nebo alerts set --title "Target accuracy" --condition "epoch/accuracy > 60"
nebo alerts set --title "Run went quiet" --condition "last_event > 120"
```

The first fires once a run crosses 60% val accuracy; the second is the
heartbeat idiom — it fires when a run has been idle for two minutes
(nebo's run-completion signal).

## What to screenshot

- Flat view of a single run: all five chart types + misclassified images.
- The `cifar10/sweep` group selected for comparison: overlaid
  `epoch/accuracy` curves colored by run.
- The `results` group doc with its `nebo://` deep links.
