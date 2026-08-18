"""Workspace backends — what a ``--logdir`` string actually means.

``--logdir`` is the workspace root in every daemon mode: it anchors the
SQLite cache identity, the ``meta/`` run tree, and the directory the watcher
tails for ``.nebo`` files. Historically it was always a POSIX directory. It
can now also be a **Hugging Face archive**::

    nebo serve --logdir ./.nebo                            # local (default)
    nebo serve --logdir hf://buckets/acme/runs             # Storage Bucket
    nebo serve --logdir hf://buckets/acme/runs/docs        # ...a subdirectory
    nebo serve --logdir hf://datasets/acme/runs            # repo (versioned)
    nebo serve --logdir hf://datasets/acme/runs@main       # ...pinned revision

Prefer a **Storage Bucket**: S3-like, unversioned object storage, which is
what HF recommends for logs and artifacts. A dataset repo also works and adds
git history, at the cost of that history growing on every republish.

Either way the archive makes run data durable independently of the machine
serving it — a daemon on ephemeral infrastructure (a Hugging Face Space that
scales to zero, a CI runner, a container) can be destroyed and recreated and
still serve the same runs. ``.nebo`` files remain the sole source of truth;
the SQLite cache stays local and disposable and rebuilds on every cold start.

**A remote workspace is read-only.** A ``.nebo`` file is an append-only event
stream and object storage cannot append, so the daemon never writes to an
archive: something else publishes into it (assemble a workspace locally, sync
it up) and the daemon reads. ``LocalWorkspace`` remains fully writable.

This module is the *only* place that decides what a logdir string means.
Two rules matter:

**Normalization.** ``normalize_workspace()`` replaces every
``Path(logdir).resolve()`` that used to be applied to a logdir. A remote URI
must never be fed to ``Path.resolve()`` — ``Path("hf://x").resolve()``
silently yields ``$CWD/hf:/x``. The cache's identity is a sha1 of this
string (``cache.py:resolve_cache_path``) and a mismatch drops the whole
database, so producers must agree exactly.

**Addressing.** Every file a workspace exposes is named by a *self-describing
URI* — an absolute path locally, a full ``hf://`` URI remotely. Those strings
are persisted (``watch_files.path``, ``media.src_path``) and resolved later by
code that has no workspace in hand, so ``read_frame_bytes()`` dispatches on
the URI alone.

Backends
--------
``LocalWorkspace`` is the pre-existing behavior, kept byte-identical:
``os.scandir``, ``open()``, and the atomic tmp+fsync+``os.replace`` write the
run tree has always used.

``HFWorkspace`` is built directly on ``huggingface_hub`` (lazily imported —
it is the optional ``nebo[deploy]`` extra, and a local daemon must never
import it), and is **read-only**. Reads go through ``HfFileSystem``, which
serves ranged GETs and resolves buckets and repos alike, so both archive
kinds share one read path. ``commit()`` raises ``WorkspaceReadOnly``.
Credentials resolve explicit token -> ``HF_TOKEN`` -> cached login; read
access is all the daemon ever needs.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator, Optional, Protocol

logger = logging.getLogger(__name__)

HF_SCHEME = "hf://"

# Poll cadence per backend. A local scandir is essentially free; an HF listing
# is an HTTP round trip, so remote polls are two orders of magnitude slower
# and additionally gated behind a cheap commit-sha check (`change_token`).
LOCAL_POLL_INTERVAL = 0.5
REMOTE_POLL_INTERVAL = 30.0

# Remote reads: a tail at or under this size is fetched with one ranged GET
# into memory. Anything larger streams through fsspec's block cache instead,
# so a huge file can't be buffered whole.
REMOTE_INLINE_MAX = 32 * 1024 * 1024
REMOTE_BLOCK_SIZE = 8 * 1024 * 1024

# hf:// path segment -> huggingface_hub repo_type. A bare `hf://owner/name`
# is a model repo, matching HfFileSystem's own convention.
#
# `buckets` is the odd one out and the one to reach for: Storage Buckets are
# S3-like object storage rather than a git repo, which is what nebo actually
# wants for an archive of append-only run files. They are also unversioned, so
# a bucket URI carries no @revision.
_HF_TYPE_SEGMENTS = {
    "buckets": "bucket",
    "datasets": "dataset",
    "spaces": "space",
    "models": "model",
}
_HF_TYPE_TO_SEGMENT = {
    "bucket": "buckets",
    "dataset": "datasets",
    "space": "spaces",
    "model": "",
}

_HF_IMPORT_ERROR = (
    "huggingface_hub is required for hf:// workspaces. Install with:\n"
    "  pip install 'nebo[deploy]'\n"
    "or:\n"
    "  pip install huggingface_hub"
)


class WorkspaceError(RuntimeError):
    """A workspace URI is malformed, or its backend is unusable."""


class WorkspaceReadOnly(WorkspaceError):
    """A write was attempted against a workspace the daemon only reads.

    A `.nebo` file is an append-only event stream and object storage cannot
    append, so a bucket (or repo) workspace is an *archive*: something else
    publishes into it, and the daemon reads. Raising rather than silently
    no-oping keeps a stray write path visible."""


# ---------------------------------------------------------------------------
# URI parsing / normalization
# ---------------------------------------------------------------------------


def is_remote_uri(value: object) -> bool:
    """True if ``value`` names a remote workspace rather than a local path."""
    return isinstance(value, str) and value.strip().lower().startswith(HF_SCHEME)


@dataclass(frozen=True)
class HfRef:
    """A parsed ``hf://`` location: repo, optional revision, optional subpath."""

    repo_type: str          # "bucket" | "dataset" | "space" | "model"
    repo_id: str            # "owner/name"
    prefix: str             # "" or "sub/dir" (no leading or trailing slash)
    revision: Optional[str]

    @property
    def uri(self) -> str:
        """Canonical ``hf://`` form. Round-trips through :func:`parse_hf_uri`."""
        seg = _HF_TYPE_TO_SEGMENT[self.repo_type]
        base = f"{HF_SCHEME}{seg}/{self.repo_id}" if seg else f"{HF_SCHEME}{self.repo_id}"
        if self.revision:
            base += f"@{self.revision}"
        return f"{base}/{self.prefix}" if self.prefix else base

    @property
    def fs_path(self) -> str:
        """Path as ``HfFileSystem`` wants it (no scheme)."""
        return self.uri[len(HF_SCHEME):]

    @property
    def repo_path(self) -> str:
        """Path relative to the repo root — what ``HfApi`` calls path_in_repo."""
        return self.prefix

    def join(self, rel: str) -> "HfRef":
        """A ref for ``rel`` beneath this one."""
        rel = rel.strip("/")
        if not rel:
            return self
        prefix = f"{self.prefix}/{rel}" if self.prefix else rel
        return HfRef(self.repo_type, self.repo_id, prefix, self.revision)


