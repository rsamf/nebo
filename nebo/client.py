"""Shared HTTP client for the nebo daemon.

Both `nebo/mcp/tools.py` and `nebo/cli.py` call into this module. It is the
only code outside `nebo/server/` that imports `urllib.request`.

Connection settings resolve in this order:
  1. Explicit kwargs (`url=`, `port=`, `api_token=`).
  2. Environment (`NEBO_CLI_URL`, `NEBO_CLI_PORT`, `NEBO_API_TOKEN`).
  3. Defaults (`http://localhost:7861`, no token).
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional


DEFAULT_PORT = 7861


def _resolve_url(url: Optional[str] = None, port: Optional[int] = None) -> str:
    if url:
        return url
    env_url = os.environ.get("NEBO_CLI_URL")
    if env_url:
        return env_url
    p = port if port is not None else int(os.environ.get("NEBO_CLI_PORT") or DEFAULT_PORT)
    return f"http://localhost:{p}"


def _resolve_token(api_token: Optional[str] = None) -> Optional[str]:
    if api_token:
        return api_token
    return os.environ.get("NEBO_API_TOKEN")


def _get(
    path: str,
    *,
    url: Optional[str] = None,
    port: Optional[int] = None,
    api_token: Optional[str] = None,
    timeout: float = 5.0,
) -> Any:
    base = _resolve_url(url=url, port=port)
    token = _resolve_token(api_token)
    full_url = f"{base}{path}"
    req = urllib.request.Request(full_url, method="GET")
    if token:
        req.add_header("X-Nebo-Token", token)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(
    path: str,
    body: Any,
    *,
    url: Optional[str] = None,
    port: Optional[int] = None,
    api_token: Optional[str] = None,
    timeout: float = 10.0,
) -> Any:
    base = _resolve_url(url=url, port=port)
    token = _resolve_token(api_token)
    full_url = f"{base}{path}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(full_url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Nebo-Token", token)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _run_scope(run_id: Optional[str]) -> str:
    """Return `/runs/<id>` for run-scoped reads, or '' for the legacy
    'latest run' read paths kept for backward compatibility."""
    return f"/runs/{urllib.parse.quote(run_id)}" if run_id else ""


def get_health(**conn) -> Any:
    return _get("/health", **conn)


def get_run_history(**conn) -> Any:
    return _get("/runs", **conn)


def get_run_status(run_id: str, **conn) -> Any:
    return _get(f"/runs/{urllib.parse.quote(run_id)}", **conn)


def get_description(run_id: Optional[str] = None, **conn) -> Any:
    """Return workflow description + per-node docstrings.

    The daemon doesn't expose a dedicated /description route — the
    workflow description and node docstrings live inside the /graph
    payload. This helper does the extraction so callers can rely on a
    stable `{workflow_description, node_descriptions}` shape regardless
    of how the daemon happens to serve it.
    """
    graph = get_graph(run_id=run_id, **conn)
    return {
        "workflow_description": graph.get("workflow_description"),
        "node_descriptions": {
            nid: n.get("docstring")
            for nid, n in graph.get("nodes", {}).items()
            if n.get("docstring")
        },
    }


def get_graph(run_id: Optional[str] = None, **conn) -> Any:
    return _get(f"{_run_scope(run_id)}/graph" if run_id else "/graph", **conn)


def get_loggable_status(loggable_id: str, run_id: Optional[str] = None, **conn) -> Any:
    if run_id:
        path = f"{_run_scope(run_id)}/loggables/{urllib.parse.quote(loggable_id)}"
    else:
        path = f"/loggables/{urllib.parse.quote(loggable_id)}"
    return _get(path, **conn)


def get_text(
    loggable_id: Optional[str] = None,
    run_id: Optional[str] = None,
    limit: Optional[int] = None,
    **conn,
) -> Any:
    path = f"{_run_scope(run_id)}/text" if run_id else "/text"
    qs: list[str] = []
    if loggable_id:
        qs.append(f"loggable_id={urllib.parse.quote(loggable_id)}")
    if limit is not None:
        qs.append(f"limit={int(limit)}")
    if qs:
        path = f"{path}?{'&'.join(qs)}"
    return _get(path, **conn)


def get_metrics(
    loggable_id: str,
    *,
    name: Optional[str] = None,
    tag: Optional[str] = None,
    step: Optional[int] = None,
    run_id: Optional[str] = None,
    **conn,
) -> Any:
    if run_id:
        path = f"{_run_scope(run_id)}/loggables/{urllib.parse.quote(loggable_id)}"
    else:
        path = f"/loggables/{urllib.parse.quote(loggable_id)}"
    qs: list[str] = []
    if name:
        qs.append(f"name={urllib.parse.quote(name)}")
    if tag:
        qs.append(f"tag={urllib.parse.quote(tag)}")
    if step is not None:
        qs.append(f"step={int(step)}")
    if qs:
        path = f"{path}?{'&'.join(qs)}"
    return _get(path, **conn)


def list_images(run_id: str, **conn) -> Any:
    """List a run's images: {loggable_id: [{name, step, media_id, ...}]}."""
    return _get(f"/runs/{urllib.parse.quote(run_id)}/images", **conn)


