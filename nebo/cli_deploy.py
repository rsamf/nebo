"""`nebo deploy` — push the daemon to a Hugging Face Space.

Creates a Docker-SDK Space (or reuses an existing one), uploads a
Dockerfile and Spaces-flavored README, sets `NEBO_API_TOKEN` as a Space
secret, and prints the connection snippet for the user's local SDK.
"""

from __future__ import annotations

import argparse
import secrets
import string
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from string import Template
from typing import Optional


# Stages that mean the Space is up. "RUNNING" is canonical; the others
# show up briefly during startup transitions in some HF API versions.
_READY_STAGES = {"RUNNING"}
# Stages that mean we should stop waiting and surface the failure.
_FATAL_STAGES = {"BUILD_ERROR", "RUNTIME_ERROR", "CONFIG_ERROR", "NO_APP_FILE"}


def _probe_health(space_url: str, timeout_s: float = 5.0) -> bool:
    """Hit the daemon's /health and return True iff it 200s."""
    try:
        req = urllib.request.Request(f"{space_url}/health", method="GET")
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return False


def _wait_for_space_ready(
    api,
    space_id: str,
    space_url: str,
    *,
    timeout_s: float = 900.0,
    poll_s: float = 5.0,
    grace_s: float = 60.0,
) -> None:
    """Block until the HF Space rebuild finishes and /health responds.

    After file uploads, HF kicks off a Docker rebuild but the old image
    keeps serving until the new one's ready. Callers that hit the Space
    immediately after `nebo deploy` returns would otherwise talk to the
    pre-deploy daemon — that's the bug behind `build_docs_demos.py`
    pushing runs to the stale instance.

    Strategy: poll `SpaceRuntime.stage`. Wait until it transitions away
    from RUNNING (rebuild started), then wait for it to return to
    RUNNING, then confirm /health responds. If no transition is seen
    within `grace_s` we assume the upload didn't trigger a rebuild
    (identical files) and fall back to the /health probe alone.
    """
    start = time.monotonic()
    saw_rebuild = False
    while True:
        elapsed = time.monotonic() - start
        if elapsed > timeout_s:
            raise TimeoutError(
                f"Space {space_id} did not reach a serving state within "
                f"{timeout_s:.0f}s. Check https://huggingface.co/spaces/{space_id}"
            )

        try:
            runtime = api.get_space_runtime(repo_id=space_id)
            stage = (getattr(runtime, "stage", None) or "").upper()
        except Exception as e:
            # Transient API hiccup — keep polling rather than abort.
            print(f"  warning: could not query Space runtime ({e}); retrying")
            time.sleep(poll_s)
            continue

        if stage in _FATAL_STAGES:
            raise RuntimeError(
                f"Space {space_id} entered fatal stage {stage!r}. "
                f"Check the build logs at https://huggingface.co/spaces/{space_id}"
            )

        if stage not in _READY_STAGES:
            if not saw_rebuild:
                print(f"  Space stage: {stage} (rebuilding...)")
            saw_rebuild = True
        elif saw_rebuild or elapsed > grace_s:
            # Either we observed a rebuild and it's back to RUNNING, or
            # the grace window expired without a rebuild (no-op deploy).
            # In both cases, confirm the daemon is actually serving.
            if _probe_health(space_url):
                if saw_rebuild:
                    print("  ✓ Rebuild complete; /health 200 OK.")
                else:
                    print("  ✓ /health 200 OK (no rebuild observed).")
                return
            # Stage says RUNNING but /health isn't up yet — keep polling.

        time.sleep(poll_s)


_SPACES_TEMPLATE_DIR = Path(__file__).parent / "server" / "spaces"


def _generate_token(prefix: str = "nb_") -> str:
    """Generate a 32-char URL-safe token with a recognizable prefix."""
    alphabet = string.ascii_letters + string.digits
    return prefix + "".join(secrets.choice(alphabet) for _ in range(32))