def parse_hf_uri(uri: str) -> HfRef:
    """Parse ``hf://[buckets|datasets|spaces|models/]<owner>/<name>[@rev][/<prefix>]``.

    Raises :class:`WorkspaceError` on anything that isn't a complete reference
    — a half-specified location would otherwise fail much later, deep inside
    an HTTP call.
    """
    if not is_remote_uri(uri):
        raise WorkspaceError(f"not an hf:// URI: {uri!r}")
    rest = uri.strip()[len(HF_SCHEME):].strip("/")
    if not rest:
        raise WorkspaceError(f"incomplete hf:// URI: {uri!r}")

    parts = rest.split("/")
    repo_type = "model"
    if parts[0] in _HF_TYPE_SEGMENTS:
        repo_type = _HF_TYPE_SEGMENTS[parts[0]]
        parts = parts[1:]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise WorkspaceError(
            f"hf:// URI must name a repo as <owner>/<name>: {uri!r}\n"
            "  e.g. hf://datasets/acme/nebo-runs"
        )

    owner, name = parts[0], parts[1]
    prefix_parts = parts[2:]

    # A revision rides on the repo segment: `.../name@main/sub`.
    revision = None
    if "@" in name:
        name, revision = name.split("@", 1)
        if not name or not revision:
            raise WorkspaceError(f"malformed revision in hf:// URI: {uri!r}")
        if repo_type == "bucket":
            # Buckets are unversioned; HfFileSystem forces revision=None for
            # them, so an @rev here would silently address something else.
            raise WorkspaceError(
                f"buckets have no revisions: {uri!r}\n"
                "  Storage Buckets are unversioned object storage. Drop the "
                "'@' suffix, or use hf://datasets/... for a versioned repo."
            )

    prefix = "/".join(p for p in prefix_parts if p)
    return HfRef(repo_type, f"{owner}/{name}", prefix, revision)


