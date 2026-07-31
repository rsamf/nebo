"""Tests for the DaemonClient."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import msgpack
import pytest

from nebo.core.client import NetworkTransport as DaemonClient


class TestDaemonClient:
    """Tests for the SDK daemon client."""

    def test_init_defaults(self) -> None:
        """Should initialize with default host/port."""
        client = DaemonClient()
        assert client._host == "localhost"
        assert client._port == 7861
        assert client._connected is False

    def test_connect_fails_when_no_server(self) -> None:
        """Should return False when daemon is not running."""
        client = DaemonClient(port=19999)  # unlikely to be running
        assert client.connect() is False
        assert client.is_connected() is False

    def test_send_event_buffers_when_disconnected(self) -> None:
        """Should buffer events in fallback buffer when not connected."""
        client = DaemonClient(port=19999)
        client.send_event({"type": "text", "message": "buffered"})
        assert len(client._fallback_buffer) == 1
        assert client._fallback_buffer[0]["message"] == "buffered"

    def test_send_events_multiple(self) -> None:
        """Should buffer multiple events."""
        client = DaemonClient(port=19999)
        client.send_events([
            {"type": "text", "message": "a"},
            {"type": "text", "message": "b"},
        ])
        assert len(client._fallback_buffer) == 2

    def test_disconnect_safe_when_not_connected(self) -> None:
        """Should not crash when disconnecting without connection."""
        client = DaemonClient(port=19999)
        client.disconnect()  # should not raise

    def test_post_packed_uses_events_endpoint_with_auth_header(self) -> None:
        """Regression: when an api_token is set, `_post_packed` must POST
        to /events with `X-Nebo-Token` — not to a separate /r/v1
        envelope endpoint. The HF-Spaces deploy in 2026-05 caught the
        old behaviour silently 404ing on `/r/v1`."""
        client = DaemonClient(
            base_url="https://example.test", api_token="tok123",
        )
        captured: dict[str, Any] = {}

        class FakeConn:
            def request(self, method, path, body=None, headers=None):
                captured.update(method=method, path=path, body=body, headers=headers)

            def getresponse(self):
                class R:
                    status = 200

                    def read(self):
                        return b"{}"

                return R()

            def close(self):
                pass

        client._connection = lambda: FakeConn()  # type: ignore[method-assign]
        events = [{"type": "text", "message": "hi"}]
        packed = [msgpack.packb(e, use_bin_type=True) for e in events]
        ok, exc = client._post_packed(events, packed)

        assert ok is True and exc is None
        assert captured["path"].startswith("/events")
        assert captured["headers"]["X-Nebo-Token"] == "tok123"
        # Body is the concatenation of packed event maps, no envelope.
        unpacker = msgpack.Unpacker(raw=False)
        unpacker.feed(captured["body"])
        assert list(unpacker)[0]["message"] == "hi"


class TestChunkPacked:
    def test_splits_by_byte_cap(self) -> None:
        events = [{"data": "x" * 300_000} for _ in range(10)]
        packed = [msgpack.packb(e, use_bin_type=True) for e in events]
        chunks = DaemonClient._chunk_packed(events, packed, max_bytes=1_000_000)

        for ev_chunk, pk_chunk in chunks:
            size = sum(len(p) for p in pk_chunk)
            assert len(ev_chunk) == 1 or size <= 1_000_000

        flat = [e for ev_chunk, _ in chunks for e in ev_chunk]
        assert flat == events

    def test_oversize_event_is_own_chunk(self) -> None:
        big = {"data": "x" * 3_000_000}
        packed = [msgpack.packb(big, use_bin_type=True)]
        chunks = DaemonClient._chunk_packed([big], packed, max_bytes=1_000_000)
        assert chunks == [([big], packed)]

    def test_empty_input_returns_empty_list(self) -> None:
        assert DaemonClient._chunk_packed([], [], max_bytes=1_000_000) == []


class TestDrainQueueIntoBuffer:
    def test_moves_all_queued_events_into_buffer(self) -> None:
        client = DaemonClient()
        client._queue.put({"e": 1})
        client._queue.put({"e": 2})
        client._queue.put({"e": 3})

        client._drain_queue_into_buffer()

        assert client._queue.empty()
        assert client._buffer == [{"e": 1}, {"e": 2}, {"e": 3}]

    def test_appends_to_existing_buffer(self) -> None:
        client = DaemonClient()
        client._buffer = [{"e": 0}]
        client._queue.put({"e": 1})

        client._drain_queue_into_buffer()

        assert client._buffer == [{"e": 0}, {"e": 1}]

    def test_no_op_on_empty_queue(self) -> None:
        client = DaemonClient()
        client._drain_queue_into_buffer()
        assert client._buffer == []


class TestPreparePackedQuarantine:
    """`_prepare_packed` underpins the poison-batch quarantine.

    Without it, a single un-serializable event (e.g. a `set` value snuck
    into a payload via `@nb.fn(ui={"a", "b"})`) re-buffers forever and
    every subsequent event is buried behind it.
    """

    def test_all_serializable_passes_through(self) -> None:
        client = DaemonClient()
        events = [{"e": 1}, {"e": 2}, {"e": 3}]
        good, packed, bad = client._prepare_packed(events)
        assert good == events
        assert len(packed) == 3
        assert bad == []

    def test_separates_set_value(self) -> None:
        client = DaemonClient()
        events = [
            {"type": "text", "id": 1},
            {"type": "loggable_register", "ui_hints": {"default_tab", "metrics"}},
            {"type": "text", "id": 2},
        ]
        good, packed, bad = client._prepare_packed(events)
        assert good == [{"type": "text", "id": 1}, {"type": "text", "id": 2}]
        assert len(packed) == 2
        assert len(bad) == 1
        assert bad[0]["ui_hints"] == {"default_tab", "metrics"}


class TestShutdownDrainsFallbackBuffer:
    """Regression tests for silent fallback-buffer loss at shutdown.

    Prior behaviour: `_flush_remaining` only drained `_queue` + `_buffer`.
    Events parked in `_fallback_buffer` (queued while disconnected, or
    moved there by `_handle_disconnect`) were discarded at exit with no
    send attempt and no warning — `DrainResult.dropped` read 0.
    """

    def test_disconnect_attempts_to_send_fallback_buffer(self) -> None:
        client = DaemonClient(port=19999)
        client.send_event({"type": "text", "message": "parked1"})
        client.send_event({"type": "text", "message": "parked2"})
        assert len(client._fallback_buffer) == 2

        sent: list[dict[str, Any]] = []

        def fake_post(events, packed):
            sent.extend(events)
            return True, None

        client._post_packed = fake_post  # type: ignore[method-assign]
        client.disconnect()

        msgs = [e.get("message") for e in sent]
        assert msgs == ["parked1", "parked2"]
        assert client._fallback_buffer == []

    def test_disconnect_warns_when_fallback_undeliverable(self, capsys) -> None:
        client = DaemonClient(port=19999)
        client._shutdown_timeout = 0.3
        client.send_event({"type": "text", "message": "doomed"})

        def fake_post(events, packed):
            return False, RuntimeError("still down")

        client._post_packed = fake_post  # type: ignore[method-assign]
        client.disconnect()

        err = capsys.readouterr().err
        assert "WARNING" in err
        assert "dropped 1 event" in err


class TestDoFlushQuarantine:
    """Regression tests for the poison-batch bug.

    Prior behaviour: `_post_batch` raised TypeError on `json.dumps`,
    `_do_flush` re-buffered the entire batch via `self._buffer = batch +
    self._buffer`, the next flush failed identically, and every event
    queued after the bad one was lost forever — no warning, no error.
    """

    def test_unserializable_event_does_not_block_good_events(self) -> None:
        client = DaemonClient()
        client._buffer = [
            {"type": "text", "id": 1},
            {"type": "loggable_register", "ui_hints": {"a", "b"}},
            {"type": "text", "id": 2},
        ]
        sent_batches: list[list[dict[str, Any]]] = []

        def fake_post(events, packed):
            sent_batches.append(events)
            return True, None

        client._post_packed = fake_post  # type: ignore[method-assign]

        ok = client._do_flush()

        assert ok is True
        flat = [e for b in sent_batches for e in b]
        assert flat == [
            {"type": "text", "id": 1},
            {"type": "text", "id": 2},
        ]
        assert client._buffer == []

    def test_all_unserializable_returns_success_without_post(self) -> None:
        client = DaemonClient()
        client._buffer = [{"type": "x", "v": {"a"}}]
        called: list[Any] = []

        def fake_post(events, packed):
            called.append(events)
            return True, None

        client._post_packed = fake_post  # type: ignore[method-assign]

        ok = client._do_flush()

        assert ok is True
        assert called == []
        assert client._buffer == []

    def test_warning_logged_when_dropping_unserializable(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = DaemonClient()
        client._buffer = [{"type": "loggable_register", "v": {"a"}}]
        client._post_packed = lambda events, packed: (True, None)  # type: ignore[method-assign]

        with caplog.at_level(logging.WARNING, logger="nebo.core.client"):
            client._do_flush()

        assert any(
            "un-serializable" in rec.getMessage() for rec in caplog.records
        ), f"expected un-serializable warning, got {[r.getMessage() for r in caplog.records]}"

    def test_network_failure_re_buffers_only_good_events(self) -> None:
        """Bad events MUST NOT be re-buffered on network failure — that's
        exactly how the poison loop happens.
        """
        client = DaemonClient()
        client._buffer = [
            {"type": "text", "id": 1},
            {"type": "x", "v": {"a"}},
            {"type": "text", "id": 2},
        ]
        client._post_packed = lambda events, packed: (False, RuntimeError("net"))  # type: ignore[method-assign]

        ok = client._do_flush()

        assert ok is False
        assert client._buffer == [
            {"type": "text", "id": 1},
            {"type": "text", "id": 2},
        ]


class TestDrainWithRetryQuarantine:
    """`_drain_with_retry` is the shutdown / explicit-flush path. It must
    also keep moving past un-serializable events. Prior behaviour was
    even worse than `_do_flush`: `_chunk_buffer` calls `json.dumps` per
    event with no exception handling, so a poisoned buffer would raise
    uncaught from inside the atexit drain.
    """

    def test_drains_around_unserializable_event(self) -> None:
        client = DaemonClient()
        client._buffer = [
            {"type": "text", "id": 1},
            {"type": "x", "v": {"a", "b"}},
            {"type": "text", "id": 2},
        ]
        sent: list[list[dict[str, Any]]] = []

        def fake_post(events, packed):
            sent.append(events)
            return True, None

        client._post_packed = fake_post  # type: ignore[method-assign]

        deadline = time.monotonic() + 5.0
        result = client._drain_with_retry(deadline)

        flat = [e for batch in sent for e in batch]
        assert flat == [
            {"type": "text", "id": 1},
            {"type": "text", "id": 2},
        ]
        assert result.sent == 2
        assert result.dropped == 0
        assert client._buffer == []

    def test_all_unserializable_buffer_drains_to_empty(self) -> None:
        client = DaemonClient()
        client._buffer = [{"v": {"a"}}, {"v": frozenset()}]
        called: list[Any] = []

        def fake_post(events, packed):
            called.append(events)
            return True, None

        client._post_packed = fake_post  # type: ignore[method-assign]

        deadline = time.monotonic() + 5.0
        result = client._drain_with_retry(deadline)

        assert called == []
        assert result.sent == 0
        assert result.dropped == 0
        assert client._buffer == []


class TestDrainWithRetry:
    def test_succeeds_on_first_try(self) -> None:
        client = DaemonClient()
        client._buffer = [{"e": 1}, {"e": 2}]
        sent_batches: list[list[dict[str, Any]]] = []

        def fake_post(events, packed):
            sent_batches.append(events)
            return True, None

        client._post_packed = fake_post  # type: ignore[method-assign]

        deadline = time.monotonic() + 5.0
        result = client._drain_with_retry(deadline)

        assert result.sent == 2
        assert result.dropped == 0
        assert result.last_error is None
        assert client._buffer == []
        assert sent_batches == [[{"e": 1}, {"e": 2}]]

    def test_succeeds_after_transient_failure(self) -> None:
        client = DaemonClient()
        client._buffer = [{"e": 1}, {"e": 2}]
        calls = {"n": 0}

        def fake_post(events, packed):
            calls["n"] += 1
            if calls["n"] == 1:
                return False, RuntimeError("transient")
            return True, None

        client._post_packed = fake_post  # type: ignore[method-assign]

        deadline = time.monotonic() + 5.0
        result = client._drain_with_retry(deadline)

        assert result.sent == 2
        assert result.dropped == 0
        assert result.last_error is None
        assert client._buffer == []
        assert calls["n"] == 2

    def test_returns_dropped_after_deadline(self) -> None:
        client = DaemonClient()
        client._buffer = [{"e": 1}, {"e": 2}]

        def fake_post(events, packed):
            return False, RuntimeError("permanent")

        client._post_packed = fake_post  # type: ignore[method-assign]

        deadline = time.monotonic() + 0.3
        result = client._drain_with_retry(deadline)

        assert result.sent == 0
        assert result.dropped == 2
        assert result.dropped_bytes > 0
        assert result.last_error is not None
        assert "permanent" in result.last_error
        assert client._buffer == [{"e": 1}, {"e": 2}]

    def test_drains_newly_queued_events_in_same_call(self) -> None:
        client = DaemonClient()
        client._queue.put({"e": 1})
        first_call = {"done": False}

        def fake_post(events, packed):
            if not first_call["done"]:
                first_call["done"] = True
                client._queue.put({"e": 2})
            return True, None

        client._post_packed = fake_post  # type: ignore[method-assign]

        deadline = time.monotonic() + 5.0
        result = client._drain_with_retry(deadline)

        assert result.sent == 2
        assert client._buffer == []

    def test_chunks_oversized_buffer(self) -> None:
        client = DaemonClient()
        client._buffer = [{"data": "x" * 300_000} for _ in range(10)]

        sent_batches: list[list[dict[str, Any]]] = []

        def fake_post(events, packed):
            sent_batches.append(events)
            return True, None

        client._post_packed = fake_post  # type: ignore[method-assign]

        deadline = time.monotonic() + 5.0
        result = client._drain_with_retry(deadline)

        assert result.sent == 10
        assert result.dropped == 0
        assert len(sent_batches) >= 2


class TestShutdownTimeout:
    def test_default_is_ten_seconds(self) -> None:
        client = DaemonClient()
        assert client._shutdown_timeout == 10.0

    def test_constructor_arg_overrides_default(self) -> None:
        client = DaemonClient(shutdown_timeout=2.5)
        assert client._shutdown_timeout == 2.5

    def test_env_var_overrides_constructor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NEBO_SHUTDOWN_TIMEOUT", "0.5")
        client = DaemonClient(shutdown_timeout=99.0)
        assert client._shutdown_timeout == 0.5

    def test_invalid_env_value_falls_back_to_constructor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NEBO_SHUTDOWN_TIMEOUT", "not-a-number")
        client = DaemonClient(shutdown_timeout=7.0)
        assert client._shutdown_timeout == 7.0


class TestFlushTimeout:
    def test_returns_true_on_success(self) -> None:
        client = DaemonClient()
        client._buffer = [{"e": 1}]
        client._post_packed = lambda events, packed: (True, None)  # type: ignore[method-assign]

        assert client.flush(timeout=1.0) is True
        assert client._buffer == []

    def test_returns_false_when_dropped(self) -> None:
        client = DaemonClient()
        client._buffer = [{"e": 1}]
        client._post_packed = lambda events, packed: (False, RuntimeError("nope"))  # type: ignore[method-assign]

        assert client.flush(timeout=0.2) is False
        assert client._buffer == [{"e": 1}]

    def test_default_timeout_is_finite(self) -> None:
        """Calling flush() with no args should not hang forever — returns
        within the default budget (~5 s + slack)."""
        client = DaemonClient()
        client._buffer = [{"e": 1}]
        client._post_packed = lambda events, packed: (False, RuntimeError("nope"))  # type: ignore[method-assign]

        start = time.monotonic()
        result = client.flush()
        elapsed = time.monotonic() - start

        assert result is False
        assert elapsed < 6.0


class TestFlushRemainingWarning:
    def test_silent_on_full_drain(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = DaemonClient(shutdown_timeout=1.0)
        client._buffer = [{"e": 1}]
        client._post_packed = lambda events, packed: (True, None)  # type: ignore[method-assign]

        client._flush_remaining()

        captured = capsys.readouterr()
        assert captured.err == ""
        assert client._buffer == []

    def test_warns_on_dropped_events(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = DaemonClient(shutdown_timeout=0.1)
        client._buffer = [{"e": 1}, {"e": 2}]
        client._post_packed = lambda events, packed: (False, RuntimeError("nope"))  # type: ignore[method-assign]

        client._flush_remaining()

        captured = capsys.readouterr()
        assert "nebo: WARNING" in captured.err
        assert "dropped 2 event" in captured.err
        assert "KB" in captured.err
        assert "nope" in captured.err

    def test_warning_includes_timeout_value(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = DaemonClient(shutdown_timeout=0.05)
        client._buffer = [{"e": 1}]
        client._post_batch = lambda batch: (False, RuntimeError("x"))  # type: ignore[method-assign]

        client._flush_remaining()

        captured = capsys.readouterr()
        assert "0.05" in captured.err


class TestModeDetection:
    """Tests for mode detection in nb.init()."""

    def setup_method(self) -> None:
        import nebo as nb
        from nebo.core.state import SessionState
        SessionState.reset_singleton()
        nb._auto_init_done = False

    def test_file_mode_when_no_daemon(self) -> None:
        """Should use file mode (default) when no uri given."""
        import nebo as nb
        # Default uri=None -> file mode
        nb.init()

        from nebo.core.state import get_state
        state = get_state()
        assert state._mode == "file"

    def test_network_mode_falls_back_when_no_daemon(self) -> None:
        """Should stay in network mode even when daemon unreachable (buffering)."""
        import nebo as nb
        nb.init(uri="localhost:19999")  # unlikely to be running

        from nebo.core.state import get_state
        state = get_state()
        assert state._mode == "network"


class _FakeClient:
    """In-memory stand-in for DaemonClient used to capture events."""

    def __init__(self, host=None, port=None, run_id=None, flush_interval=None,  # noqa: ARG002
                 base_url=None, api_token=None):  # noqa: ARG002
        self._run_id = run_id
        self._connected = False
        self.events: list[dict] = []

    def connect(self) -> bool:
        self._connected = True
        return True

    def is_connected(self) -> bool:
        return self._connected

    def send_event(self, event: dict) -> None:
        self.events.append(event)

    def send_events(self, events) -> None:
        self.events.extend(events)

    def flush(self) -> None:
        pass

    def disconnect(self) -> None:
        self._connected = False

    def get(self, path):  # pragma: no cover
        return None


class TestRunStartEmission:
    """Regression tests for `run_start` event emission in nb.init().
    """

    def setup_method(self) -> None:
        import os
        import nebo as nb
        from nebo.core.state import SessionState

        SessionState.reset_singleton()
        nb._auto_init_done = False
        self._saved_env = {
            k: os.environ.pop(k, None)
            for k in ["NEBO_URI", "NEBO_RUN_ID", "NEBO_FLUSH_INTERVAL"]
        }
        self._captured: list[_FakeClient] = []

    def teardown_method(self) -> None:
        import os
        for k, v in self._saved_env.items():
            if v is not None:
                os.environ[k] = v

    def _install_fake_client(self, monkeypatch) -> None:
        import nebo.core.client as client_mod

        captured = self._captured

        class TrackingFake(_FakeClient):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                captured.append(self)

        monkeypatch.setattr(client_mod, "NetworkTransport", TrackingFake)

    def test_run_start_emitted_when_run_id_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Run start event is emitted when Run ID is defined in an environment variable.

        Since the lazy-run refactor, run_start fires when the run is
        materialized (first emit), not at nb.init() time. The env's
        NEBO_RUN_ID flows through state._pending_run_id and gets
        consumed by _ensure_run on the trigger emit.
        """
        monkeypatch.setenv("NEBO_RUN_ID", "abcdef012345")
        self._install_fake_client(monkeypatch)

        import nebo as nb
        nb.init(uri="localhost:7861")
        nb.log_text("text", "trigger materialization")

        assert len(self._captured) == 1
        client = self._captured[0]
        run_starts = [e for e in client.events if e.get("type") == "run_start"]
        assert len(run_starts) == 1, (
            f"expected exactly one run_start, got events: {client.events}"
        )
        data = run_starts[0]["data"]
        assert data.get("script_path"), "script_path must be non-empty"
        # store field is no longer emitted by the SDK (NEBO_NO_STORE env controls it)
        assert "store" not in data

    def test_run_start_emitted_without_env_run_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Direct script execution path must also emit run_start on first emit."""
        self._install_fake_client(monkeypatch)

        import nebo as nb
        nb.init(uri="localhost:7861")
        nb.log_text("text", "trigger materialization")

        assert len(self._captured) == 1
        client = self._captured[0]
        run_starts = [e for e in client.events if e.get("type") == "run_start"]
        assert len(run_starts) == 1

    def test_run_start_script_path_is_absolute(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """run_start.data.script_path must be an absolute path.

        Regression: it was previously set to `os.path.basename(sys.argv[0])`,
        which broke `nebo_restart_pipeline` — the MCP tool reads `script_path`
        from the stored run and feeds it to `run_pipeline`, which fails with
        HTTP 404 "Script not found" whenever the user invoked nebo from a
        directory other than the daemon's CWD.
        """
        import os
        import sys

        monkeypatch.setenv("NEBO_RUN_ID", "abcdef012345")
        self._install_fake_client(monkeypatch)

        # Simulate a script invoked with only a basename in argv[0]
        fake_argv = ["train.py"]
        monkeypatch.setattr(sys, "argv", fake_argv)

        import nebo as nb
        nb.init(uri="localhost:7861")
        nb.log_text("text", "trigger materialization")

        client = self._captured[0]
        run_starts = [e for e in client.events if e.get("type") == "run_start"]
        assert len(run_starts) == 1
        script_path = run_starts[0]["data"]["script_path"]
        assert os.path.isabs(script_path), (
            f"script_path must be absolute, got: {script_path!r}"
        )
        assert script_path.endswith("train.py")


class TestUiConfigEmission:
    """Regression tests for `nb.ui()` UI-config forwarding.

    `nb.ui(...)` is declarative: called before any run exists it writes
    the script-level template, and the template must reach the daemon as
    a `ui_config` event (after run_start) when the run materializes on
    the first real emit. Without the event, the daemon never learns about
    the run-level UI defaults so the web UI can't apply them.
    """

    def setup_method(self) -> None:
        import os
        import nebo as nb
        from nebo.core.state import SessionState

        SessionState.reset_singleton()
        nb._auto_init_done = False
        self._saved_env = {
            k: os.environ.pop(k, None)
            for k in ["NEBO_URI", "NEBO_RUN_ID", "NEBO_FLUSH_INTERVAL"]
        }
        self._captured: list[_FakeClient] = []

    def teardown_method(self) -> None:
        import os
        for k, v in self._saved_env.items():
            if v is not None:
                os.environ[k] = v

    def _install_fake_client(self, monkeypatch) -> None:
        import nebo.core.client as client_mod

        captured = self._captured

        class TrackingFake(_FakeClient):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                captured.append(self)

        monkeypatch.setattr(client_mod, "NetworkTransport", TrackingFake)

    def test_ui_sends_ui_config_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The declared template must be emitted as a ui_config event
        (after run_start) once the run materializes."""
        self._install_fake_client(monkeypatch)

        import nebo as nb
        nb.init(uri="localhost:7861")
        nb.ui(layout="horizontal", view="dag", minimap=True, theme="dark")
        nb.log_text("text", "materialize")  # first real event opens the run

        assert len(self._captured) == 1
        client = self._captured[0]
        ui_events = [e for e in client.events if e.get("type") == "ui_config"]
        assert len(ui_events) == 1, (
            f"expected exactly one ui_config event, got events: {client.events}"
        )
        data = ui_events[0]["data"]
        assert data["layout"] == "horizontal"
        assert data["view"] == "dag"
        assert data["minimap"] is True
        assert data["theme"] == "dark"
        # Template application rides the normal event queue: after
        # run_start, before the materializing log.
        types = [e.get("type") for e in client.events]
        assert types.index("run_start") < types.index("ui_config") < types.index("text")

    def test_ui_updates_session_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The template must land in SessionState.ui_config at
        materialization."""
        self._install_fake_client(monkeypatch)

        import nebo as nb
        from nebo.core.state import get_state

        nb.init(uri="localhost:7861")
        nb.ui(layout="vertical", theme="light")
        nb.log_text("text", "materialize")

        state = get_state()
        assert state.ui_config == {"layout": "vertical", "theme": "light"}

    def test_ui_omits_unspecified_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fields left as None must not appear in the emitted ui_config event."""
        self._install_fake_client(monkeypatch)

        import nebo as nb
        nb.init(uri="localhost:7861")
        nb.ui(minimap=False)
        nb.log_text("text", "materialize")

        client = self._captured[0]
        ui_events = [e for e in client.events if e.get("type") == "ui_config"]
        assert len(ui_events) == 1
        data = ui_events[0]["data"]
        assert data == {"minimap": False}