def _render_readme(space_id: str) -> str:
    """Substitute the template's ${space_url} / ${title} placeholders."""
    tmpl_path = _SPACES_TEMPLATE_DIR / "README.md.tmpl"
    tmpl = Template(tmpl_path.read_text(encoding="utf-8"))
    # HF Spaces URLs replace `/` with `-` and lowercase the slug.
    space_url = f"{space_id.replace('/', '-').lower()}.hf.space"
    return tmpl.substitute(title="Nebo Dashboard", space_url=space_url)


def _render_dockerfile(install_block: str) -> str:
    """Substitute the install command into the Dockerfile template."""
    tmpl_path = _SPACES_TEMPLATE_DIR / "Dockerfile.tmpl"
    tmpl = Template(tmpl_path.read_text(encoding="utf-8"))
    return tmpl.substitute(install_block=install_block)


def _build_wheel(out_dir: Path) -> Path:
    """Build a wheel of the current source tree into `out_dir`.

    Returns the path to the produced .whl. Used by `--from-source` so the
    Space ships the local code instead of pulling from PyPI.
    """
    # Locate the project root by walking up to the first pyproject.toml
    # whose name is "nebo".
    here = Path(__file__).resolve().parent
    for ancestor in [here, *here.parents]:
        pp = ancestor / "pyproject.toml"
        if pp.exists() and "nebo" in pp.read_text(encoding="utf-8")[:200]:
            project_root = ancestor
            break
    else:
        raise RuntimeError("could not locate the nebo project root from the installed package")

    out_dir.mkdir(parents=True, exist_ok=True)
    # `uv build --wheel` is fast and isolated; falling back to `python -m
    # build` would also work but adds an extra dependency.
    cmd = ["uv", "build", "--wheel", "--out-dir", str(out_dir)]
    print(f"Building wheel from {project_root}...")
    proc = subprocess.run(cmd, cwd=str(project_root), capture_output=True, text=True)
    if proc.returncode != 0:
        # If `uv` isn't on PATH, suggest the standard fallback.
        raise RuntimeError(
            f"wheel build failed (cwd={project_root}):\n{proc.stderr}\n"
            f"Make sure `uv` is on PATH, or use `pip install nebo` mode (omit --from-source)."
        )
    wheels = sorted(out_dir.glob("nebo-*.whl"))
    if not wheels:
        raise RuntimeError(f"no nebo-*.whl produced in {out_dir}")
    # If multiple wheels are produced (rare), the newest sorts last.
    return wheels[-1]


