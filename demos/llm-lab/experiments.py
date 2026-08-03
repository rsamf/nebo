"""Run the chain once per prompt template into llm-lab/prompt-experiments.

Run:  python experiments.py            (~10-15 min CPU)
      python experiments.py --smoke    (2 docs per template)
Then hand the group to an agent — see AGENT.md.
"""
from __future__ import annotations

import argparse

import nebo as nb
from chain import TEMPLATES, run_chain

GROUP = "llm-lab/prompt-experiments"


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--smoke", action="store_true", help="2 documents per template")
    ap.add_argument("--model", default="google/flan-t5-small")
    args = ap.parse_args(argv)
    limit = 2 if args.smoke else None
    for tname in sorted(TEMPLATES):
        cfg = {"model": args.model, "template": TEMPLATES[tname],
               "template_name": tname, "limit": limit}
        with nb.start_run(name=f"prompt={tname}", config=cfg, group=GROUP) as run:
            result = run_chain(cfg)
        print(f"{run.run_id}  prompt={tname}  mean_score={result['mean_score']}")
    print(f"\n3 runs in group {GROUP}. Next: follow AGENT.md.")


if __name__ == "__main__":
    main()