class TestMsgpackWire:
    """v4 wire: concatenated-msgpack bodies, packed once, keep-alive."""

    @staticmethod
    def _pt(name, value, step):
        return {
            "type": "metric", "loggable_id": "a", "name": name,
            "metric_type": "line", "value": value, "step": step,
            "tags": [], "timestamp": 100.0 + step,
        }

    def test_pack_each_event_exactly_once(self, monkeypatch) -> None:
        import msgpack as _msgpack

        calls = []
        real_packb = _msgpack.packb

        def counting_packb(obj, **kw):
            calls.append(obj)
            return real_packb(obj, **kw)

        monkeypatch.setattr("nebo.core.client.msgpack.packb", counting_packb)
        client = DaemonClient()
        posted = []
        client._post_packed = lambda events, packed: (posted.extend(packed), (True, None))[1]  # type: ignore[method-assign]
        client._buffer = [self._pt("loss", 0.5, 0), self._pt("loss", 0.4, 1)]
        assert client._do_flush() is True
        # 2 points coalesce into 1 metric_batch -> exactly 1 packb call.
        assert len(calls) == 1
        assert len(posted) == 1

    def test_unpackable_event_quarantined(self, caplog) -> None:
        client = DaemonClient()
        posted = []
        client._post_packed = lambda events, packed: (posted.extend(events), (True, None))[1]  # type: ignore[method-assign]
        client._buffer = [
            {"type": "text", "message": "good"},
            {"type": "loggable_register", "ui_hints": {"a", "b"}},  # a set
            {"type": "text", "message": "also good"},
        ]
        with caplog.at_level(logging.WARNING, logger="nebo.core.client"):
            ok = client._do_flush()
        assert ok is True
        assert [e.get("message") for e in posted] == ["good", "also good"]
        assert any("un-serializable" in r.getMessage() for r in caplog.records)

    def test_chunking_by_packed_size(self) -> None:
        client = DaemonClient()
        batches = []
        client._post_packed = lambda events, packed: (batches.append(list(events)), (True, None))[1]  # type: ignore[method-assign]
        big = "x" * 700_000  # ~700KB message; two fit under the 2MB cap, three do not
        client._buffer = [
            {"type": "text", "message": big},
            {"type": "text", "message": big},
            {"type": "text", "message": big},
        ]
        assert client._do_flush() is True
        assert len(batches) == 2
        assert sum(len(b) for b in batches) == 3

    def test_media_bytes_ride_natively(self) -> None:
        import msgpack as _msgpack

        raw = b"\x89PNG\r\n\x1a\n123"
        client = DaemonClient()
        bodies = []
        client._post_packed = lambda events, packed: (bodies.append(b"".join(packed)), (True, None))[1]  # type: ignore[method-assign]
        client._buffer = [{
            "type": "image", "loggable_id": "a", "name": "f",
            "data": raw, "step": None, "timestamp": 1.0,
        }]
        assert client._do_flush() is True
        unpacker = _msgpack.Unpacker(raw=False)
        unpacker.feed(bodies[0])
        (event,) = list(unpacker)
        assert event["data"] == raw  # no base64 anywhere

    def test_wire_post_shape(self, monkeypatch) -> None:
        """_post_packed sends Content-Type: application/msgpack with the
        concatenated body over a persistent connection."""
        client = DaemonClient()
        seen = {}

        class FakeConn:
            def __init__(self):
                self.requests = 0
            def request(self, method, path, body=None, headers=None):
                self.requests += 1
                seen.update(method=method, path=path, body=body, headers=headers)
            def getresponse(self):
                class R:
                    status = 200
                    def read(self):
                        return b"{}"
                return R()
            def close(self):
                pass

        fake = FakeConn()
        monkeypatch.setattr(client, "_connection", lambda: fake)
        events = [{"type": "text", "message": "hi"}]
        import msgpack as _msgpack

        packed = [_msgpack.packb(e, use_bin_type=True) for e in events]
        ok, exc = client._post_packed(events, packed)
        assert ok is True and exc is None
        assert seen["headers"]["Content-Type"] == "application/msgpack"
        assert seen["body"] == b"".join(packed)
        # Second post reuses the same connection (keep-alive).
        client._post_packed(events, packed)
        assert fake.requests == 2


