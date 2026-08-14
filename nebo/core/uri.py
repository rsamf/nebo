"""Resolve nb.init(uri=...) input into a (Mode, dest) pair."""

from __future__ import annotations

import enum
import re
from typing import Optional


class Mode(enum.Enum):
    FILE = "file"
    NETWORK = "network"


_URL_RE = re.compile(r"^(https?|wss?)://", re.IGNORECASE)
_HOST_PORT_RE = re.compile(r"^[A-Za-z0-9._-]+:\d+(/.*)?$")
_BUCKET_RE = re.compile(r"^hf://", re.IGNORECASE)

DEFAULT_FILE_URI = ".nebo/"


def resolve_uri(uri: Optional[str]) -> tuple[Mode, str]:
    """Map a URI string to (Mode, normalized destination).

    Rules:
      * None / empty -> file mode at DEFAULT_FILE_URI.
      * http(s)://... -> network mode, dest = uri as-given.
      * ws(s)://... -> ValueError (no websocket transport here).
      * hf://... -> ValueError (buckets are a daemon-side concern).
      * host:port[/path] (no leading slash, no scheme) -> network mode,
        dest = "http://" + uri.
      * Anything else -> file mode at the given path.
    """
    if not uri:
        return Mode.FILE, DEFAULT_FILE_URI
    if _URL_RE.match(uri):
        scheme = uri.split("://", 1)[0].lower()
        if scheme in ("ws", "wss"):
            raise ValueError(
                f"nebo.init(uri={uri!r}): use http(s)://, ws not supported on init"
            )
        return Mode.NETWORK, uri
    if _BUCKET_RE.match(uri):
        # The SDK appends to an open .nebo stream, which object storage can't
        # do — and falling through to file mode would silently create a local
        # directory literally named "hf:".
        raise ValueError(
            f"nebo.init(uri={uri!r}): the SDK writes .nebo files locally, not "
            "to a bucket. Write to a local directory and publish the files, or "
            "point the daemon at the bucket with "
            "`nebo serve --logdir hf://...` and connect over http(s)://."
        )
    if _HOST_PORT_RE.match(uri):
        return Mode.NETWORK, f"http://{uri}"
    return Mode.FILE, uri