def list_audio(run_id: str, **conn) -> Any:
    """List a run's audio: {loggable_id: [{name, sr, step, media_id, ...}]}."""
    return _get(f"/runs/{urllib.parse.quote(run_id)}/audio", **conn)


def list_actions(
    run_id: str,
    loggable_id: Optional[str] = None,
    name: Optional[str] = None,
    limit: Optional[int] = None,
    **conn,
) -> Any:
    """List a run's 3D scenes and the body models they reference.

    Returns ``{"actions": {loggable_id: [frame, ...]},
    "body_models": {model_id: manifest}}``. ``limit=0`` asks the daemon
    for full fidelity instead of the default per-scene frame cap.
    """
    params: dict[str, Any] = {}
    if loggable_id is not None:
        params["loggable_id"] = loggable_id
    if name is not None:
        params["name"] = name
    if limit is not None:
        params["limit"] = limit
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    return _get(
        f"/runs/{urllib.parse.quote(run_id)}/actions{query}", **conn
    )


def get_media(
    run_id: str,
    media_id: str,
    *,
    url: Optional[str] = None,
    port: Optional[int] = None,
    api_token: Optional[str] = None,
    timeout: float = 10.0,
) -> tuple[bytes, str]:
    """Fetch one media object's raw bytes. Returns (bytes, content_type).

    media_id comes from list_images/list_audio; it is content-addressed,
    so the same id always yields the same bytes.
    """
    base = _resolve_url(url=url, port=port)
    token = _resolve_token(api_token)
    path = f"/runs/{urllib.parse.quote(run_id)}/media/{urllib.parse.quote(media_id)}"
    req = urllib.request.Request(f"{base}{path}", method="GET")
    if token:
        req.add_header("X-Nebo-Token", token)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        ctype = resp.headers.get("Content-Type", "application/octet-stream")
        return resp.read(), ctype


def load_file(filepath: str, **conn) -> Any:
    return _post("/load", {"filepath": filepath}, **conn)


def wait_for_alert(
    run_id: str,
    *,
    timeout: float = 300.0,
    min_level: int = 20,
    **conn,
) -> Any:
    """Block until an alert at or above min_level fires, or timeout.

    Returns `{"status": "alert", "alert": {...}}` or `{"status": "timeout"}`.
    """
    path = (
        f"/runs/{urllib.parse.quote(run_id)}/alerts/wait"
        f"?timeout={timeout}&min_level={int(min_level)}"
    )
    return _get(path, timeout=max(timeout + 5, 30), **conn)


# Condition strings look like "train/loss > 5" — a metric name (which may
# itself contain '/', '.', etc.), one comparison operator, and a number.
_CONDITION_RE = re.compile(
    r"^\s*(?P<metric>.+?)\s*(?P<op>>=|<=|==|!=|>|<)\s*(?P<value>-?\d+(?:\.\d+)?)\s*$"
)


def parse_condition(expr: str) -> dict[str, Any]:
    """Parse a condition string into `{"metric", "op", "value"}`.

    The metric name `last_event` is reserved for heartbeat rules
    ("last_event > 60" fires once a run has been idle 60s); the daemon
    validates its extra constraints (ops > >= only, no loggable_id).

    Raises ValueError with a usage hint on malformed input.
    """
    m = _CONDITION_RE.match(expr or "")
    if not m or not m.group("metric").strip():
        raise ValueError(
            f"invalid condition {expr!r}; expected '<metric> <op> <number>' "
            "with op one of > >= < <= == != (e.g. 'train/loss > 5')"
        )
    return {
        "metric": m.group("metric").strip(),
        "op": m.group("op"),
        "value": float(m.group("value")),
    }


def list_alerts(run_id: Optional[str] = None, **conn) -> Any:
    path = "/alerts"
    if run_id:
        path += f"?run_id={urllib.parse.quote(run_id)}"
    return _get(path, **conn)


def get_alert(rule_id: str, **conn) -> Any:
    return _get(f"/alerts/{urllib.parse.quote(rule_id)}", **conn)


