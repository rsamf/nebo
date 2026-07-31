"""``nebo://`` resource references — the canonical ID for every resource.

Grammar (mirrored by ``ui/src/lib/refs.ts`` — keep the two in lockstep)::

    nebo://run/<run_id>                          a run
    nebo://run/<run_id>/<loggable_id>            a loggable within it
    nebo://run/<run_id>/<loggable_id>/<name...>  a named stream
    <any run form>@<step>                        a datapoint at a step
    nebo://group/<path>                          a group

Parsing is unambiguous because the loggable segment is always exactly one
path segment: the special loggables are single tokens (``__global__``,
``__agent__``) and node ids are Python qualnames (dots, never slashes).
Everything after it belongs to the stream name, which may itself contain
``/`` (e.g. ``train/time/rollout_s``).

``?step=<n>`` is accepted as a legacy alias for ``@<step>`` (older group
docs used it on run refs); ``format_ref`` always emits ``@<step>``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union
from urllib.parse import parse_qs

SCHEME = "nebo://"


@dataclass(frozen=True)
class RunRef:
    run_id: str
    loggable_id: Optional[str] = None
    name: Optional[str] = None
    step: Optional[int] = None


@dataclass(frozen=True)
class GroupRef:
    path: str


NeboRef = Union[RunRef, GroupRef]


def parse_ref(href: str) -> Optional[NeboRef]:
    """Parse a ``nebo://`` URI; None if it isn't one (or is malformed)."""
    if not href.startswith(SCHEME):
        return None
    rest = href[len(SCHEME):]
    path_part, _, query = rest.partition("?")

    step: Optional[int] = None
    if query:
        raw = parse_qs(query).get("step", [None])[0]
        if raw is not None:
            try:
                step = int(raw)
            except ValueError:
                step = None

    if path_part.startswith("group/"):
        path = path_part[len("group/"):].strip("/")
        return GroupRef(path=path) if path else None

    if not path_part.startswith("run/"):
        return None
    body = path_part[len("run/"):].strip("/")
    if not body:
        return None

    # `@<step>` binds to the end of the whole path. An `@` may legally
    # appear inside a stream name, so only a numeric tail counts.
    head, sep, tail = body.rpartition("@")
    if sep and tail.lstrip("-").isdigit():
        body = head
        step = int(tail)

    segments = [s for s in body.split("/") if s]
    if not segments:
        return None
    run_id = segments[0]
    loggable_id = segments[1] if len(segments) > 1 else None
    name = "/".join(segments[2:]) if len(segments) > 2 else None
    return RunRef(run_id=run_id, loggable_id=loggable_id, name=name, step=step)


def format_ref(ref: NeboRef) -> str:
    """The canonical string for a reference (inverse of ``parse_ref``)."""
    if isinstance(ref, GroupRef):
        return f"{SCHEME}group/{ref.path.strip('/')}"
    parts = [ref.run_id]
    if ref.loggable_id:
        parts.append(ref.loggable_id)
        if ref.name:
            parts.append(ref.name)
    out = SCHEME + "run/" + "/".join(parts)
    if ref.step is not None:
        out += f"@{ref.step}"
    return out
