# llm-lab — model chaining + agent demo

A summarize-and-judge chain running genuinely local inference
(flan-t5-small, CPU, first run downloads ~300 MB) over real Wikipedia
extracts. Shows: a chained DAG, hierarchical text streams
(`summarize/prompt`, `summarize/output`, `judge/verdict`,
`judge/rationale`) in the tracker's stream tree, score/latency lines, a
verdict pie, per-category bars, word-length histograms — and the agent
workflow in AGENT.md, where a coding agent compares runs, logs a derived
chart, sets an alert rule, and authors a group doc with `nebo://` links.

## Run

```bash
python -m venv ../.venv && ../.venv/bin/pip install -e ../.. -r requirements.txt
nebo serve                                # in another terminal
../.venv/bin/python chain.py              # one run (group "llm-lab")
../.venv/bin/python experiments.py        # 3 prompt variants -> llm-lab/prompt-experiments
```

`--smoke` limits to 2 documents. Then follow `AGENT.md` with your agent.

## What to screenshot

- Tracker stream tree with the per-stage text streams; scrub across docs.
- The verdict pie + per-category bars on `aggregate`.
- The three `prompt=` runs compared; the agent-authored `findings` doc.
