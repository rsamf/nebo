# Nebo demo projects

Three real-world projects that exercise nebo's full surface — real
computation on real data, CPU-only, minutes per run. Each folder has its
own README and `requirements.txt`; none of their dependencies touch the
nebo package itself.

| demo | domain | headline features |
| --- | --- | --- |
| [cifar10](cifar10/) | ML training (torch) | all 5 chart types, tagged train/val lines, misclassified-image streams, 4-run sweep group, alert rules |
| [night-sky](night-sky/) | data pipeline (numpy/scipy) | fan-out/fan-in DAG, `depends_on`, **all 5 image label kinds on one tile**, overlaid histograms, threshold-study group |
| [llm-lab](llm-lab/) | model chaining (transformers) | hierarchical text streams, verdict/score analytics, agent playbook: MCP/CLI reads, derived charts, alert rule, `nebo://`-linked group doc |

## Quick start

```bash
python3 -m venv demos/.venv
demos/.venv/bin/pip install -e . -r demos/<demo>/requirements.txt
nebo serve          # terminal 1 — UI at http://localhost:7861
cd demos/<demo> && ../.venv/bin/python <entry>.py    # terminal 2
```

Every entry point takes `--smoke` (seconds, tiny subset). To populate a
full workspace for screenshots or a public deployment, run each demo's
multi-run mode: `cifar10/sweep.py`, `night-sky/pipeline.py --populate`,
`llm-lab/experiments.py` — then serve that logdir.

Deliberate gap: none of these demos use `nb.log_audio` — audio gets its
own standalone example later.
