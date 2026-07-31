import threading
import time
from pathlib import Path

from nebo.core.fileformat import NeboFileReader
from nebo.core.transport import FileTransport, Transport


def _read_entries(path: Path) -> list[dict]:
    with path.open("rb") as f:
        reader = NeboFileReader(f)
        reader.read_header()
        return list(reader.read_entries())


def test_transport_is_protocol():
    class DuckTransport:
        def send_event(self, event: dict) -> None: ...
        def flush(self, timeout: float = 5.0) -> bool: return True
        def close(self) -> None: ...

    assert isinstance(DuckTransport(), Transport)


def test_transport_missing_method_fails_isinstance():
    class IncompleteTransport:
        def send_event(self, event: dict) -> None: ...

    assert not isinstance(IncompleteTransport(), Transport)


def test_file_transport_satisfies_protocol(tmp_path):
    t = FileTransport(logdir=tmp_path, run_id="abc123def456", script_path="/x/s.py")
    try:
        assert isinstance(t, Transport)
    finally:
        t.close()


def test_file_transport_writes_header_and_events(tmp_path):
    t = FileTransport(logdir=tmp_path, run_id="abc123def456", script_path="/x/s.py")
    try:
        t.send_event({"type": "text", "loggable_id": "__global__", "message": "hi"})
        t.send_event({"type": "text", "loggable_id": "__global__", "message": "bye"})
        assert t.flush(timeout=2.0)
    finally:
        t.close()

    files = list(tmp_path.glob("*.nebo"))
    assert len(files) == 1
    assert "abc123def456" in files[0].name

    with files[0].open("rb") as f:
        reader = NeboFileReader(f)
        meta = reader.read_header()
        assert meta["run_id"] == "abc123def456"
        assert meta["script_path"] == "/x/s.py"
        entries = list(reader.read_entries())

    text_msgs = [
        e["payload"].get("message")
        for e in entries
        if e["type"] == "text"
    ]
    assert text_msgs == ["hi", "bye"]


def test_file_transport_seeds_global_and_agent_loggables(tmp_path):
    """FileTransport mirrors what the daemon does on run_start: emit
    loggable_register entries for __global__ and __agent__ so file readers
    see the same shape they'd see for a daemon-written file.
    """
    t = FileTransport(logdir=tmp_path, run_id="aaa111bbb222", script_path="/x/s.py")
    try:
        assert t.flush(timeout=2.0)
    finally:
        t.close()

    file = next(tmp_path.glob("*.nebo"))
    with file.open("rb") as f:
        reader = NeboFileReader(f)
        reader.read_header()
        entries = list(reader.read_entries())

    seeded = [
        e["payload"]["data"]["loggable_id"]
        for e in entries
        if e["type"] == "loggable_register"
    ]
    assert "__global__" in seeded
    assert "__agent__" in seeded


def test_file_transport_rolls_on_new_run(tmp_path, monkeypatch):
    """nb.init() + two start_run() blocks in file mode produces exactly
    two .nebo files (one per start_run). No orphan init-time file —
    init() is plumbing-only now; the run materializes when start_run
    or the first emit kicks in.
    """
    import nebo as nb
    from nebo.core.state import SessionState

    monkeypatch.setenv("NEBO_QUIET", "1")
    monkeypatch.delenv("NEBO_NO_STORE", raising=False)
    monkeypatch.chdir(tmp_path)

    SessionState.reset_singleton()
    nb._auto_init_done = False
    try:
        nb.init(uri=str(tmp_path / "runs"))
        with nb.start_run() as r1:
            nb.log_text("text", "first run log")
        with nb.start_run() as r2:
            nb.log_text("text", "second run log")
        nb.flush(timeout=2.0)
    finally:
        state = nb.get_state()
        if state._transport is not None:
            state._transport.close()
        SessionState.reset_singleton()
        nb._auto_init_done = False

    files = sorted((tmp_path / "runs").glob("*.nebo"))
    run_ids_in_filenames = {f.stem.split("_")[-1] for f in files}
    assert r1.run_id in run_ids_in_filenames, f"r1 file missing; got {[f.name for f in files]}"
    assert r2.run_id in run_ids_in_filenames, f"r2 file missing; got {[f.name for f in files]}"
    assert len(files) == 2, (
        f"expected exactly 2 files (r1 + r2) — no orphan init file; "
        f"got {[f.name for f in files]}"
    )