def test_prepare_packed_resolves_pending_media() -> None:
    import numpy as np

    from nebo.logging.serializers import prepare_image

    client = DaemonClient()
    events, packed, bad = client._prepare_packed([{
        "type": "image", "loggable_id": "a", "name": "f",
        "data": prepare_image(np.zeros((4, 4, 3), dtype=np.uint8)),
        "step": None, "timestamp": 1.0,
    }])
    assert bad == []
    assert isinstance(events[0]["data"], bytes)  # encoded off the caller path
    assert events[0]["data"].startswith(b"\x89PNG")
    assert len(packed) == 1


class TestBufferBudget:
    def _client(self, budget_bytes: int) -> DaemonClient:
        client = DaemonClient()
        client._buffer_budget = budget_bytes
        client._connected = True  # route send_event to the queue
        return client

    @staticmethod
    def _media(n: int) -> dict[str, Any]:
        return {"type": "image", "loggable_id": "a", "name": "f",
                "data": b"x" * n, "step": None, "timestamp": 1.0}

    def test_over_budget_drops_data_but_not_structural(self, caplog) -> None:
        client = self._client(budget_bytes=1000)
        with caplog.at_level(logging.WARNING, logger="nebo.core.client"):
            client.send_event(self._media(500))   # admitted
            client.send_event(self._media(5000))  # over budget -> dropped
            client.send_event({"type": "run_completed", "data": {}})  # structural
        assert client._queue.qsize() == 2
        assert client._dropped_events == 1
        assert any("over budget" in r.getMessage() for r in caplog.records)

    def test_progress_drops_before_other_data(self) -> None:
        client = self._client(budget_bytes=1000)
        client._buffered_bytes = 950  # > 90% of budget
        client.send_event({"type": "progress", "loggable_id": "a",
                           "data": {"current": 1}})
        assert client._dropped_events == 1
        assert client._queue.qsize() == 0

    def test_budget_released_after_successful_flush(self) -> None:
        client = self._client(budget_bytes=2000)
        client._post_packed = lambda events, packed: (True, None)  # type: ignore[method-assign]
        client.send_event(self._media(1500))
        assert client._buffered_bytes > 1500
        client._drain_queue_into_buffer()
        assert client._do_flush() is True
        # Budget freed: the next big event is admitted again.
        client.send_event(self._media(1500))
        assert client._queue.qsize() == 1
        assert client._dropped_events == 0

    def test_failed_flush_keeps_budget_accounted(self) -> None:
        client = self._client(budget_bytes=2000)
        client._post_packed = lambda events, packed: (False, RuntimeError("net"))  # type: ignore[method-assign]
        client.send_event(self._media(1500))
        client._drain_queue_into_buffer()
        assert client._do_flush() is False
        # Event re-buffered -> still counted -> next big event dropped.
        client.send_event(self._media(1500))
        assert client._dropped_events == 1


class TestPersistentReconnect:
    def test_flush_loop_retries_until_daemon_returns(self, monkeypatch) -> None:
        client = DaemonClient()
        client._connected = False
        client._running = True
        client._reconnect_backoff_max = 0.01
        client._reconnect_backoff_initial = 0.001
        attempts = {"n": 0}

        def fake_connect():
            attempts["n"] += 1
            if attempts["n"] < 3:
                return False
            client._connected = True
            return True

        monkeypatch.setattr(client, "connect", fake_connect)
        client._fallback_buffer = [{"type": "text", "message": "queued while down"}]
        sent: list[dict[str, Any]] = []
        client._post_packed = lambda events, packed: (sent.extend(events), (True, None))[1]  # type: ignore[method-assign]

        import threading as _threading

        t = _threading.Thread(target=client._flush_loop, daemon=True)
        t.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not sent:
            time.sleep(0.02)
        client._running = False
        t.join(timeout=2.0)

        assert attempts["n"] >= 3  # kept retrying past the old 5-attempt cap
        assert [e["message"] for e in sent] == ["queued while down"]
        assert client._fallback_buffer == []
