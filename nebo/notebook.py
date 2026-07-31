"""Notebook rendering helpers.

In a Jupyter / IPython context, ``nb.show()`` returns an object that renders
as an inline ``<iframe>`` pointing at the running nebo daemon. The slice of
the run shown is determined by which kwargs are set — there is no separate
``view`` discriminator. Each slice maps directly to a URL query parameter,
so ``nb.show(metric="loss")`` becomes ``?run=<id>&metric=loss`` etc.
"""

from __future__ import annotations

from html import escape
from typing import Optional, Union

from nebo.core.state import get_state


_SLICE_KWARGS = ("metric", "image", "audio", "text", "dag")


class _ShowHandle:
    """Renderable returned by :func:`show`. Has a ``_repr_html_`` that emits
    the iframe; in a non-notebook context, ``str()`` shows the URL."""

    def __init__(self, url: Optional[str], width: Union[str, int], height: Union[str, int],
                 hint: Optional[str] = None) -> None:
        self.url = url
        self.width = width
        self.height = height
        self.hint = hint

    def __repr__(self) -> str:
        if self.hint:
            return f"<nebo.show: {self.hint}>"
        return f"<nebo.show: {self.url}>"

    def _repr_html_(self) -> str:
        if not self.url:
            return f'<div style="color: #888; font-size: 13px;">{escape(self.hint or "nebo: nothing to show")}</div>'
        w = self.width if isinstance(self.width, str) else f"{self.width}px"
        h = self.height if isinstance(self.height, str) else f"{self.height}px"
        return (
            f'<iframe src="{escape(self.url)}" '
            f'width="{escape(str(w))}" height="{escape(str(h))}" '
            f'style="border: 1px solid #2a2a2a; border-radius: 6px;" '
            f'sandbox="allow-scripts allow-same-origin"></iframe>'
        )


def show(
    *,
    run: Optional[str] = None,
    node: Optional[str] = None,
    metric: Union[str, bool, None] = None,
    image: Union[str, bool, None] = None,
    audio: Union[str, bool, None] = None,
    text: Union[str, bool, None] = None,
    dag: bool = False,
    width: Union[str, int] = "100%",
    height: Union[str, int] = 600,
) -> _ShowHandle:
    """Return a Jupyter-renderable iframe of the daemon UI.

    Each slice kwarg is mutually exclusive: pick at most one, or pass
    nothing to show the full run dashboard.

    Args:
        run: Run ID to embed. Defaults to the currently active run.
        node: Node ID or function name. With no slice kwarg this shows
            the node's full detail card; combined with a slice, it
            filters that slice to the node.
        metric: ``str`` shows a single metric by name; ``True`` shows
            the metrics gallery for the run (or for ``node``).
        image: Same shape as ``metric`` for images.
        audio: Same shape as ``metric`` for audio recordings.
        text: Same shape as ``metric`` for text streams — a ``str``
            shows a single named stream, ``True`` the text panel
            (optionally filtered by ``node``).
        dag: ``True`` shows the DAG-only view.
        width, height: iframe dimensions. Strings (``"100%"``) or ints (px).

    Returns:
        A handle with ``_repr_html_`` for inline notebook display.
    """
    active = {
        "metric": metric,
        "image": image,
        "audio": audio,
        "text": text,
        "dag": dag,
    }
    truthy = {k: v for k, v in active.items() if v}
    if len(truthy) > 1:
        raise ValueError(
            f"At most one slice may be set; got {sorted(truthy)}. "
            "Pass nothing to show the full run."
        )

    state = get_state()
    run_id = run or state._active_run_id
    if run_id is None:
        return _ShowHandle(
            url=None,
            width=width,
            height=height,
            hint="No active run. Call nb.init() and start a run before nb.show().",
        )

    host = "localhost"  # daemon is local-only for the notebook renderer
    port = state.port
    base = f"http://{host}:{port}/"

    parts: list[str] = [f"run={run_id}"]
    if node is not None:
        parts.append(f"node={node}")

    # Slice flags: a string value becomes the singular form (`metric=NAME`),
    # a True value becomes the plural-flag form (`metrics`). Bare flags
    # (logs / dag) are presence-only.
    if isinstance(metric, str):
        parts.append(f"metric={metric}")
    elif metric is True:
        parts.append("metrics")
    if isinstance(image, str):
        parts.append(f"image={image}")
    elif image is True:
        parts.append("images")
    if isinstance(audio, str):
        parts.append(f"audio={audio}")
    elif audio is True:
        parts.append("audios")
    if isinstance(text, str):
        parts.append(f"text={text}")
    elif text is True:
        parts.append("text")
    if dag:
        parts.append("dag")

    url = base + "?" + "&".join(parts)
    return _ShowHandle(url=url, width=width, height=height)