def set_alert(
    title: str,
    condition: dict[str, Any],
    *,
    text: str = "",
    level: int = 20,
    loggable_id: Optional[str] = None,
    run_id: Optional[str] = None,
    **conn,
) -> Any:
    """Create an alert rule on the daemon.

    `condition` is `{"metric", "op", "value"}` (see `parse_condition`).
    """
    body: dict[str, Any] = {
        "title": title,
        "text": text,
        "level": int(level),
        "condition": {**condition, "loggable_id": loggable_id},
    }
    if run_id:
        body["run_id"] = run_id
    return _post("/alerts", body, **conn)


def delete_alert(rule_id: str, **conn) -> Any:
    return _request_json(f"/alerts/{urllib.parse.quote(rule_id)}", method="DELETE", **conn)


def _request_json(
    path: str,
    *,
    method: str,
    body: Any = None,
    url: Optional[str] = None,
    port: Optional[int] = None,
    api_token: Optional[str] = None,
    timeout: float = 10.0,
) -> Any:
    base = _resolve_url(url=url, port=port)
    token = _resolve_token(api_token)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{base}{path}", data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Nebo-Token", token)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


# -- run tree (groups + placements + docs) -----------------------------

def _group_path(path: str) -> str:
    """URL-encode a group path, keeping `/` separators intact."""
    return urllib.parse.quote(path.strip("/"), safe="/")


def get_tree(**conn) -> Any:
    return _get("/tree", **conn)


def create_group(path: str, **conn) -> Any:
    return _request_json("/groups", method="POST", body={"path": path}, **conn)


def move_group(path: str, new_path: str, **conn) -> Any:
    return _request_json(
        f"/groups/{_group_path(path)}", method="PATCH",
        body={"new_path": new_path}, **conn,
    )


def delete_group(path: str, **conn) -> Any:
    return _request_json(f"/groups/{_group_path(path)}", method="DELETE", **conn)


def set_run_group(run_id: str, group: str, **conn) -> Any:
    return _request_json(
        f"/runs/{urllib.parse.quote(run_id)}/group", method="PUT",
        body={"group": group}, **conn,
    )