def normalize_workspace(value: Optional[object]) -> str:
    """Canonical string form of a logdir, for identity and comparison.

    Local paths resolve to an absolute path (unchanged from the historical
    ``str(Path(x).resolve())``); remote URIs canonicalize without ever
    touching the filesystem. ``None`` maps to ``""``, matching the cache's
    pre-existing "no logdir" key.
    """
    if value is None:
        return ""
    if is_remote_uri(value):
        return parse_hf_uri(str(value)).uri
    return str(Path(str(value)).resolve())


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileStat:
    """One ``.nebo`` file as the watcher sees it.

    ``token`` is an opaque content marker (an HF blob id) used to notice a
    file whose bytes were replaced without its size changing — a same-size
    rewrite that size-based growth detection would miss. ``None`` means the
    backend has none and size is the only signal, which is the historical
    local behavior.
    """

    uri: str
    size: int
    mtime: Optional[float] = None
    token: Optional[str] = None


class Workspace(Protocol):
    """Storage operations the daemon needs from a workspace root."""

    uri: str
    is_remote: bool
    # Whether the daemon may write here. False for every archive backend: a
    # .nebo file is an append-only stream and object storage cannot append, so
    # something else publishes into the archive and the daemon reads it.
    writable: bool
    default_poll_interval: float

    def ensure_root(self) -> None: ...

    # -- .nebo files (watcher) ------------------------------------------
    def list_nebo(self) -> list[FileStat]: ...
    def stat(self, uri: str) -> Optional[FileStat]: ...
    def change_token(self) -> Optional[str]: ...
    def reader(self, uri: str, offset: int = 0) -> Any: ...
    def read_range(self, uri: str, offset: int, length: int) -> Optional[bytes]: ...

    # -- meta/ (run tree) -----------------------------------------------
    def read_bytes(self, rel: str) -> Optional[bytes]: ...
    def list_dir(self, rel: str) -> list[str]: ...
    def list_tree(self, rel: str) -> list[str]: ...
    def commit(
        self,
        adds: list[tuple[str, bytes]],
        deletes: list[str],
        *,
        message: str = "nebo: update",
    ) -> None:
        """Apply adds and deletes. Only valid when ``writable``.

        Deletes are applied first, so a move is one call. A delete path
        ending in ``/`` names a directory and removes it recursively.
        Read-only backends raise :class:`WorkspaceReadOnly`.
        """
        ...

    def move(self, src: str, dst: str) -> None:
        """Relocate a subtree. No-op if ``src`` does not exist.

        Only valid when ``writable``; read-only backends raise
        :class:`WorkspaceReadOnly`.
        """
        ...

    async def run_io(self, fn: Callable[..., Any], *args: Any) -> Any: ...


