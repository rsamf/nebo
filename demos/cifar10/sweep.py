"""Four-config CIFAR-10 sweep into group cifar10/sweep, plus a results doc.

Run from this directory:  python sweep.py     (~15 min CPU)
                          python sweep.py --smoke
"""
from __future__ import annotations

import argparse
from pathlib import Path

import nebo as nb
from train import run_training

SWEEP = [
    {"lr": 1e-3, "width": 32},
    {"lr": 3e-3, "width": 32},
    {"lr": 1e-3, "width": 64},
    {"lr": 3e-4, "width": 16},
]
BASE = {"epochs": 3, "batch_size": 128, "train_size": 20000}
GROUP = "cifar10/sweep"


def publish_doc(results: list[dict]) -> None:
    best = max(results, key=lambda r: r["acc"])
    lines = [
        "# Sweep results",
        "",
        "Four configurations, 3 epochs each on a 20k CIFAR-10 subset.",
        "",
        "| run | val accuracy |",
        "| --- | --- |",
    ]
    for r in sorted(results, key=lambda r: -r["acc"]):
        mark = " **(best)**" if r is best else ""
        lines.append(f"| [{r['name']}](nebo://run/{r['run_id']}){mark} | {r['acc']:.1f}% |")
    lines += [
        "",
        f"Best accuracy curve: [{best['name']}](nebo://run/{best['run_id']}/evaluate/epoch/accuracy).",
    ]
    doc = "\n".join(lines)
    try:
        from nebo.client import set_group_doc
        set_group_doc(GROUP, "results.md", doc)
        print(f"group doc written to {GROUP}/results.md")
    except Exception as exc:
        fallback = Path(__file__).parent / "sweep_results.md"
        fallback.write_text(doc)
        print(
            f"daemon unreachable ({exc}); doc saved to {fallback}\n"
            f"publish with: nebo groups doc set {GROUP} results.md --file {fallback}"
        )


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--smoke", action="store_true", help="2 configs, 1 epoch, tiny subset")
    args = ap.parse_args(argv)
    base = dict(BASE)
    sweep = SWEEP
    if args.smoke:
        base.update(epochs=1, train_size=2000)
        sweep = SWEEP[:2]
    results = []
    for over in sweep:
        cfg = {**base, **over}
        name = f"lr={cfg['lr']:g}-w{cfg['width']}"
        with nb.start_run(name=name, config=cfg, group=GROUP) as run:
            acc = run_training(cfg)
        results.append({"run_id": run.run_id, "name": name, "acc": acc})
    publish_doc(results)


if __name__ == "__main__":
    main()