def test_file_transport_coalesces_metric_events(tmp_path):
    t = FileTransport(logdir=tmp_path, run_id="coalescerun1", script_path="/x/s.py")

    def _pt(name, value, step):
        return {
            "type": "metric", "loggable_id": "a", "name": name,
            "metric_type": "line", "value": value, "step": step,
            "tags": [], "timestamp": 100.0 + step,
        }

    try:
        for i in range(5):
            t.send_event(_pt("loss", 0.5 - i * 0.1, i))
            t.send_event(_pt("acc", 0.1 + i * 0.1, i))
        assert t.flush(timeout=2.0)
    finally:
        t.close()

    (path,) = tmp_path.glob("*.nebo")
    with path.open("rb") as f:
        reader = NeboFileReader(f)
        reader.read_header()
        entries = list(reader.read_entries())

    batches = [e for e in entries if e["type"] == "metric_batch"]
    singles = [e for e in entries if e["type"] == "metric"]
    # 10 interleaved points -> exactly 2 columnar frames, 0 plain metrics.
    assert len(singles) == 0
    total_points = sum(len(b["payload"]["steps"]) for b in batches)
    assert total_points == 10
    by_name = {}
    for b in batches:
        by_name.setdefault(b["payload"]["name"], []).extend(b["payload"]["steps"])
    assert by_name == {"loss": [0, 1, 2, 3, 4], "acc": [0, 1, 2, 3, 4]}


def test_file_transport_writes_media_bytes(tmp_path):
    raw = b"\x89PNG\r\n\x1a\n" + b"q" * 32
    t = FileTransport(logdir=tmp_path, run_id="mediabytes01", script_path="/x/s.py")
    try:
        t.send_event({
            "type": "image", "loggable_id": "a", "name": "f",
            "data": raw, "step": None, "timestamp": 1.0,
        })
        assert t.flush(timeout=2.0)
    finally:
        t.close()

    (path,) = tmp_path.glob("*.nebo")
    with path.open("rb") as f:
        reader = NeboFileReader(f)
        reader.read_header()
        (img,) = [e for e in reader.read_entries() if e["type"] == "image"]
    assert img["payload"]["data"] == raw


def test_file_transport_flush_means_on_disk(tmp_path):
    t = FileTransport(logdir=tmp_path, run_id="flushcheck01", script_path="/x/s.py")
    try:
        t.send_event({"type": "text", "loggable_id": "__global__", "message": "x"})
        assert t.flush(timeout=2.0)
        # Without closing, another reader must already see the entry.
        (path,) = tmp_path.glob("*.nebo")
        with path.open("rb") as f:
            reader = NeboFileReader(f)
            reader.read_header()
            msgs = [
                e["payload"].get("message")
                for e in reader.read_entries()
                if e["type"] == "text"
            ]
        assert "x" in msgs
    finally:
        t.close()


