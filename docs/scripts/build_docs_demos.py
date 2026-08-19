"""Run every docs demo and publish the resulting runs to a Hugging Face bucket.

Pipeline:

  1. Walk ``docs/demos/**/*.py``.
  2. For each script, derive a stable ``run_id`` from its path
     (``docs/demos/<section>/<n>_<name>.py`` -> ``docs-<section>-<name>``).
  3. Execute the script with ``NEBO_URI=<build_dir>``, ``NEBO_RUN_ID=<derived>``
     and ``NEBO_GROUP=<section>`` so a ``.nebo`` file lands in the build dir
     with the pinned ID and its group baked into the header, then rename it to
     ``<run_id>.nebo``.
  4. Sync the build dir into a Storage Bucket with ``delete=True``, so the
     bucket ends up holding exactly the runs this build produced.

The demos Space serves those files directly (``nebo serve --logdir
hf://buckets/<owner>/<name>``) and **only reads** them: a ``.nebo`` file is an
append-only stream and object storage cannot append, so this script is the one
writer. The runs therefore outlive the Space, which can be rebuilt or scaled to
zero and still show the same dashboards — the whole point of publishing to an
archive rather than replaying events into a daemon's memory, which is what this
script used to do via ``nebo load``.

Grouping needs no ``meta/``: ``NEBO_GROUP`` lands in the ``.nebo`` header, and
the daemon re-derives the run tree from those headers on every scan.

Filenames are pinned to ``<run_id>.nebo`` — dropping the SDK's timestamp
prefix — so each rebuild replaces the same paths instead of accumulating one
per release.

The derived run IDs are referenced verbatim from the ``.rst`` files'
``<iframe src=...&run=docs-...&...>``. Renaming or moving a script
changes its run ID — fix the ``.rst`` to match.

Usage::

    NEBO_DEMOS_BUCKET=rsamf/nebo-demo-runs \\
    HF_TOKEN=hf_xxx \\
    uv run python docs/scripts/build_docs_demos.py

    # Dry-run (build .nebo files but skip upload):
    uv run python docs/scripts/build_docs_demos.py --no-upload

    # Only rebuild one section:
    uv run python docs/scripts/build_docs_demos.py --section index
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMOS_ROOT = REPO_ROOT / "docs" / "demos"


def derive_run_id(script: Path) -> str:
    """``docs/demos/index/1_hello_world.py`` -> ``docs-index-hello-world``.

    Drops a leading ``<digits>_`` from the filename so demos can be
    ordered on disk without that order leaking into the public run ID.
    """
    rel = script.relative_to(DEMOS_ROOT)
    parts = list(rel.with_suffix("").parts)
    parts[-1] = re.sub(r"^\d+_", "", parts[-1])
    return "docs-" + "-".join(parts).replace("_", "-")


def discover_demos(section: str | None) -> list[Path]:
    if not DEMOS_ROOT.exists():
        return []
    scripts = sorted(p for p in DEMOS_ROOT.rglob("*.py") if not p.name.startswith("_"))
    if section:
        scripts = [p for p in scripts if p.relative_to(DEMOS_ROOT).parts[:1] == (section,)]
    return scripts


def derive_group(script: Path) -> str:
    """``docs/demos/guide/2_log_line.py`` -> ``guide``.

    The section directory becomes the run's group, so the Space sidebar
    mirrors the docs structure. This rides in the ``.nebo`` header, which is
    why the archive needs no ``meta/``.
    """
    return script.relative_to(DEMOS_ROOT).parts[0]


def run_demo(script: Path, build_dir: Path) -> Path:
    """Execute one demo and return the .nebo file it produced."""
    run_id = derive_run_id(script)
    group = derive_group(script)
    env = {
        **os.environ,
        "NEBO_URI": str(build_dir),
        "NEBO_RUN_ID": run_id,
        "NEBO_GROUP": group,
        "NEBO_QUIET": "1",
    }
    env.pop("NEBO_NO_STORE", None)
    print(f"  -> running {script.relative_to(REPO_ROOT)} "
          f"(run_id={run_id}, group={group})")
    subprocess.run(
        [sys.executable, str(script)],
        cwd=REPO_ROOT,
        env=env,
        check=True,
    )
    matches = sorted(build_dir.glob(f"*_{run_id}.nebo"))
    if not matches:
        raise RuntimeError(
            f"{script.name} did not produce a .nebo file for run_id={run_id} "
            f"in {build_dir}. Did the script call any nb.* function?"
        )
    if len(matches) > 1:
        print(f"     note: multiple files for {run_id}, picking newest: {matches[-1].name}")
    # Drop the SDK's timestamp prefix so rebuilds overwrite the same object
    # instead of piling up a new path per release.
    final = build_dir / f"{run_id}.nebo"
    for stale in matches[:-1]:
        stale.unlink()
    matches[-1].rename(final)
    return final


def upload(build_dir: Path, bucket_id: str, token: str | None) -> None:
    """Sync the build dir into the bucket, replacing what was there.

    ``delete=True`` removes destination objects absent from the source, so the
    bucket ends up a byte-for-byte mirror of this build — fully reproducible.
    There is deliberately no exclude: this script is the only writer, and the
    daemon that serves the bucket never writes to it, so anything else in there
    is stale by definition.
    """
    try:
        from huggingface_hub import create_bucket, sync_bucket
    except ImportError:
        print(
            "huggingface_hub is required to publish demo runs. Install with:\n"
            "  uv sync --all-groups\n"
            "or:\n"
            "  pip install huggingface_hub",
            file=sys.stderr,
        )
        raise

    print(f"Ensuring bucket {bucket_id} exists...")
    # Created on first run, so there is nothing to set up by hand. Public on
    # purpose: the demos Space reads it anonymously (no HF_TOKEN secret is set
    # on the Space), so a private bucket would serve zero runs.
    create_bucket(bucket_id, private=False, exist_ok=True, token=token)

    files = sorted(p.name for p in build_dir.glob("*.nebo"))
    print(f"Syncing {len(files)} run(s) to hf://buckets/{bucket_id}:")
    for name in files:
        print(f"  -> {name}")
    sync_bucket(
        source=str(build_dir),
        dest=f"hf://buckets/{bucket_id}",
        delete=True,
        token=token,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--build-dir",
        default=str(REPO_ROOT / "build" / "demos"),
        help="Where to write .nebo files (will be wiped first).",
    )
    parser.add_argument(
        "--section",
        help="Only run demos under docs/demos/<section>/ (e.g. 'index').",
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Skip publishing; just produce .nebo files locally.",
    )
    parser.add_argument(
        "--bucket",
        default=os.environ.get("NEBO_DEMOS_BUCKET"),
        help="Hugging Face Storage Bucket to publish into, as <owner>/<name>. "
             "Default: $NEBO_DEMOS_BUCKET.",
    )
    parser.add_argument(
        "--hf-token",
        default=os.environ.get("HF_TOKEN"),
        help="Hugging Face write token. Default: $HF_TOKEN (or a cached login).",
    )
    args = parser.parse_args()

    # Validate the publish arguments before spending a couple of minutes
    # running 18 demo scripts.
    if not args.no_upload:
        if not args.bucket:
            print(
                "ERROR: --bucket is required for upload. "
                "Set NEBO_DEMOS_BUCKET, or pass --no-upload.",
                file=sys.stderr,
            )
            return 2
        # Publishing mirrors the build dir, so a partial build would delete
        # the sections it did not produce.
        if args.section:
            print(
                f"ERROR: --section only builds {args.section!r}, but publishing "
                "replaces every run in the bucket. Use --no-upload with "
                "--section, or run a full build to publish.",
                file=sys.stderr,
            )
            return 2

    build_dir = Path(args.build_dir).resolve()
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)

    scripts = discover_demos(args.section)
    if not scripts:
        print(f"No demo scripts found under {DEMOS_ROOT}")
        return 0
    print(f"Found {len(scripts)} demo script(s) under {DEMOS_ROOT}")

    produced: list[Path] = []
    for script in scripts:
        produced.append(run_demo(script, build_dir))

    if args.no_upload:
        print(f"\nWrote {len(produced)} .nebo file(s) to {build_dir}. Skipping upload.")
        return 0

    upload(build_dir, args.bucket, args.hf_token)

    print(f"\nPublished {len(produced)} run(s) to hf://buckets/{args.bucket}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
