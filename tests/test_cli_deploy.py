"""Tests for `nebo deploy` — the Hugging Face Space deploy CLI.

We don't hit the network: the huggingface_hub API is mocked at the
import boundary, and we exercise the URL/template substitution and
the input validation paths.
"""

from __future__ import annotations

import argparse
import sys
from unittest.mock import MagicMock, patch

import pytest

from nebo.cli_deploy import _generate_token, _render_readme, cmd_deploy


def test_token_has_prefix_and_length() -> None:
    t = _generate_token()
    assert t.startswith("nb_")
    # 32 random chars after the prefix.
    assert len(t) == 3 + 32


def test_two_tokens_differ() -> None:
    assert _generate_token() != _generate_token()


def test_render_readme_substitutes_space_url() -> None:
    body = _render_readme("alice/my-dashboard")
    assert "alice-my-dashboard.hf.space" in body
    # Frontmatter is preserved.
    assert "sdk: docker" in body
    assert "app_port: 7860" in body


def test_render_readme_lowercases_slug() -> None:
    body = _render_readme("Alice/MyDashboard")
    # Spaces URLs are lowercase; `_render_readme` follows that.
    assert "alice-mydashboard.hf.space" in body


def test_invalid_space_id_exits(capsys: pytest.CaptureFixture[str]) -> None:
    args = argparse.Namespace(
        space_id="no-slash", hf_token=None, api_token=None, private=False, from_source=False,
        read="public", write="private",
    )
    with pytest.raises(SystemExit):
        cmd_deploy(args)
    captured = capsys.readouterr()
    assert "owner/name" in captured.err


def test_missing_huggingface_hub_exits(capsys: pytest.CaptureFixture[str]) -> None:
    """If huggingface_hub isn't installed, `cmd_deploy` should print an
    install hint and exit non-zero rather than raising ImportError."""
    args = argparse.Namespace(
        space_id="alice/my-dashboard", hf_token=None, api_token=None, private=False,
    )
    # Simulate huggingface_hub being unimportable.
    with patch.dict(sys.modules, {"huggingface_hub": None}):
        with pytest.raises(SystemExit):
            cmd_deploy(args)
    captured = capsys.readouterr()
    assert "huggingface_hub" in captured.err
    assert "pip install" in captured.err


def test_happy_path_calls_hf_api() -> None:
    """End-to-end with a mocked HfApi: ensure we create the repo, set the
    secret, and upload both templates."""
    fake_api = MagicMock()
    fake_module = MagicMock()
    fake_module.HfApi.return_value = fake_api
    # huggingface_hub.utils.HfHubHTTPError is referenced for `except`
    fake_utils = MagicMock()
    fake_utils.HfHubHTTPError = type("HfHubHTTPError", (Exception,), {})
    fake_module.utils = fake_utils

    args = argparse.Namespace(
        space_id="alice/my-dashboard",
        hf_token="hf_xxx",
        api_token="nb_test_token",
        private=False,
        from_source=False,
        read="public",
        write="private",
        # Skip the post-upload rebuild poll: with a MagicMock HfApi the
        # Space stage never reaches RUNNING, which would burn the full
        # 900s wait timeout in the test suite.
        wait=False,
    )

    with patch.dict(sys.modules, {
        "huggingface_hub": fake_module,
        "huggingface_hub.utils": fake_utils,
    }):
        cmd_deploy(args)

    fake_api.create_repo.assert_called_once()
    create_kwargs = fake_api.create_repo.call_args.kwargs
    assert create_kwargs["repo_id"] == "alice/my-dashboard"
    assert create_kwargs["space_sdk"] == "docker"
    assert create_kwargs["exist_ok"] is True

    fake_api.add_space_secret.assert_called_once()
    sec_kwargs = fake_api.add_space_secret.call_args.kwargs
    assert sec_kwargs["key"] == "NEBO_API_TOKEN"
    assert sec_kwargs["value"] == "nb_test_token"

    # Read/write modes are non-secret variables, not secrets.
    assert fake_api.add_space_variable.call_count == 2
    var_keys = {c.kwargs["key"]: c.kwargs["value"] for c in fake_api.add_space_variable.call_args_list}
    assert var_keys == {"NEBO_READ_MODE": "public", "NEBO_WRITE_MODE": "private"}

    # Two upload_file calls: Dockerfile + README.
    assert fake_api.upload_file.call_count == 2
    paths = {c.kwargs["path_in_repo"] for c in fake_api.upload_file.call_args_list}
    assert paths == {"Dockerfile", "README.md"}


def test_logdir_deploy_points_the_space_at_the_bucket() -> None:
    """The Dockerfile CMD is the whole mechanism: get it wrong and the Space
    keeps serving its ephemeral /data, which is the bug this feature fixes."""
    fake_api = MagicMock()
    fake_module = MagicMock()
    fake_module.HfApi.return_value = fake_api
    fake_utils = MagicMock()
    fake_utils.HfHubHTTPError = type("HfHubHTTPError", (Exception,), {})
    fake_module.utils = fake_utils

    def run(**over):
        fake_api.reset_mock()
        args = argparse.Namespace(**{
            "space_id": "alice/my-dashboard", "hf_token": "hf_xxx",
            "api_token": "nb_test_token", "private": False, "from_source": False,
            "read": "public", "write": "private", "logdir": None,
            "wait": False, **over,
        })
        with patch.dict(sys.modules, {
            "huggingface_hub": fake_module, "huggingface_hub.utils": fake_utils,
        }):
            cmd_deploy(args)
        return {
            c.kwargs["path_in_repo"]: c.kwargs["path_or_fileobj"].decode("utf-8")
            for c in fake_api.upload_file.call_args_list
        }, {c.kwargs["key"] for c in fake_api.add_space_secret.call_args_list}

    def cmd(dockerfile):
        return next(ln for ln in dockerfile.splitlines() if ln.startswith("CMD ["))

    base = 'CMD ["nebo", "serve", "--host", "0.0.0.0", "--port", "7860"'

    # Deploys that predate bucket logdirs are untouched.
    files, secrets = run()
    assert cmd(files["Dockerfile"]) == base + ', "--remote", "/data", "--no-local"]'
    assert "Connect from Python" in files["README.md"]
    # nebo never puts an HF credential into a Space. The deploy token is
    # write-scoped and a Space secret is readable by anyone who can push
    # there, so a private archive is configured by hand in Space settings.
    assert secrets == {"NEBO_API_TOKEN"}

    # With a bucket: watcher on, no network intake, and the Space page stops
    # telling visitors to push runs it would reject. The install line must
    # carry [deploy] or the daemon cannot import huggingface_hub and the
    # Space never boots.
    files, secrets = run(logdir="hf://buckets/acme/runs")
    assert secrets == {"NEBO_API_TOKEN"}
    assert cmd(files["Dockerfile"]) == base + ', "--logdir", "hf://buckets/acme/runs"]'
    # The Space page must say the archive is read-only and show how to publish.
    assert "only reads" in files["README.md"]
    assert "sync_bucket" in files["README.md"]
    assert "hf://buckets/acme/runs" in files["README.md"]
    assert "NEBO_GROUP" in files["README.md"]
    assert "nebo[deploy]" in files["Dockerfile"]

    # A Space has no durable local disk to point at.
    for bad in ("/var/lib/nebo", "hf://buckets/acme", "hf://buckets/a/b@rev"):
        with pytest.raises(SystemExit):
            run(logdir=bad)