class _OffsetStream:
    """A seekable read-only view whose offsets are absolute from file start.

    Remote reads fetch ``[offset, EOF]`` into a buffer, but
    ``NeboFileReader.read_entries_incremental`` reports and rewinds to
    absolute frame positions. Rebasing here keeps the reader — and every
    persisted offset — identical across backends.
    """

    __slots__ = ("_buf", "_base")

    def __init__(self, buf: io.BytesIO, base: int) -> None:
        self._buf = buf
        self._base = base

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def tell(self) -> int:
        return self._base + self._buf.tell()

    def seek(self, pos: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self._buf.seek(max(0, pos - self._base))
        else:
            self._buf.seek(pos, whence)
        return self.tell()

    def close(self) -> None:
        self._buf.close()

    def __enter__(self) -> "_OffsetStream":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Local backend
# ---------------------------------------------------------------------------


class LocalWorkspace:
    """A plain directory. Behavior is identical to pre-workspace nebo."""

    is_remote = False
    writable = True

    def __init__(self, root: Path | str, poll_interval: Optional[float] = None) -> None:
        self._root = Path(str(root)).resolve()
        self.uri = str(self._root)
        self.default_poll_interval = (
            LOCAL_POLL_INTERVAL if poll_interval is None else poll_interval
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"LocalWorkspace({self.uri!r})"

    @property
    def root(self) -> Path:
        return self._root

    def _abs(self, rel: str) -> Path:
        rel = str(rel).strip("/")
        return self._root / rel if rel else self._root

    def ensure_root(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)

    # -- .nebo files ----------------------------------------------------

    def list_nebo(self) -> list[FileStat]:
        # Non-recursive on purpose: the --remote writer dir and meta/ can sit
        # inside the logdir without feeding back into the watcher.
        #
        # An unreadable root raises rather than reporting an empty workspace:
        # the watcher treats "no files" as "every tracked file was deleted",
        # so a transiently missing directory would otherwise reap every
        # offset it holds.
        entries = [
            e for e in os.scandir(self._root)
            if e.is_file() and e.name.endswith(".nebo")
        ]
        out: list[FileStat] = []
        for e in entries:
            try:
                st = e.stat()
            except FileNotFoundError:
                continue
            out.append(FileStat(uri=str(Path(e.path)), size=st.st_size, mtime=st.st_mtime))
        return out

    def stat(self, uri: str) -> Optional[FileStat]:
        try:
            st = os.stat(uri)
        except (FileNotFoundError, NotADirectoryError):
            return None
        return FileStat(uri=str(uri), size=st.st_size, mtime=st.st_mtime)

    def change_token(self) -> Optional[str]:
        return None  # scandir is cheap; always re-list

    @contextmanager
    def reader(self, uri: str, offset: int = 0) -> Iterator[BinaryIO]:
        with open(uri, "rb") as f:
            if offset:
                f.seek(offset)
            yield f

    def read_range(self, uri: str, offset: int, length: int) -> Optional[bytes]:
        try:
            with open(uri, "rb") as f:
                f.seek(offset)
                return f.read(length)
        except OSError:
            return None

    # -- meta/ ----------------------------------------------------------

    def read_bytes(self, rel: str) -> Optional[bytes]:
        p = self._abs(rel)
        if not p.is_file():
            return None
        return p.read_bytes()

    def list_dir(self, rel: str) -> list[str]:
        d = self._abs(rel)
        if not d.is_dir():
            return []
        return sorted(p.name for p in d.iterdir() if p.is_file())

    def list_tree(self, rel: str) -> list[str]:
        """Every file beneath ``rel``, as paths relative to ``rel``."""
        d = self._abs(rel)
        if not d.is_dir():
            return []
        return sorted(
            str(p.relative_to(d)) for p in d.rglob("*") if p.is_file()
        )

    def commit(
        self,
        adds: list[tuple[str, bytes]],
        deletes: list[str],
        *,
        message: str = "nebo: update",
    ) -> None:
        # Deletes run first so a move (delete old dir + add new paths) works
        # in one call.
        for rel in deletes:
            p = self._abs(rel)
            if p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()
        for rel, data in adds:
            self._write_atomic(self._abs(rel), data)

    def move(self, src: str, dst: str) -> None:
        source = self._abs(src)
        if not source.exists():
            return
        target = self._abs(dst)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target) if target.is_dir() else target.unlink()
        shutil.move(str(source), str(target))

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> None:
        """tmp + fsync + rename — the run tree has always written this way."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)  # atomic on POSIX and Windows

    async def run_io(self, fn: Callable[..., Any], *args: Any) -> Any:
        # Local I/O is fast enough to stay on the event loop; keeping it inline
        # preserves the watcher's existing timing (tests poll at 50 ms).
        return fn(*args)


# ---------------------------------------------------------------------------
# Hugging Face backend
# ---------------------------------------------------------------------------


class HFWorkspace:
    """A Hugging Face Storage Bucket or repo, addressed as ``hf://…``.

    **Read-only.** The daemon serves runs out of the archive and never writes
    to it; publishing is a separate, offline step (see the module docstring).
    """

    is_remote = True
    writable = False

    def __init__(
        self,
        ref: HfRef,
        token: Optional[str] = None,
        poll_interval: Optional[float] = None,
    ) -> None:
        self._ref = ref
        self._token = token or os.environ.get("HF_TOKEN") or None
        self.uri = ref.uri
        self.default_poll_interval = (
            REMOTE_POLL_INTERVAL if poll_interval is None else poll_interval
        )
        self._fs: Any = None
        self._api: Any = None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"HFWorkspace({self.uri!r})"

    @property
    def ref(self) -> HfRef:
        return self._ref

    # -- lazy huggingface_hub handles ------------------------------------

    def _hub(self) -> Any:
        try:
            import huggingface_hub
        except ImportError as e:  # pragma: no cover - depends on install
            raise WorkspaceError(_HF_IMPORT_ERROR) from e
        return huggingface_hub

    @property
    def fs(self) -> Any:
        if self._fs is None:
            self._fs = self._hub().HfFileSystem(token=self._token)
        return self._fs

    @property
    def api(self) -> Any:
        if self._api is None:
            self._api = self._hub().HfApi(token=self._token)
        return self._api

    # -- path plumbing ---------------------------------------------------

    def _fs_path(self, rel: str = "") -> str:
        return self._ref.join(rel).fs_path

    def _repo_path(self, rel: str) -> str:
        return self._ref.join(rel).repo_path

    def _uri_to_fs_path(self, uri: str) -> str:
        """A file URI this workspace handed out -> an HfFileSystem path."""
        return parse_hf_uri(uri).fs_path

    def ensure_root(self) -> None:
        # Object stores have no directories; a repo prefix springs into being
        # with its first file. Nothing to create.
        return None

    # -- .nebo files ------------------------------------------------------

    def list_nebo(self) -> list[FileStat]:
        try:
            entries = self.fs.ls(self._fs_path(), detail=True, refresh=True)
        except FileNotFoundError:
            return []
        out: list[FileStat] = []
        for e in entries:
            if e.get("type") != "file":
                continue
            name = str(e.get("name", ""))
            base = name.rsplit("/", 1)[-1]
            if not base.endswith(".nebo"):
                continue
            out.append(
                FileStat(
                    uri=f"{self.uri}/{base}",
                    size=int(e.get("size") or 0),
                    mtime=_as_epoch(e.get("mtime") or e.get("last_modified")),
                    token=_content_token(e),
                )
            )
        return out

    def stat(self, uri: str) -> Optional[FileStat]:
        try:
            info = self.fs.info(self._uri_to_fs_path(uri), refresh=True)
        except FileNotFoundError:
            return None
        return FileStat(
            uri=uri,
            size=int(info.get("size") or 0),
            mtime=_as_epoch(info.get("mtime") or info.get("last_modified")),
            token=_content_token(info),
        )

    def change_token(self) -> Optional[str]:
        """A cheap marker that changes when the archive does, or None.

        Repos have a head commit sha, so one small call per poll lets the
        watcher skip the much more expensive listing while it is unchanged.

        Buckets have no such marker. `bucket_info` exposes size/total_files,
        but that pair misses a same-size replacement — exactly what a
        republished archive looks like — so returning None is the honest
        answer. The watcher treats None as "always list", and a bucket
        listing is one cheap call at a 30 s cadence.
        """
        if self._ref.repo_type == "bucket":
            return None
        try:
            info = self.api.repo_info(
                repo_id=self._ref.repo_id,
                repo_type=self._ref.repo_type,
                revision=self._ref.revision,
            )
        except Exception:
            return None
        return getattr(info, "sha", None)

    @contextmanager
    def reader(self, uri: str, offset: int = 0) -> Iterator[Any]:
        """A seekable, absolutely-offset stream over ``[offset, EOF]``.

        ``read_entries_incremental`` reads 1 byte, then 4, then N per entry —
        unbuffered that is three range requests *per log entry*. A small tail
        is therefore fetched whole in one GET; a large one streams through
        fsspec's block cache so memory stays bounded.
        """
        path = self._uri_to_fs_path(uri)
        size = 0
        try:
            size = int(self.fs.info(path).get("size") or 0)
        except FileNotFoundError:
            raise
        except Exception:
            pass

        if size and size - offset > REMOTE_INLINE_MAX:
            f = self.fs.open(path, "rb", block_size=REMOTE_BLOCK_SIZE)
            try:
                if offset:
                    f.seek(offset)
                yield f  # fsspec offsets are already absolute
            finally:
                f.close()
            return

        data = self.fs.cat_file(path, start=offset or None)
        with _OffsetStream(io.BytesIO(data or b""), offset) as stream:
            yield stream

    def read_range(self, uri: str, offset: int, length: int) -> Optional[bytes]:
        try:
            return self.fs.cat_file(
                self._uri_to_fs_path(uri), start=offset, end=offset + length
            )
        except Exception:
            return None

    # -- meta/ -------------------------------------------------------------

    def read_bytes(self, rel: str) -> Optional[bytes]:
        # `None` means "not there", and nothing else. A transport failure
        # must propagate: callers treat None as absence, and TreeStore in
        # particular would read a failed tree.json fetch as an empty tree and
        # commit that over real curation on the next mutation.
        try:
            return self.fs.cat_file(self._fs_path(rel))
        except FileNotFoundError:
            return None

    def list_dir(self, rel: str) -> list[str]:
        try:
            entries = self.fs.ls(self._fs_path(rel), detail=True, refresh=True)
        except FileNotFoundError:
            return []
        return sorted(
            str(e["name"]).rsplit("/", 1)[-1]
            for e in entries
            if e.get("type") == "file"
        )

    def list_tree(self, rel: str) -> list[str]:
        base = self._fs_path(rel)
        try:
            entries = self.fs.find(base, detail=True, refresh=True)
        except FileNotFoundError:
            return []
        if isinstance(entries, dict):
            entries = list(entries.values())
        out = []
        for e in entries:
            if e.get("type") != "file":
                continue
            name = str(e.get("name", ""))
            if name.startswith(base + "/"):
                out.append(name[len(base) + 1:])
        return sorted(out)

    def commit(
        self,
        adds: list[tuple[str, bytes]],
        deletes: list[str],
        *,
        message: str = "nebo: update",
    ) -> None:
        """Always raises — the daemon does not write to an archive.

        Publishing is a separate, offline step: assemble a workspace locally
        and sync it up (``huggingface_hub.sync_bucket`` for a bucket, a commit
        for a repo). Writing from the daemon would mean an object PUT per
        mutation against storage that cannot append, so the run tree degrades
        to read-only here instead — see `TreeStore`.
        """
        raise WorkspaceReadOnly(
            f"{self.uri} is an archive; nebo only reads from it. Publish by "
            "syncing a local directory up (e.g. huggingface_hub.sync_bucket)."
        )

    def move(self, src: str, dst: str) -> None:
        """Always raises — see :meth:`commit`."""
        raise WorkspaceReadOnly(
            f"{self.uri} is an archive; nebo only reads from it."
        )

    async def run_io(self, fn: Callable[..., Any], *args: Any) -> Any:
        # Every remote call is blocking HTTP. Running it on the event loop
        # would stall the whole daemon for the duration of the request.
        import asyncio

        return await asyncio.to_thread(fn, *args)


def _as_epoch(value: object) -> Optional[float]:
    """Coerce whatever a backend calls a timestamp into epoch seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    ts = getattr(value, "timestamp", None)
    if callable(ts):
        try:
            return float(ts())
        except Exception:
            return None
    return None


def _content_token(info: dict) -> Optional[str]:
    """An opaque marker that changes when a file's bytes change."""
    for key in ("blob_id", "xet_hash", "etag"):
        val = info.get(key)
        if val:
            return str(val)
    return None


# ---------------------------------------------------------------------------
# Factory + standalone resolution
# ---------------------------------------------------------------------------


def open_workspace(
    uri: Optional[object],
    *,
    token: Optional[str] = None,
    poll_interval: Optional[float] = None,
) -> Workspace:
    """Build the backend for a logdir string."""
    if uri is None:
        raise WorkspaceError("a workspace needs a logdir")
    if is_remote_uri(uri):
        return HFWorkspace(parse_hf_uri(str(uri)), token=token, poll_interval=poll_interval)
    return LocalWorkspace(str(uri), poll_interval=poll_interval)


# Backends are memoized per repo so a burst of media reads shares one HTTP
# session and one listing cache.
_REMOTE_CACHE: dict[str, HFWorkspace] = {}


def _remote_for(uri: str) -> HFWorkspace:
    ref = parse_hf_uri(uri)
    root = HfRef(ref.repo_type, ref.repo_id, "", ref.revision)
    ws = _REMOTE_CACHE.get(root.uri)
    if ws is None:
        ws = HFWorkspace(root)
        _REMOTE_CACHE[root.uri] = ws
    return ws


def read_frame_bytes(uri: str, offset: int, length: int) -> Optional[bytes]:
    """Read ``length`` bytes at ``offset`` from a file URI, any backend.

    Media occurrences are stored as ``(src_path, offset, length)`` references
    into a ``.nebo`` file and resolved much later by the cache, which has no
    workspace in hand — so this dispatches on the URI alone.
    """
    if is_remote_uri(uri):
        try:
            return _remote_for(uri).read_range(uri, offset, length)
        except WorkspaceError:
            logger.warning("workspace: cannot resolve %s (huggingface_hub missing)", uri)
            return None
    return _LOCAL_READER.read_range(uri, offset, length)


# Path-agnostic: `read_range` takes absolute URIs, so the root is irrelevant.
_LOCAL_READER = LocalWorkspace(".")
