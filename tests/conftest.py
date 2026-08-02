"""Shared pytest configuration.

Suite-wide invariants:
* NEBO_NO_STORE=1 — SDK file mode opens no file, so tests don't litter
  the working dir. (A bare DaemonState() defaults to remote-ephemeral, so
  daemon-side ingest also persists nothing unless a test opts in.)
* NEBO_QUIET=1 — suppress the startup banner so pytest's stdout capture
  stays focused on what each test prints.
"""

from __future__ import annotations

import builtins
import contextlib
import sys

import pytest


@pytest.fixture(autouse=True)
def _quiet_nebo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEBO_NO_STORE", "1")
    monkeypatch.setenv("NEBO_QUIET", "1")


@contextlib.contextmanager
def blocked_import(prefix: str):
    """Make `import <prefix>` (and submodules) raise ImportError.

    Simulates an environment where an optional dependency (Pillow, httpx)
    is not installed, so tests can prove nebo works without it.
    """
    saved = {
        name: sys.modules.pop(name)
        for name in list(sys.modules)
        if name == prefix or name.startswith(prefix + ".")
    }
    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name == prefix or name.startswith(prefix + "."):
            raise ImportError(f"import of {name!r} blocked by test")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = _blocked
    try:
        yield
    finally:
        builtins.__import__ = real_import
        sys.modules.update(saved)


@pytest.fixture
def block_import():
    """`blocked_import` as a fixture (conftest isn't importable under
    pytest 9's default importlib mode)."""
    return blocked_import


class CapturingClient:
    """Stand-in transport used by tests that assert on the SDK's wire output."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def send_event(self, event: dict) -> None:
        self.events.append(event)

    def flush(self, timeout: float = 5.0) -> bool:
        return True

    def close(self) -> None:
        pass

    def is_connected(self) -> bool:
        return True

    def by_type(self, event_type: str) -> list[dict]:
        return [e for e in self.events if e.get("type") == event_type]

    def metrics_named(self, name: str) -> list[dict]:
        return [
            e for e in self.events
            if e.get("type") == "metric" and e.get("name") == name
        ]


@pytest.fixture
def capturing_client():
    from nebo.core.state import get_state, SessionState

    SessionState.reset_singleton()
    client = CapturingClient()
    get_state()._transport = client
    try:
        yield client
    finally:
        get_state()._transport = None
        SessionState.reset_singleton()