def _raw(
    path: str, *, method: str, data: Optional[bytes] = None,
    content_type: Optional[str] = None, url: Optional[str] = None,
    port: Optional[int] = None, api_token: Optional[str] = None,
    timeout: float = 10.0,
) -> tuple[int, str]:
    """Send a request returning (status, text); 4xx/5xx don't raise."""
    base = _resolve_url(url=url, port=port)
    token = _resolve_token(api_token)
    req = urllib.request.Request(f"{base}{path}", data=data, method=method)
    if content_type:
        req.add_header("Content-Type", content_type)
    if token:
        req.add_header("X-Nebo-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def get_group_doc(path: str, name: str, **conn) -> Optional[str]:
    """Return a group doc's markdown, or None if it doesn't exist."""
    status, text = _raw(
        f"/groups/{_group_path(path)}/docs/{urllib.parse.quote(name)}",
        method="GET", **conn,
    )
    return text if status == 200 else None


def set_group_doc(path: str, name: str, content: str, **conn) -> Any:
    status, text = _raw(
        f"/groups/{_group_path(path)}/docs/{urllib.parse.quote(name)}",
        method="PUT", data=content.encode("utf-8"),
        content_type="text/markdown", **conn,
    )
    if status >= 400:
        raise RuntimeError(f"set_group_doc failed: HTTP {status} {text}")
    return {"status": status}


def delete_group_doc(path: str, name: str, **conn) -> Any:
    status, text = _raw(
        f"/groups/{_group_path(path)}/docs/{urllib.parse.quote(name)}",
        method="DELETE", **conn,
    )
    if status not in (204, 200, 404):
        raise RuntimeError(f"delete_group_doc failed: HTTP {status} {text}")
    return {"status": status}


def _ensure_loggable_event(loggable_id: str) -> dict[str, Any]:
    """Idempotent register event so the daemon doesn't drop entries on
    unknown loggables. Matches the kind used elsewhere for the two
    synthetic loggables (__agent__ / __global__); other ids default to
    "global" which is what the daemon's register handler treats as
    "non-node" (i.e., not a DAG node)."""
    if loggable_id == "__agent__":
        kind = "agent"
    elif loggable_id == "__global__":
        kind = "global"
    else:
        kind = "global"
    return {
        "type": "loggable_register",
        "loggable_id": loggable_id,
        "data": {"loggable_id": loggable_id, "kind": kind},
    }


def _events_path(run_id: Optional[str]) -> str:
    if run_id:
        return f"/events?run_id={urllib.parse.quote(run_id)}"
    return "/events"


def log_metric(
    entries: list[dict[str, Any]],
    *,
    run_id: Optional[str] = None,
    **conn,
) -> Any:
    """Push metric entries to the daemon.

    Each entry: ``{loggable_id?, name, value, type?, step?, tags?}``.
    `loggable_id` defaults to ``__agent__`` (the agent sandbox).
    """
    events: list[dict[str, Any]] = []
    for e in entries:
        lid = e.get("loggable_id") or "__agent__"
        events.append(_ensure_loggable_event(lid))
        events.append({
            "type": "metric",
            "loggable_id": lid,
            "name": e.get("name", ""),
            "metric_type": e.get("type", "line"),
            "value": e.get("value"),
            "step": e.get("step"),
            "tags": list(e.get("tags") or []),
            "timestamp": time.time(),
        })
    return _post(_events_path(run_id), events, **conn)


def log_text(
    entries: list[dict[str, Any]],
    *,
    run_id: Optional[str] = None,
    **conn,
) -> Any:
    """Push text entries to the daemon.

    Each entry: ``{loggable_id?, name?, message, step?}``. ``loggable_id``
    defaults to ``__agent__``; ``name`` defaults to ``text``.
    """
    events: list[dict[str, Any]] = []
    for e in entries:
        lid = e.get("loggable_id") or "__agent__"
        events.append(_ensure_loggable_event(lid))
        events.append({
            "type": "text",
            "loggable_id": lid,
            "name": e.get("name") or "text",
            "message": e.get("message", ""),
            "step": e.get("step"),
            "timestamp": time.time(),
        })
    return _post(_events_path(run_id), events, **conn)


MEDIA_BYTES_LIMIT = 50 * 1024 * 1024  # 50 MB


def _normalize_media_payload(entry: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """Return (data_b64, url) where exactly one is non-None.

    Accepts entries with `path`, `url`, or `data`. Reads `path` locally and
    base64-encodes. Rejects entries that supply more than one source.
    """
    sources = [k for k in ("path", "url", "data") if entry.get(k)]
    if len(sources) > 1:
        raise ValueError(
            f"entry {entry.get('name')!r}: supply only one of path/url/data (got {sources})"
        )
    if not sources:
        raise ValueError(f"entry {entry.get('name')!r}: needs one of path, url, or data")
    if entry.get("path"):
        with open(entry["path"], "rb") as fh:
            raw = fh.read()
        if len(raw) > MEDIA_BYTES_LIMIT:
            raise ValueError(
                f"entry {entry.get('name')!r}: {len(raw)} bytes exceeds {MEDIA_BYTES_LIMIT}"
            )
        return base64.b64encode(raw).decode("ascii"), None
    if entry.get("data"):
        return str(entry["data"]), None
    return None, str(entry["url"])


def log_image(
    entries: list[dict[str, Any]],
    *,
    run_id: Optional[str] = None,
    **conn,
) -> Any:
    """Push image entries to the daemon.

    Each entry: ``{loggable_id?, name, path? | url? | data?, step?, labels?}``.
    Exactly one of `path` (local file, read+base64-encoded here), `url`
    (fetched server-side), or `data` (already-base64) must be set.
    `loggable_id` defaults to ``__agent__``.
    """
    events: list[dict[str, Any]] = []
    for e in entries:
        lid = e.get("loggable_id") or "__agent__"
        data_b64, url = _normalize_media_payload(e)
        events.append(_ensure_loggable_event(lid))
        evt: dict[str, Any] = {
            "type": "image",
            "loggable_id": lid,
            "name": e.get("name", ""),
            "step": e.get("step"),
            "timestamp": time.time(),
        }
        if data_b64 is not None:
            evt["data"] = data_b64
        else:
            evt["url"] = url
        if "labels" in e:
            evt["labels"] = e["labels"]
        events.append(evt)
    return _post(_events_path(run_id), events, **conn)


def log_audio(
    entries: list[dict[str, Any]],
    *,
    run_id: Optional[str] = None,
    **conn,
) -> Any:
    """Push audio entries to the daemon.

    Each entry: ``{loggable_id?, name, path? | url? | data?, sr?, step?}``.
    Exactly one of `path`/`url`/`data` must be set. `sr` defaults to 16000.
    `loggable_id` defaults to ``__agent__``.
    """
    events: list[dict[str, Any]] = []
    for e in entries:
        lid = e.get("loggable_id") or "__agent__"
        data_b64, url = _normalize_media_payload(e)
        events.append(_ensure_loggable_event(lid))
        evt: dict[str, Any] = {
            "type": "audio",
            "loggable_id": lid,
            "name": e.get("name", ""),
            "sr": e.get("sr", 16000),
            "step": e.get("step"),
            "timestamp": time.time(),
        }
        if data_b64 is not None:
            evt["data"] = data_b64
        else:
            evt["url"] = url
        events.append(evt)
    return _post(_events_path(run_id), events, **conn)
