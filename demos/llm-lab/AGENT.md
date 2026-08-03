# Agent playbook — analyze the prompt experiments

For a coding agent with the nebo skills installed (`nebo skills install`)
and a daemon running (`nebo serve`). `experiments.py` has left three runs
in the `llm-lab/prompt-experiments` group, one per prompt template. Your
job: decide which template wins and publish the findings.

Useful ids: judge scores live on loggable `judge`, stream `judge/score`;
rollups live on loggable `aggregate`.

## 1. Find the runs

```bash
nebo runs list --json
```

Note the three run ids whose names start with `prompt=`.

## 2. Compare scores across runs

```bash
nebo metrics get judge --name judge/score --runs <id1>,<id2>,<id3> --json
```

Compute each run's mean score. Read a few verdicts/rationales for color:

```bash
nebo text ls --run <id> --node judge --limit 20
```

## 3. Log a derived comparison chart

Write the per-template means back as a bar chart on the best run (it lands
on the `__agent__` loggable — visibly agent-authored):

```bash
nebo metrics log --run <best_id> --entries-json '[{"name": "analysis/avg_score_by_prompt", "type": "bar", "value": {"plain": 61.2, "one-sentence": 55.0, "explain-simple": 58.4}}]'
```

(Substitute the real means you computed.)

## 4. Set an alert rule for future runs

```bash
nebo alerts set --title "Low judge score" --condition "judge/score < 50"
```

## 5. Publish findings as a group doc

Write `findings.md` with: the winner and why, the mean-score table, one
good and one bad example linked by `nebo://` deep link — e.g.
`nebo://run/<id>/judge/judge/verdict@3` (verdict at doc step 3) and
`nebo://run/<id>/summarize/summarize/output@3`. Then:

```bash
nebo groups doc set llm-lab/prompt-experiments findings --file findings.md
```

The doc renders in the UI's group tree with working deep links — that's
the artifact this demo exists to produce.