def cmd_deploy(args: argparse.Namespace) -> None:
    """Deploy the nebo daemon to a Hugging Face Space."""
    space_id = args.space_id
    if "/" not in space_id:
        print(f"Error: --space-id must be 'owner/name' (got {space_id!r})", file=sys.stderr)
        sys.exit(1)

    try:
        from huggingface_hub import HfApi
        from huggingface_hub.utils import HfHubHTTPError
    except ImportError:
        print(
            "huggingface_hub is required for `nebo deploy`. Install with:\n"
            "  pip install 'nebo[deploy]'\n"
            "or:\n"
            "  pip install huggingface_hub",
            file=sys.stderr,
        )
        sys.exit(1)

    api = HfApi(token=args.hf_token)

    print(f"Ensuring Space {space_id} exists...")
    try:
        api.create_repo(
            repo_id=space_id,
            repo_type="space",
            space_sdk="docker",
            private=args.private,
            exist_ok=True,
        )
    except HfHubHTTPError as e:
        print(f"Failed to create/find Space: {e}", file=sys.stderr)
        sys.exit(1)

    # Token: use --api-token if supplied, otherwise generate one and
    # surface it once. Either way we set it as a Space secret so the
    # daemon container reads it from env at startup.
    api_token = args.api_token or _generate_token()
    print("Setting NEBO_API_TOKEN secret on the Space...")
    api.add_space_secret(
        repo_id=space_id,
        key="NEBO_API_TOKEN",
        value=api_token,
        description="Required on every API request as X-Nebo-Token or ?token=",
    )

    # Read/write modes are non-secret configuration — public Space
    # variables (visible in the UI, not redacted). Default deploy
    # produces 'public' reads + 'private' writes: anyone can view,
    # only the token holder can push events / control runs.
    read_mode = getattr(args, "read", "public")
    write_mode = getattr(args, "write", "private")
    print(f"Setting access mode: read={read_mode}, write={write_mode}...")
    api.add_space_variable(
        repo_id=space_id, key="NEBO_READ_MODE", value=read_mode,
        description="public | private — gates GET requests when NEBO_API_TOKEN is set.",
    )
    api.add_space_variable(
        repo_id=space_id, key="NEBO_WRITE_MODE", value=write_mode,
        description="public | private — gates non-GET requests when NEBO_API_TOKEN is set.",
    )

    # Decide the Dockerfile install line. By default we pull `nebo`
    # from PyPI; with --from-source we build a wheel locally and ship
    # it alongside the Dockerfile so the Space runs the current
    # checkout instead of the last published release.
    wheel_path: Optional[Path] = None
    if getattr(args, "from_source", False):
        tmp = Path(tempfile.mkdtemp(prefix="nebo-deploy-"))
        wheel_path = _build_wheel(tmp / "wheel")
        # Preserve the original wheel filename — pip rejects renamed
        # wheels because it parses version/abi/platform from the name.
        install_block = (
            f"COPY {wheel_path.name} /tmp/\n"
            f"RUN pip install --no-cache-dir /tmp/{wheel_path.name}"
        )
    else:
        install_block = "RUN pip install --no-cache-dir 'nebo'"

    dockerfile = _render_dockerfile(install_block)
    readme = _render_readme(space_id)

    print("Uploading Dockerfile and README...")
    api.upload_file(
        path_or_fileobj=dockerfile.encode("utf-8"),
        path_in_repo="Dockerfile",
        repo_id=space_id,
        repo_type="space",
        commit_message="nebo deploy: Dockerfile",
    )
    api.upload_file(
        path_or_fileobj=readme.encode("utf-8"),
        path_in_repo="README.md",
        repo_id=space_id,
        repo_type="space",
        commit_message="nebo deploy: README",
    )
    if wheel_path is not None:
        print(f"Uploading wheel ({wheel_path.name})...")
        api.upload_file(
            path_or_fileobj=str(wheel_path),
            path_in_repo=wheel_path.name,
            repo_id=space_id,
            repo_type="space",
            commit_message="nebo deploy: wheel",
        )

    space_url = f"https://{space_id.replace('/', '-').lower()}.hf.space"

    # Wait for the Space to actually serve the new image. HF starts a
    # Docker rebuild after `upload_file` but keeps the prior container
    # alive until it's ready; callers that hit the Space the moment
    # `nebo deploy` returns would otherwise talk to the old daemon.
    if getattr(args, "wait", True):
        print()
        print(f"Waiting for {space_id} to finish rebuilding...")
        _wait_for_space_ready(api, space_id, space_url)

    print()
    print("✓ Deployed.")
    print(f"  Space:    https://huggingface.co/spaces/{space_id}")
    print(f"  URL:      {space_url}")
    print(f"  Access:   read={read_mode}, write={write_mode}")
    print()
    print("Connect your SDK by setting these env vars locally:")
    print(f"  export NEBO_URI={space_url}")
    print(f"  export NEBO_API_TOKEN={api_token}")
    print()
    print("Or in code:")
    print(f"  nb.init(uri={space_url!r}, api_token={api_token!r})")
    print()
    print("To point the `nebo` CLI at this Space (for `nebo logs`,")
    print("`nebo runs list`, etc.), also set:")
    print(f"  export NEBO_CLI_URL={space_url}")
    print()
    if not args.api_token:
        print(
            "The token above is randomly generated and was set on the "
            "Space. Save it somewhere — `nebo deploy` won't show it again."
        )
