# Nebo demo projects

Four real-world projects that exercise nebo's full surface — real
computation on real data, CPU-friendly (GPU-accelerated where available),
minutes per run. Each folder has its own README and `requirements.txt`;
none of their dependencies touch the nebo package itself.

| demo | domain | headline features |
| --- | --- | --- |
| [cifar10](cifar10/) | ML training (torch) | all 5 chart types, tagged train/val lines, misclassified-image streams, 4-run sweep group, alert rules |
| [night-sky](night-sky/) | data pipeline (numpy/scipy) | fan-out/fan-in DAG, `depends_on`, **all 5 image label kinds on one tile**, overlaid histograms, threshold-study group |
| [llm-lab](llm-lab/) | model chaining (transformers) | hierarchical text streams, verdict/score analytics, agent playbook: skills/CLI reads, derived charts, alert rule, `nebo://`-linked group doc |
| [vision-pipeline](vision-pipeline/) | inference pipeline (torchvision) | per-sample stage cards (detect boxes, segmentation bitmasks, annotated composite), growing TSNE scatter, per-stage latency lines, CUDA auto-acceleration |

## Quick start

```bash
cd demos/<demo>                     # both terminals start here
python3 -m venv ../.venv && ../.venv/bin/pip install -e ../.. -r requirements.txt
../.venv/bin/nebo serve             # terminal 1 — UI at http://localhost:7861, watches ./.nebo here
../.venv/bin/python <entry>.py      # terminal 2
```

Every entry point takes `--smoke` (seconds, tiny subset). To populate a
full workspace for screenshots or a public deployment, run each demo's
multi-run mode: `cifar10/sweep.py`, `night-sky/pipeline.py --populate`,
`llm-lab/experiments.py`, plus a full `vision-pipeline/pipeline.py` run —
then serve that logdir. To build ONE unified workspace across the demos,
run every populate command with
`NEBO_URI=<abs>/.nebo` set while `nebo serve --logdir <abs>/.nebo` is running.

Deliberate gap: none of these demos use `nb.log_audio` — audio gets its
own standalone example later.