def test_file_transport_close_drains_slow_encode_backlog(tmp_path, monkeypatch):
    """Regression (v0.3.0 deferred media encoding): close() must drain the
    full queue even when per-event encoding makes the backlog outlive any
    fixed join deadline. The old close() joined for 5 s and then closed the
    stream under the still-encoding worker, silently truncating the run.

    300 events x 20 ms simulated encode = ~6 s of drain work, deliberately
    past the old 5 s join budget. Slow by design — it IS the repro.
    """
    import nebo.logging.serializers as serializers

    real_resolve = serializers.resolve_media

    def slow_resolve(event):
        time.sleep(0.02)
        return real_resolve(event)

    # Patch before constructing the transport: the worker thread binds
    # resolve_media when it starts.
    monkeypatch.setattr(serializers, "resolve_media", slow_resolve)

    t = FileTransport(logdir=tmp_path, run_id="slowencode01", script_path="/x/s.py")
    n = 300
    for i in range(n):
        t.send_event({"type": "text", "loggable_id": "__global__", "message": f"m{i}"})
    t.close()

    (path,) = tmp_path.glob("*.nebo")
    msgs = [e["payload"].get("message") for e in _read_entries(path) if e["type"] == "text"]
    assert len(msgs) == n, f"close() truncated the backlog: {len(msgs)}/{n} written"


def test_file_transport_close_sync_drains_when_worker_dead(tmp_path):
    """If the writer thread died, close() must drain the queue synchronously
    on the calling thread instead of silently discarding it."""
    t = FileTransport(logdir=tmp_path, run_id="deadworker01", script_path="/x/s.py")
    try:
        # Simulate a crashed worker: swap in an already-finished thread while
        # the transport still believes it is running.
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        t._running = False
        t._queue.put(None)
        t._thread.join(timeout=5.0)
        t._running = True
        t._thread = dead

        t._queue.put({"type": "text", "loggable_id": "__global__", "message": "orphan1"})
        t._queue.put({"type": "text", "loggable_id": "__global__", "message": "orphan2"})
    finally:
        t.close()

    (path,) = tmp_path.glob("*.nebo")
    msgs = [e["payload"].get("message") for e in _read_entries(path) if e["type"] == "text"]
    assert msgs == ["orphan1", "orphan2"]


def test_file_transport_atexit_drains_after_context_exit(tmp_path):
    """The atexit handler must still drain + close the transport when
    start_run's __exit__ already sent run_completed (flag set). The old
    guard early-returned entirely, so events logged after the with-block
    could be lost and the file never finalized. Only the duplicate
    run_completed emission should be suppressed."""
    t = FileTransport(logdir=tmp_path, run_id="postblock001", script_path="/x/s.py")
    # Mirror _RunContext.__exit__: run_completed sent, flag flipped.
    t.send_event({"type": "run_completed", "data": {"timestamp": 1.0}})
    assert t.flush(timeout=5.0)
    t._run_completed_sent = True
    # Events after the with-block, then process exit.
    t.send_event({"type": "text", "loggable_id": "__global__", "message": "tail"})
    t._emit_run_completed_atexit()

    assert not t._running, "atexit must close the transport"
    (path,) = tmp_path.glob("*.nebo")
    entries = _read_entries(path)
    completed = [e for e in entries if e["type"] == "run_completed"]
    msgs = [e["payload"].get("message") for e in entries if e["type"] == "text"]
    assert len(completed) == 1, "run_completed must not be duplicated"
    assert msgs == ["tail"], "post-block events must be drained at exit"


def test_file_transport_resolves_pending_media(tmp_path):
    import numpy as np
    from nebo.logging.serializers import prepare_image

    t = FileTransport(logdir=tmp_path, run_id="pendingmedia", script_path="/x/s.py")
    try:
        t.send_event({
            "type": "image", "loggable_id": "a", "name": "f",
            "data": prepare_image(np.zeros((6, 6, 3), dtype=np.uint8)),
            "step": None, "timestamp": 1.0,
        })
        assert t.flush(timeout=2.0)
    finally:
        t.close()

    (path,) = tmp_path.glob("*.nebo")
    with path.open("rb") as f:
        reader = NeboFileReader(f)
        reader.read_header()
        (img,) = [e for e in reader.read_entries() if e["type"] == "image"]
    assert isinstance(img["payload"]["data"], bytes)
    assert img["payload"]["data"].startswith(b"\x89PNG")
