"""CLI entry points for nebo.

Commands:
    nebo serve   — Start the persistent daemon server
    nebo status  — Show daemon status and recent runs
    nebo stop    — Stop the daemon
    nebo text ls — View text entries from runs
    nebo mcp     — Print MCP connection config for Claude Code
    nebo skill   — List or install nebo-shipped agent skills

Pipelines are launched directly from the shell (e.g. `uv run python
my_script.py`) — the SDK auto-detects a running daemon and connects.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from nebo.server.workspace import (
    WorkspaceError,
    is_remote_uri,
    normalize_workspace,
)

_PID_DIR = Path.home() / ".nebo"
_PID_FILE = _PID_DIR / "server.pid"


def _get_version() -> str:
    """Resolve the installed package version (git-tag-derived via hatch-vcs).
    Falls back to "unknown" for an uninstalled source checkout."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("nebo")
    except PackageNotFoundError:
        return "unknown"

# argparse `const` for a bare `--remote` (no dir): resolve to <logdir>/remote/
# in cmd_serve, where the logdir is known.
_REMOTE_DEFAULT = object()


def _write_pid(pid: int) -> None:
    _PID_DIR.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(pid))


def _read_pid() -> int | None:
    try:
        if _PID_FILE.exists():
            return int(_PID_FILE.read_text().strip())
    except (ValueError, OSError):
        pass
    return None


def _remove_pid() -> None:
    try:
        _PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _is_alive(port: int) -> bool:
    from urllib.request import urlopen

    try:
        with urlopen(f"http://localhost:{port}/health", timeout=2.0) as resp:
            return resp.status == 200
    except Exception:
        return False


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def cmd_serve(args: argparse.Namespace) -> None:
    """Start the persistent daemon server."""
    port = args.port
    host = args.host

    if _is_alive(port):
        print(f"Nebo daemon is already running on {host}:{port}")
        return

    # --no-store and --store-dir were removed; emit a clear error if someone
    # still passes them (they'd be caught by argparse as unknown args, but
    # if they're typed as known args downstream we'd silently accept). The
    # argparse change above already rejects unknown args; this is belt-and-suspenders.

    # --logdir is the workspace root in every mode — it anchors the SQLite
    # cache identity, the meta/ tree, and the default remote dir. --no-local
    # only turns off the directory watcher; it no longer nulls the logdir.
    # It may be a local directory or an hf:// bucket URI; normalize_workspace
    # canonicalizes both without ever Path.resolve()-ing a URI.
    try:
        logdir_key = normalize_workspace(args.logdir)
    except WorkspaceError as e:
        print(f"nebo serve: {e}", file=sys.stderr)
        sys.exit(2)
    logdir_remote = is_remote_uri(logdir_key)

    remote = getattr(args, "remote", None)
    ephemeral = getattr(args, "remote_ephemeral", False)

    if args.no_local and remote is None and not ephemeral:
        print(
            "nebo serve --no-local needs --remote or --remote-ephemeral — "
            "with the watcher off and no network intake, the daemon would "
            "ingest nothing.",
            file=sys.stderr,
        )
        sys.exit(2)

    remote_abs = None
    if remote is not None:
        # The remote writer appends to an open .nebo stream, which object
        # storage cannot do cheaply — it stays a local directory.
        if is_remote_uri(remote):
            print(
                "nebo serve: --remote must be a local directory, not a bucket "
                f"URI (got {remote}).\n"
                "  The daemon appends to an open .nebo stream, which object "
                "storage can't do.\n"
                "  Use --logdir for a bucket workspace.",
                file=sys.stderr,
            )
            sys.exit(2)
        if remote is _REMOTE_DEFAULT:
            if logdir_remote:
                print(
                    "nebo serve: --remote needs an explicit directory when "
                    f"--logdir is a bucket ({logdir_key}).\n"
                    "  There is no <logdir>/remote/ to default to.",
                    file=sys.stderr,
                )
                sys.exit(2)
            remote_abs = Path(logdir_key) / "remote"
        else:
            remote_abs = Path(remote).resolve()
        # Watcher input and the remote writer can't share a directory or they
        # feed back into each other. Nesting under the logdir is fine — the
        # watcher is non-recursive.
        if not args.no_local and str(remote_abs) == logdir_key:
            print(
                "nebo serve: --remote dir cannot be the watched --logdir.\n"
                f"  --logdir: {logdir_key}\n"
                f"  --remote: {remote_abs}\n"
                "  Use a subdirectory (the default <logdir>/remote/) or a "
                "separate path.",
                file=sys.stderr,
            )
            sys.exit(2)

    os.environ["NEBO_LOGDIR"] = logdir_key
    if getattr(args, "hf_token", None):
        os.environ["HF_TOKEN"] = args.hf_token
    if getattr(args, "poll_interval", None):
        os.environ["NEBO_POLL_INTERVAL"] = str(args.poll_interval)
    if args.no_local:
        os.environ["NEBO_NO_LOCAL"] = "1"
    if remote_abs is not None:
        os.environ["NEBO_REMOTE"] = str(remote_abs)
    if ephemeral:
        os.environ["NEBO_REMOTE_EPHEMERAL"] = "1"
    if getattr(args, "api_token", None):
        os.environ["NEBO_API_TOKEN"] = args.api_token
    if getattr(args, "read", None):
        os.environ["NEBO_READ_MODE"] = args.read
    if getattr(args, "write", None):
        os.environ["NEBO_WRITE_MODE"] = args.write

    # SQLite cache config (see nebo/server/cache.py). The daemon only
    # builds a cache when NEBO_CACHE_PATH is set, so the default path is
    # resolved here rather than server-side.
    if getattr(args, "no_cache", False):
        os.environ["NEBO_NO_CACHE"] = "1"
    else:
        if getattr(args, "cache_path", None):
            cache_path = Path(args.cache_path).resolve()
        else:
            from nebo.server.cache import resolve_cache_path
            cache_path = resolve_cache_path(logdir_key)
        os.environ["NEBO_CACHE_PATH"] = str(cache_path)
        # Friendly pre-check of the cache's single-owner lock so the common
        # mistake (a second daemon on the same logdir) fails here with a
        # clear message instead of inside uvicorn. Advisory only — RunCache
        # re-checks authoritatively at startup (CacheLockedError).
        from nebo.server.cache import cache_lock_holder
        holder = cache_lock_holder(cache_path)
        if holder is not None:
            pid = holder.get("pid")
            print(
                "nebo serve: another daemon is already serving this logdir's "
                f"cache ({cache_path})"
                + (f" — pid {pid}" if pid else "") + ".\n"
                "  Stop it first (nebo stop), or use a different "
                "--logdir/--cache-path.",
                file=sys.stderr,
            )
            sys.exit(2)
    if getattr(args, "ram_budget", None):
        os.environ["NEBO_RAM_BUDGET_MB"] = str(args.ram_budget)
    if getattr(args, "media_lru", None):
        os.environ["NEBO_MEDIA_LRU_MB"] = str(args.media_lru)
    if getattr(args, "cache_retention_days", None):
        os.environ["NEBO_CACHE_RETENTION_DAYS"] = str(args.cache_retention_days)

    # Say what the daemon is serving and, for an archive, that it only reads
    # it — otherwise the first refused `nebo groups add` is a surprise.
    workspace_line = (
        f"  workspace: {logdir_key}"
        + (" (read-only archive)" if logdir_remote else "")
    )

    if args.daemon:
        # Background mode
        cmd = [
            sys.executable, "-m", "uvicorn",
            "nebo.server.daemon:create_daemon_app",
            "--factory",
            "--host", host,
            "--port", str(port),
            "--log-level", "warning",
        ]
        # Every NEBO_* var (logdir, mode, cache, auth) was already written to
        # os.environ above, so the child inherits them via the copy.
        env = os.environ.copy()
        env["NEBO_DAEMON_PORT"] = str(port)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
        _write_pid(proc.pid)

        # Wait for server to come up
        for _ in range(50):
            if _is_alive(port):
                print(f"Nebo daemon started on {host}:{port} (PID {proc.pid})")
                print(workspace_line)
                return
            time.sleep(0.1)

        print(f"Nebo daemon started (PID {proc.pid}), but health check pending...")
        print(workspace_line)
    else:
        # Foreground mode
        _write_pid(os.getpid())
        os.environ["NEBO_DAEMON_PORT"] = str(port)
        print(f"Starting Nebo daemon on {host}:{port}...")
        print(workspace_line)
        print("Press Ctrl+C to stop.\n")
        import uvicorn
        try:
            uvicorn.run(
                "nebo.server.daemon:create_daemon_app",
                factory=True,
                host=host,
                port=port,
                log_level="info",
            )
        except KeyboardInterrupt:
            pass
        finally:
            _remove_pid()
            print("\nNebo daemon stopped.")


def _cache_db_info(path: Path) -> dict:
    """Read a cache db's identity without importing the full cache module."""
    import sqlite3

    logdir = None
    try:
        conn = sqlite3.connect(path)
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='logdir'"
            ).fetchone()
            logdir = row[0] if row else None
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    st = path.stat()
    return {
        "path": str(path),
        "logdir": logdir or None,
        "size_bytes": st.st_size,
        "mtime": st.st_mtime,
    }


def cmd_cache(args: argparse.Namespace) -> None:
    """Inspect or delete daemon cache databases. Pure file operations —
    no daemon required; the cache is disposable by design."""
    cache_dir = Path(
        getattr(args, "cache_dir", None) or Path.home() / ".nebo" / "cache"
    )
    dbs = sorted(cache_dir.glob("*.db")) if cache_dir.is_dir() else []

    if args.cache_command == "ls":
        infos = [_cache_db_info(p) for p in dbs]
        if getattr(args, "json", False):
            print(json.dumps({"caches": infos}))
            return
        if not infos:
            print("no cache databases")
            return
        for info in infos:
            mb = info["size_bytes"] / (1024 * 1024)
            print(f"{info['path']}  {mb:.1f} MB  logdir={info['logdir'] or '?'}")
        return

    if args.cache_command == "clear":
        if getattr(args, "all", False):
            targets = dbs
        elif getattr(args, "logdir", None):
            from nebo.server.cache import resolve_cache_path
            from nebo.server.workspace import normalize_workspace

            name = resolve_cache_path(args.logdir).name
            resolved = normalize_workspace(args.logdir)
            targets = [
                p for p in dbs
                if p.name == name or _cache_db_info(p)["logdir"] == resolved
            ]
        else:
            print("nebo cache clear: pass a LOGDIR or --all", file=sys.stderr)
            sys.exit(2)
        if not targets:
            print("nothing matched")
            return
        for p in targets:
            for side in (
                p,
                p.with_name(p.name + "-wal"),
                p.with_name(p.name + "-shm"),
            ):
                try:
                    side.unlink()
                except FileNotFoundError:
                    pass
            print(f"deleted {p}")
        return

    print("usage: nebo cache {ls,clear}", file=sys.stderr)
    sys.exit(2)


def cmd_status(args: argparse.Namespace) -> None:
    """Show daemon status and recent runs."""
    from nebo import client

    conn = _conn_kwargs(args)
    try:
        runs = client.get_run_history(**conn)
    except Exception:
        print("Nebo daemon is not running.")
        pid = _read_pid()
        if pid:
            print(f"  Stale PID file found: {pid}")
        return

    try:
        mode = client.get_health(**conn).get("mode")
    except Exception:
        mode = None

    if args.json:
        print(json.dumps(
            {"daemon": "running", "mode": mode, "runs": runs.get("runs", [])}
        ))
        return

    port = getattr(args, "port", None) or int(os.environ.get("NEBO_CLI_PORT", 7861))
    mode_note = f"  (mode: {mode})" if mode else ""
    print(f"Nebo daemon: running on port {port}{mode_note}")

    run_list = runs.get("runs", [])
    if run_list:
        print(f"\nRecent runs:")
        for r in run_list[-10:]:
            print(f"  {r['id']}: {r.get('script_path', '')} | "
                  f"nodes={r.get('node_count', 0)}, metrics={r.get('metric_series_count', 0)}, "
                  f"texts={r.get('text_count', 0)}")


def cmd_stop(args: argparse.Namespace) -> None:
    """Stop the daemon."""
    port = args.port
    pid = _read_pid()

    if pid and _process_alive(pid):
        os.kill(pid, signal.SIGTERM)
        print(f"Sent SIGTERM to daemon (PID {pid})")
        # Wait for it to die
        for _ in range(30):
            if not _process_alive(pid):
                break
            time.sleep(0.1)
        _remove_pid()
        print("Nebo daemon stopped.")
    elif _is_alive(port):
        print(f"Daemon is running on port {port} but PID unknown. Use kill manually.")
    else:
        print("Nebo daemon is not running.")
        _remove_pid()


def cmd_text_ls(args: argparse.Namespace) -> None:
    """View text entries from runs."""
    from nebo import client

    try:
        result = client.get_text(
            loggable_id=args.node,
            run_id=args.run,
            limit=args.limit,
            **_conn_kwargs(args),
        )
    except Exception as e:
        print(f"Error: {e}")
        return

    if args.json:
        print(json.dumps(result))
        return

    texts = result.get("texts", [])
    if not texts:
        print("No text entries found.")
        return

    for entry in texts:
        node_tag = f"[{entry.get('loggable_id', '?')}]" if entry.get("loggable_id") else ""
        name = entry.get("name") or "text"
        step = entry.get("step")
        step_tag = f"@{step}" if step is not None else ""
        print(f"  {node_tag} {name}{step_tag}: {entry.get('message', '')}")


def cmd_mcp(args: argparse.Namespace) -> None:
    """Print MCP connection config for Claude Code."""
    port = getattr(args, "port", 7861)
    nebo_args = ["mcp-stdio"]
    if port != 7861:
        nebo_args += ["--port", str(port)]
    config = {
        "mcpServers": {
            "nebo": {
                "command": "nebo",
                "args": nebo_args,
                "env": {},
            }
        }
    }
    print("# Add this to your Claude Code MCP config (~/.claude/mcp.json):")
    print(json.dumps(config, indent=2))


def _replay_nebo_file_to_remote(filepath: str, base_url: str, api_token: str | None) -> None:
    """Read a .nebo file locally and POST its events to a remote daemon.

    The daemon's `POST /load` accepts a server-side filepath, which
    doesn't exist when the daemon is on a Hugging Face Space and the
    file is on the user's machine. This walks the file with the
    existing reader and pushes events through `/events` instead — the
    SDK's own msgpack wire format (a concatenation of individually
    packed event maps), because media entries carry raw PNG/WAV bytes
    that a JSON body cannot represent.
    """
    import urllib.parse
    import urllib.request

    import msgpack

    from nebo.core.fileformat import NeboFileReader

    headers = {"Content-Type": "application/msgpack"}
    if api_token:
        headers["X-Nebo-Token"] = api_token

    with open(filepath, "rb") as f:
        reader = NeboFileReader(f)
        meta = reader.read_header()
        run_id = meta["run_id"]

        # Open the run on the remote side. The file body carries no
        # run_start entry — name/group/started_at/args live in the
        # header, so forward them the same way the watcher's shallow
        # registration does.
        events: list[dict] = [{
            "type": "run_start",
            "data": {
                "script_path": meta.get("script_path", os.path.basename(filepath)),
                "started_at": meta.get("started_at"),
                "run_name": meta.get("run_name"),
                "group": meta.get("group"),
                "args": meta.get("args", []),
            },
        }]
        while True:
            entry = reader.read_next_entry()
            if entry is None:
                break
            # The payload's own "type" key (spread second) wins over the
            # byte-derived one: alert frames are written as unregistered
            # byte 255 ("unknown_255") with the full event dict as payload.
            events.append({"type": entry["type"], **entry["payload"]})

    # Chunk by encoded byte size so a single POST never balloons past
    # what proxies / WAFs accept. 1 MB matches the SDK's own ceiling.
    packed = [msgpack.packb(evt, use_bin_type=True) for evt in events]
    chunk_limit = 1024 * 1024
    chunks: list[list[bytes]] = [[]]
    chunk_size = 0
    for frame in packed:
        if chunks[-1] and chunk_size + len(frame) > chunk_limit:
            chunks.append([])
            chunk_size = 0
        chunks[-1].append(frame)
        chunk_size += len(frame)

    target = f"{base_url.rstrip('/')}/events?run_id={urllib.parse.quote(run_id)}"
    sent = 0
    for chunk in chunks:
        if not chunk:
            continue
        req = urllib.request.Request(
            target, data=b"".join(chunk), method="POST", headers=headers,
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status} from {target}")
        sent += len(chunk)

    print(f"Loaded: {filepath} → {base_url} (run_id={run_id}, {sent} events in {len(chunks)} batch(es))")


def cmd_load(args: argparse.Namespace) -> None:
    """Load a .nebo file into the daemon (local or remote)."""
    from nebo import client

    filepath = os.path.abspath(args.file)
    if not os.path.exists(filepath):
        print(f"Error: File not found: {filepath}")
        sys.exit(1)

    # --url triggers the event-replay path (daemon can't see our filesystem).
    url = getattr(args, "url", None) or os.environ.get("NEBO_CLI_URL")
    api_token = getattr(args, "api_token", None) or os.environ.get("NEBO_API_TOKEN")

    if url:
        try:
            _replay_nebo_file_to_remote(filepath, url, api_token)
        except Exception as e:
            print(f"Error: {e}")
            sys.exit(1)
        if args.json:
            print(json.dumps({"status": "loaded", "filepath": filepath}))
        return

    try:
        data = client.load_file(filepath, **_conn_kwargs(args))
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

    if "error" in data:
        print(f"Error: {data['error']}")
        sys.exit(1)

    if args.json:
        print(json.dumps({"status": "loaded", "filepath": filepath, **data}))
        return

    print(f"Loaded: {filepath}")


def cmd_skills(args: argparse.Namespace) -> None:
    """List or install nebo-shipped agent skills."""
    # Imported lazily so `nebo --help` doesn't pay the cost.
    from nebo import skills
    from nebo.skills import install as skill_install

    action = getattr(args, "skills_action", None)

    if action == "list" or action is None:
        for name in skills.available_skills():
            body = skills.read_skill(name)
            # Extract description from frontmatter for a friendlier listing.
            description = ""
            if body.startswith("---"):
                end = body.find("\n---", 3)
                if end != -1:
                    fm = body[3:end]
                    for line in fm.splitlines():
                        if line.strip().startswith("description:"):
                            description = line.split(":", 1)[1].strip()
                            break
            print(f"{name}")
            if description:
                print(f"  {description}")
        return

    if action == "install":
        platform = getattr(args, "platform", "claude-code") or "claude-code"
        names = list(getattr(args, "names", []) or [])
        project = bool(getattr(args, "project", False))

        if platform == "all":
            platforms = list(skill_install.PLATFORMS)
        else:
            platforms = [platform]

        # No names → install every shipped skill (skill=None). Explicit
        # names install exactly those.
        merged: dict[str, list] = {}
        try:
            for target in names or [None]:
                results = skill_install.install(
                    platforms=platforms, skill=target, project=project,
                )
                for platform_name, paths in results.items():
                    bucket = merged.setdefault(platform_name, [])
                    for p in paths if isinstance(paths, list) else [paths]:
                        if p not in bucket:  # agents-md yields one path per pass
                            bucket.append(p)
        except (ValueError, FileNotFoundError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(2)

        for platform_name, paths in merged.items():
            for p in paths:
                print(f"{platform_name}: wrote {p}")
        return

    print(f"unknown skills action: {action!r}", file=sys.stderr)
    sys.exit(2)


def cmd_runs(args: argparse.Namespace) -> None:
    """Inspect runs: list / show / wait."""
    from nebo import client
    sub = args.runs_action
    conn = _conn_kwargs(args)
    if sub == "list":
        result = client.get_run_history(**conn)
        if args.json:
            print(json.dumps(result))
        else:
            for r in result.get("runs", []):
                rid = r.get("id", "")
                name = r.get("run_name") or ""
                started = r.get("started_at") or ""
                summary = (
                    f"nodes={r.get('node_count', 0)} "
                    f"metrics={r.get('metric_series_count', 0)}"
                )
                latest_step = r.get("latest_step")
                if latest_step is not None:
                    summary += f" step={latest_step}"
                print(f"{rid:<20} {name:<24} {started:<22} {summary}")
        return
    if sub == "show":
        result = client.get_run_status(args.run_id, **conn)
        if args.json:
            print(json.dumps(result))
        else:
            for k, v in result.items():
                print(f"{k}: {v}")
        return
    if sub == "mv":
        group = "" if args.root else (args.group or "")
        client.set_run_group(args.run_id, group, **conn)
        print(f"moved {args.run_id} -> {group or '(root)'}")
        return
    if sub == "wait":
        result = client.wait_for_alert(
            args.run_id,
            timeout=args.timeout,
            min_level=args.min_level,
            **conn,
        )
        if args.json:
            print(json.dumps(result))
        else:
            if result.get("status") == "alert":
                a = result["alert"]
                print(f"ALERT [{a.get('level_name', '')}] {a.get('title', '')}: {a.get('text', '')}")
            else:
                print(f"status: {result.get('status')}")
        return


def cmd_graph(args: argparse.Namespace) -> None:
    """Inspect the DAG: graph show."""
    from nebo import client
    result = client.get_graph(run_id=args.run, **_conn_kwargs(args))
    if args.json:
        print(json.dumps(result))
        return
    nodes = result.get("nodes", {})
    edges = result.get("edges", [])
    print(f"{len(nodes)} nodes, {len(edges)} edges")
    for nid, info in nodes.items():
        print(f"  {nid}  ({info.get('func_name', '')})")


def cmd_loggables(args: argparse.Namespace) -> None:
    """Inspect a loggable: loggables show <id>."""
    from nebo import client
    result = client.get_loggable_status(
        args.loggable_id, run_id=args.run, **_conn_kwargs(args),
    )
    if args.json:
        print(json.dumps(result))
        return
    for k, v in result.items():
        print(f"{k}: {v!r}")


def cmd_describe(args: argparse.Namespace) -> None:
    """Print workflow description."""
    from nebo import client
    result = client.get_description(run_id=args.run, **_conn_kwargs(args))
    if args.json:
        print(json.dumps(result))
        return
    print(result.get("workflow_description") or "<no description>")


def cmd_metrics(args: argparse.Namespace) -> None:
    """Read or write metrics: list / get / log."""
    from nebo import client
    sub = args.metrics_action
    conn = _conn_kwargs(args)

    if sub == "list":
        # Derive from run_status; if no --run given, hit history and pick latest.
        if args.run:
            status = client.get_run_status(args.run, **conn)
        else:
            history = client.get_run_history(**conn)
            runs = history.get("runs", [])
            if not runs:
                if args.json:
                    print(json.dumps({}))
                else:
                    print("(no runs)")
                return
            status = client.get_run_status(runs[-1]["id"], **conn)
        index = status.get("metrics_index", {})
        if args.json:
            print(json.dumps(index))
        else:
            for lid, names in index.items():
                print(f"{lid}: {', '.join(names)}")
        return

    if sub == "get":
        if args.values_only and not args.name:
            print("error: --values-only requires --name", file=sys.stderr)
            sys.exit(2)
        if args.runs and args.run:
            print("error: pass either --run or --runs, not both", file=sys.stderr)
            sys.exit(2)

        def _fetch(run_id):
            result = client.get_metrics(
                args.loggable_id,
                name=args.name,
                tag=args.tag,
                step=args.step,
                run_id=run_id,
                **conn,
            )
            metrics = result.get("metrics", {})
            if args.values_only:
                return (metrics.get(args.name) or {}).get("entries", [])
            return metrics

        def _print_human(payload, indent=""):
            if args.values_only:
                for e in payload:
                    print(f"{indent}{e.get('step')}\t{e.get('value')}")
            else:
                for mname, series in payload.items():
                    count = len(series.get("entries", []))
                    print(f"{indent}{mname} ({series.get('type')}): {count} entries")

        run_ids = [r.strip() for r in (args.runs or "").split(",") if r.strip()]
        if run_ids:
            # Cross-run fan-out: one daemon call per run, merged client-side.
            per_run = {rid: _fetch(rid) for rid in run_ids}
            if args.json:
                print(json.dumps({
                    "loggable_id": args.loggable_id,
                    "name": args.name,
                    "runs": per_run,
                }))
            else:
                for rid, payload in per_run.items():
                    print(f"{rid}:")
                    _print_human(payload, indent="  ")
            return

        payload = _fetch(args.run)
        if args.json:
            if args.values_only:
                print(json.dumps(payload))
            else:
                print(json.dumps({"metrics": payload}))
        else:
            _print_human(payload)
        return

    if sub == "log":
        entries = json.loads(args.entries_json)
        result = client.log_metric(entries, run_id=args.run, **conn)
        if args.json:
            print(json.dumps(result))
        else:
            print(result.get("status", "ok"))
        return


_ALERT_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}


def _parse_alert_level(raw: str) -> int:
    """Accept a numeric level or a name (DEBUG/INFO/WARN/ERROR)."""
    if raw.upper() in _ALERT_LEVELS:
        return _ALERT_LEVELS[raw.upper()]
    try:
        return int(raw)
    except ValueError:
        names = "/".join(_ALERT_LEVELS)
        raise argparse.ArgumentTypeError(
            f"invalid level {raw!r}; use {names} or an integer"
        )


_ALERT_LEVEL_NAMES = {v: k for k, v in _ALERT_LEVELS.items()}


def _format_alert_line(a: dict) -> str:
    level_name = (
        a.get("level_name")
        or _ALERT_LEVEL_NAMES.get(a.get("level"))
        or str(a.get("level", ""))
    )
    if a.get("triggered_by") == "cli":
        cond = a.get("condition_str") or a.get("condition") or ""
        fired = len(a.get("fired") or [])
        scope = f" run={a['run_id']}" if a.get("run_id") else ""
        return (
            f"{a.get('id', ''):<10} [cli]  [{level_name}] {a.get('title', '')} "
            f"when {cond}{scope} (fired {fired}x)"
        )
    return (
        f"{'':<10} [code] [{level_name}] {a.get('title', '')}"
        f"{': ' + a['text'] if a.get('text') else ''} (run={a.get('run_id', '?')})"
    )


def cmd_alerts(args: argparse.Namespace) -> None:
    """Manage alert rules: ls / get / set / rm."""
    from nebo import client
    sub = args.alerts_action
    conn = _conn_kwargs(args)

    if sub == "ls":
        result = client.list_alerts(run_id=args.run, **conn)
        if args.json:
            print(json.dumps(result))
            return
        alerts = result.get("alerts", [])
        if not alerts:
            print("(no alerts)")
            return
        for a in alerts:
            print(_format_alert_line(a))
        return

    if sub == "get":
        result = client.get_alert(args.rule_id, **conn)
        if args.json:
            print(json.dumps(result))
            return
        for k, v in result.items():
            print(f"{k}: {v}")
        return

    if sub == "set":
        try:
            condition = client.parse_condition(args.condition)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(2)
        result = client.set_alert(
            args.title,
            condition,
            text=args.text or "",
            level=args.level,
            loggable_id=args.loggable,
            run_id=args.run,
            **conn,
        )
        if args.json:
            print(json.dumps(result))
        else:
            print(f"created alert rule {result.get('id', '')}: "
                  f"{result.get('title', '')} when {args.condition.strip()}")
        return

    if sub == "rm":
        result = client.delete_alert(args.rule_id, **conn)
        if args.json:
            print(json.dumps(result))
        else:
            print(f"deleted alert rule {args.rule_id}")
        return


def cmd_mcp_stdio(args: argparse.Namespace) -> None:
    """Run the MCP stdio transport (bridges stdio <-> daemon HTTP)."""
    from nebo.mcp.stdio import run_stdio_bridge
    run_stdio_bridge(port=args.port)


def cmd_text_log(args: argparse.Namespace) -> None:
    """Write text entries."""
    from nebo import client
    entries = json.loads(args.entries_json)
    result = client.log_text(entries, run_id=args.run, **_conn_kwargs(args))
    print(json.dumps(result) if args.json else result.get("status", "ok"))


def cmd_text(args: argparse.Namespace) -> None:
    """Route `nebo text <action>`: `log` writes entries, `ls` reads them."""
    if args.text_action == "ls":
        cmd_text_ls(args)
    else:
        cmd_text_log(args)


def cmd_images_log(args: argparse.Namespace) -> None:
    """Write image entries."""
    from nebo import client
    entries = json.loads(args.entries_json)
    result = client.log_image(entries, run_id=args.run, **_conn_kwargs(args))
    print(json.dumps(result) if args.json else result.get("status", "ok"))


def cmd_audio_log(args: argparse.Namespace) -> None:
    """Write audio entries."""
    from nebo import client
    entries = json.loads(args.entries_json)
    result = client.log_audio(entries, run_id=args.run, **_conn_kwargs(args))
    print(json.dumps(result) if args.json else result.get("status", "ok"))


def _cmd_media_ls(args: argparse.Namespace, kind: str) -> None:
    """List a run's images or audio with their media ids."""
    from nebo import client

    lister = client.list_images if kind == "images" else client.list_audio
    result = lister(args.run, **_conn_kwargs(args))
    if args.json:
        print(json.dumps(result))
        return

    by_loggable = result.get(kind, {})
    entries = [(lid, e) for lid, es in by_loggable.items() for e in es]
    if not entries:
        print(f"No {kind} entries found.")
        return
    for lid, e in entries:
        step = e.get("step")
        step_tag = f"@{step}" if step is not None else ""
        sr_tag = f" sr={e['sr']}" if kind == "audio" and e.get("sr") else ""
        print(f"  [{lid}] {e.get('name')}{step_tag}{sr_tag} media_id={e.get('media_id')}")


def cmd_images(args: argparse.Namespace) -> None:
    """Route `nebo images <action>`: `log` writes, `ls` lists, `get` downloads."""
    if args.images_action == "ls":
        _cmd_media_ls(args, "images")
    elif args.images_action == "get":
        _cmd_media_get(args, "images")
    else:
        cmd_images_log(args)


def cmd_audio(args: argparse.Namespace) -> None:
    """Route `nebo audio <action>`: `log` writes, `ls` lists, `get` downloads."""
    if args.audio_action == "ls":
        _cmd_media_ls(args, "audio")
    elif args.audio_action == "get":
        _cmd_media_get(args, "audio")
    else:
        cmd_audio_log(args)


# Extensions for `images/audio get` filenames when no --out is given; the
# daemon sniffs media to exactly these two types (see _sniff_mime).
_MEDIA_EXTENSIONS = {"image/png": ".png", "audio/wav": ".wav"}


def _cmd_media_get(args: argparse.Namespace, kind: str) -> None:
    """Download one media object to a file and print its path, so an
    agent can chain into reading/attaching the file. The command is
    kind-scoped (`images get` / `audio get`): fetching the other kind's
    media_id errors with a pointer instead of writing a mislabeled file."""
    from nebo import client

    data, ctype = client.get_media(args.run, args.media_id, **_conn_kwargs(args))
    base_ctype = ctype.split(";")[0].strip()
    want = "image/" if kind == "images" else "audio/"
    if not base_ctype.startswith(want):
        other = "audio" if kind == "images" else "images"
        print(
            f"media '{args.media_id}' is {base_ctype}, not {want}* — "
            f"use `nebo {other} get`",
            file=sys.stderr,
        )
        sys.exit(1)
    out = Path(
        args.out
        or f"{args.media_id}{_MEDIA_EXTENSIONS.get(base_ctype, '.bin')}"
    )
    out.write_bytes(data)
    if args.json:
        print(json.dumps({
            "path": str(out),
            "content_type": base_ctype,
            "bytes": len(data),
        }))
    else:
        print(out.resolve())


def cmd_actions(args: argparse.Namespace) -> None:
    """Route `nebo actions <action>`: `ls` lists scenes, `get` downloads a model."""
    if args.actions_action == "get":
        _cmd_actions_get(args)
    else:
        _cmd_actions_ls(args)


def _cmd_actions_ls(args: argparse.Namespace) -> None:
    """List a run's 3D scenes and the body models they reference.

    Read-only by design: logging poses means shipping arrays of per-body
    transforms, which is a job for the SDK, not a shell.
    """
    from nebo import client

    result = client.list_actions(
        args.run, limit=0, **_conn_kwargs(args)
    )
    if args.json:
        print(json.dumps(result))
        return

    scenes: dict[tuple[str, str], dict] = {}
    for lid, frames in (result.get("actions") or {}).items():
        for frame in frames:
            key = (lid, frame.get("name", ""))
            scene = scenes.setdefault(
                key, {"frames": 0, "instances": set(), "steps": []}
            )
            scene["frames"] += 1
            scene["instances"].update(frame.get("instances") or {})
            if frame.get("step") is not None:
                scene["steps"].append(frame["step"])

    if not scenes:
        print("No action scenes found.")
    else:
        print(f"{'SCENE':<28} {'INSTANCES':<28} {'FRAMES':>7}  STEPS")
        for (lid, name), scene in sorted(scenes.items()):
            steps = scene["steps"]
            span = f"{min(steps)}-{max(steps)}" if steps else "-"
            print(
                f"  [{lid}] {name}"[:28].ljust(28) + " "
                + ",".join(sorted(scene["instances"]))[:28].ljust(28) + " "
                + f"{scene['frames']:>7}  {span}"
            )

    models = result.get("body_models") or {}
    if models:
        print("\nMODELS")
        for model_id, m in sorted(models.items()):
            print(
                f"  {m.get('name', '')}  model_id={model_id}  "
                f"{len(m.get('body_names') or [])} bodies  "
                f"{m.get('source_format', '')}  "
                f"media_id={m.get('media_id', '')}"
            )


def _cmd_actions_get(args: argparse.Namespace) -> None:
    """Download one body model's GLB and print its path."""
    from nebo import client

    conn = _conn_kwargs(args)
    result = client.list_actions(args.run, limit=1, **conn)
    models = result.get("body_models") or {}
    model = models.get(args.model_id)
    if model is None:
        known = ", ".join(sorted(models)) or "none"
        print(
            f"no body model '{args.model_id}' in run '{args.run}' "
            f"(known: {known})",
            file=sys.stderr,
        )
        sys.exit(1)

    data, ctype = client.get_media(args.run, model["media_id"], **conn)
    out = Path(args.out or f"{args.model_id}.glb")
    out.write_bytes(data)
    if args.json:
        print(json.dumps({
            "path": str(out),
            "content_type": ctype.split(";")[0].strip(),
            "bytes": len(data),
            "body_names": model.get("body_names") or [],
        }))
    else:
        print(out.resolve())


def _lazy_deploy(args: argparse.Namespace) -> None:
    """Defer the huggingface_hub import — it's an optional dependency."""
    from nebo.cli_deploy import cmd_deploy
    cmd_deploy(args)


def _common_conn_parser() -> argparse.ArgumentParser:
    """Parent parser carrying connection + output flags every read/write
    subcommand inherits via ``parents=[...]``. Uses ``add_help=False`` so
    help text isn't duplicated.
    """
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument(
        "--url",
        help="Daemon URL (overrides --port). Default: NEBO_CLI_URL env or http://localhost:7861.",
    )
    p.add_argument(
        "--port",
        type=int,
        help="Daemon port. Default: NEBO_CLI_PORT env or 7861.",
    )
    p.add_argument(
        "--api-token",
        help="X-Nebo-Token to send with requests. Default: NEBO_API_TOKEN env.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human-formatted output.",
    )
    return p


def _conn_kwargs(args: argparse.Namespace) -> dict:
    """Translate parsed args into nebo.client connection kwargs."""
    return {
        "url": getattr(args, "url", None),
        "port": getattr(args, "port", None),
        "api_token": getattr(args, "api_token", None),
    }


def _run_names(client, conn) -> dict:
    try:
        return {
            r["id"]: (r.get("run_name") or "")
            for r in client.get_run_history(**conn).get("runs", [])
        }
    except Exception:
        return {}


def cmd_tree(args: argparse.Namespace) -> None:
    """Render the whole run tree (groups + placed/root runs + docs)."""
    from nebo import client
    conn = _conn_kwargs(args)
    tree = client.get_tree(**conn)
    if args.json:
        print(json.dumps(tree))
        return
    groups = tree.get("groups", {})
    placements = tree.get("runs", {})  # run_id -> group (non-root only)
    names = _run_names(client, conn)
    by_group: dict[str, list[str]] = {}
    for rid, gp in placements.items():
        by_group.setdefault(gp, []).append(rid)

    def _run_line(indent: str, rid: str) -> str:
        return f"{indent}- {rid}  {names.get(rid, '')}".rstrip()

    if not groups and not names:
        print("(empty tree — no groups or runs yet)")
        return
    for gp in sorted(groups):
        indent = "  " * gp.count("/")
        docs = groups[gp].get("docs", [])
        doc_note = f"   [{', '.join(docs)}]" if docs else ""
        print(f"{indent}{gp.rsplit('/', 1)[-1]}/{doc_note}")
        for rid in sorted(by_group.get(gp, [])):
            print(_run_line(indent + "  ", rid))
    root = sorted(rid for rid in names if rid not in placements)
    if root:
        print("(root)")
        for rid in root:
            print(_run_line("  ", rid))


def cmd_groups(args: argparse.Namespace) -> None:
    """Create / list / move / delete groups and their markdown docs."""
    from nebo import client
    conn = _conn_kwargs(args)
    action = args.groups_action
    if action == "add":
        client.create_group(args.path, **conn)
        print(f"created group {args.path}")
    elif action == "mv":
        client.move_group(args.path, args.new_path, **conn)
        print(f"moved {args.path} -> {args.new_path}")
    elif action == "rm":
        client.delete_group(args.path, **conn)
        print(f"deleted group {args.path}")
    elif action == "ls":
        _groups_ls(args, client, conn)
    elif action == "doc":
        _groups_doc(args, client, conn)


def _groups_ls(args, client, conn) -> None:
    path = (getattr(args, "path", None) or "").strip("/")
    tree = client.get_tree(**conn)
    groups = tree.get("groups", {})
    placements = tree.get("runs", {})
    if path:
        prefix = path + "/"
        subgroups = sorted(
            g for g in groups
            if g.startswith(prefix) and "/" not in g[len(prefix):]
        )
        members = sorted(rid for rid, gp in placements.items() if gp == path)
        docs = groups.get(path, {}).get("docs", [])
    else:
        subgroups = sorted(g for g in groups if "/" not in g)
        placed = set(placements)
        members = sorted(rid for rid in _run_names(client, conn) if rid not in placed)
        docs = []
    if args.json:
        print(json.dumps(
            {"path": path, "subgroups": subgroups, "runs": members, "docs": docs}
        ))
        return
    names = _run_names(client, conn)
    if subgroups:
        print("subgroups:")
        for g in subgroups:
            print(f"  {g}")
    if members:
        print("runs:")
        for rid in members:
            print(f"  {rid}  {names.get(rid, '')}".rstrip())
    if docs:
        print("docs:")
        for d in docs:
            print(f"  {d}")
    if not (subgroups or members or docs):
        print("(empty)")


def _groups_doc(args, client, conn) -> None:
    action = args.doc_action
    if action == "ls":
        tree = client.get_tree(**conn)
        docs = tree.get("groups", {}).get(
            args.path.strip("/"), {}
        ).get("docs", [])
        if args.json:
            print(json.dumps({"docs": docs}))
        else:
            for d in docs:
                print(d)
    elif action == "get":
        content = client.get_group_doc(args.path, args.name, **conn)
        if content is None:
            print(f"doc not found: {args.path}/{args.name}", file=sys.stderr)
            sys.exit(1)
        print(content)
    elif action == "set":
        if args.file:
            content = Path(args.file).read_text()
        else:
            content = args.text or ""
        client.set_group_doc(args.path, args.name, content, **conn)
        print(f"wrote {args.path}/{args.name}")
    elif action == "rm":
        client.delete_group_doc(args.path, args.name, **conn)
        print(f"deleted {args.path}/{args.name}")


def main() -> None:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="nebo",
        description="Nebo - Multi-modal logging for Python",
    )
    parser.add_argument(
        "-v", "--version",
        action="version",
        version=f"nebo {_get_version()}",
        help="Show the nebo version and exit",
    )
    parser.add_argument("--port", type=int, default=7861, help="Daemon port (default: 7861)")
    subparsers = parser.add_subparsers(dest="command")

    # serve
    p_serve = subparsers.add_parser("serve", help="Start the persistent daemon server")
    p_serve.add_argument("--host", default="localhost", help="Host to bind (default: localhost)")
    p_serve.add_argument("--port", type=int, default=7861, help="Port (default: 7861)")
    p_serve.add_argument("--daemon", "-d", action="store_true", help="Run in background")
    p_serve.add_argument(
        "--logdir",
        default=".nebo",
        help="Workspace the daemon watches for .nebo files (default: ./.nebo). "
             "Either a local directory or a Hugging Face repo, e.g. "
             "hf://datasets/acme/runs — a bucket workspace keeps runs durable "
             "independently of the machine serving them.",
    )
    p_serve.add_argument(
        "--hf-token",
        help="Hugging Face token for an hf:// --logdir (defaults to HF_TOKEN "
             "env / cached login). Read access is enough to serve runs; write "
             "access is only needed to persist the run tree.",
    )
    p_serve.add_argument(
        "--poll-interval", type=float,
        help="Seconds between workspace scans (default: 0.5 local, 30 for a "
             "bucket, where each scan is an HTTP request).",
    )
    p_serve.add_argument(
        "--no-local",
        action="store_true",
        help="Disable the directory watcher (requires --remote or "
             "--remote-ephemeral; the logdir still anchors cache + meta/).",
    )
    _remote_group = p_serve.add_mutually_exclusive_group()
    _remote_group.add_argument(
        "--remote",
        nargs="?",
        const=_REMOTE_DEFAULT,
        default=None,
        metavar="DIR",
        help="Accept runs over the network and persist them as .nebo files in "
             "DIR (default <logdir>/remote/). Always a local directory — the "
             "daemon appends to an open stream. Without --remote/-ephemeral, "
             "network runs are rejected.",
    )
    _remote_group.add_argument(
        "--remote-ephemeral",
        action="store_true",
        help="Accept network runs but do not persist them (RAM + cache only) "
             "— for CI, demos, and tests.",
    )
    p_serve.add_argument("--api-token", help="Require this token on API requests via X-Nebo-Token / ?token=. Sets NEBO_API_TOKEN.")
    p_serve.add_argument("--read", choices=["public", "private"], help="Read access mode (default: public). Only matters when --api-token is set.")
    p_serve.add_argument("--write", choices=["public", "private"], help="Write access mode (default: private). Only matters when --api-token is set.")
    p_serve.add_argument(
        "--cache-path",
        help="SQLite cache db path (default: ~/.nebo/cache/<logdir-hash>.db).",
    )
    p_serve.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable the SQLite cache: pure-RAM daemon, no eviction.",
    )
    p_serve.add_argument(
        "--ram-budget", type=int,
        help="RAM budget for resident run data, in MB (default: 384).",
    )
    p_serve.add_argument(
        "--media-lru", type=int,
        help="In-RAM media byte-cache budget, in MB (default: 256).",
    )
    p_serve.add_argument(
        "--cache-retention-days", type=int,
        help="At startup, delete cache dbs untouched for this many days (default: 30).",
    )

    # cache
    p_cache = subparsers.add_parser(
        "cache", help="Inspect or delete the daemon's SQLite cache databases",
    )
    cache_sub = p_cache.add_subparsers(dest="cache_command")
    p_cache_ls = cache_sub.add_parser("ls", help="List cache databases")
    p_cache_ls.add_argument("--json", action="store_true", help="JSON output")
    p_cache_ls.add_argument("--cache-dir", help=argparse.SUPPRESS)
    p_cache_clear = cache_sub.add_parser("clear", help="Delete cache databases")
    p_cache_clear.add_argument(
        "logdir", nargs="?",
        help="Logdir whose cache db to delete (matches by path hash or recorded logdir)",
    )
    p_cache_clear.add_argument("--all", action="store_true", help="Delete every cache db")
    p_cache_clear.add_argument("--cache-dir", help=argparse.SUPPRESS)

    # status
    p_status = subparsers.add_parser(
        "status", parents=[_common_conn_parser()], help="Show daemon status",
    )

    # stop
    p_stop = subparsers.add_parser("stop", help="Stop the daemon")
    p_stop.add_argument("--port", type=int, default=7861)

    # load
    p_load = subparsers.add_parser(
        "load", parents=[_common_conn_parser()], help="Load a .nebo file into the daemon",
    )
    p_load.add_argument("file", help="Path to .nebo file")

    # mcp
    p_mcp = subparsers.add_parser("mcp", help="Print MCP config for Claude Code")
    p_mcp.add_argument("--port", type=int, default=7861, help="Daemon port to embed in the MCP config")

    # mcp-stdio (internal)
    p_mcp_stdio = subparsers.add_parser("mcp-stdio", help="Run MCP stdio transport")
    p_mcp_stdio.add_argument("--port", type=int, default=7861)

    # skills
    p_skills = subparsers.add_parser(
        "skills", help="List or install nebo-shipped agent skills",
    )
    skills_subparsers = p_skills.add_subparsers(dest="skills_action")
    skills_subparsers.add_parser("list", help="List available skills")
    p_skills_install = skills_subparsers.add_parser(
        "install", help="Install agent skills (all of them by default)",
    )
    p_skills_install.add_argument(
        "names", nargs="*", metavar="SKILL",
        help="Skills to install (default: all of them). `nebo skills list` shows options.",
    )
    p_skills_install.add_argument(
        "--platform",
        choices=["claude-code", "codex", "agents-md", "all"],
        default="claude-code",
        help="Target platform (default: claude-code)",
    )
    p_skills_install.add_argument(
        "--project",
        action="store_true",
        help="For claude-code: install under ./.claude/skills instead of ~/.claude/skills",
    )

    # runs
    p_runs = subparsers.add_parser("runs", help="Inspect runs")
    runs_sub = p_runs.add_subparsers(dest="runs_action", required=True)
    runs_sub.add_parser(
        "list",
        parents=[_common_conn_parser()],
        help="List all runs known to the daemon",
    )
    p_runs_show = runs_sub.add_parser(
        "show",
        parents=[_common_conn_parser()],
        help="Show summary for one run",
    )
    p_runs_show.add_argument("run_id")
    p_runs_wait = runs_sub.add_parser(
        "wait",
        parents=[_common_conn_parser()],
        help="Block until an alert fires for the run",
    )
    p_runs_wait.add_argument("run_id")
    p_runs_wait.add_argument("--timeout", type=float, default=300.0)
    p_runs_wait.add_argument("--min-level", type=int, default=20)
    p_runs_mv = runs_sub.add_parser(
        "mv", parents=[_common_conn_parser()],
        help="Move a run into a group (or --root)",
    )
    p_runs_mv.add_argument("run_id")
    p_runs_mv.add_argument("group", nargs="?", help="Destination group path")
    p_runs_mv.add_argument(
        "--root", action="store_true", help="Move the run to the root (no group)"
    )

    # tree
    p_tree = subparsers.add_parser(
        "tree", parents=[_common_conn_parser()],
        help="Render the run tree (groups + runs)",
    )

    # groups
    p_groups = subparsers.add_parser("groups", help="Manage run groups and docs")
    groups_sub = p_groups.add_subparsers(dest="groups_action", required=True)
    p_g_add = groups_sub.add_parser(
        "add", parents=[_common_conn_parser()], help="Create a group (with ancestors)"
    )
    p_g_add.add_argument("path")
    p_g_ls = groups_sub.add_parser(
        "ls", parents=[_common_conn_parser()],
        help="List a group's subgroups, member runs, and docs",
    )
    p_g_ls.add_argument("path", nargs="?", help="Group path (root if omitted)")
    p_g_mv = groups_sub.add_parser(
        "mv", parents=[_common_conn_parser()], help="Rename/move a group subtree"
    )
    p_g_mv.add_argument("path")
    p_g_mv.add_argument("new_path")
    p_g_rm = groups_sub.add_parser(
        "rm", parents=[_common_conn_parser()],
        help="Delete an empty group (refuses if it has runs or subgroups)",
    )
    p_g_rm.add_argument("path")
    p_g_doc = groups_sub.add_parser("doc", help="Manage a group's markdown docs")
    doc_sub = p_g_doc.add_subparsers(dest="doc_action", required=True)
    p_d_ls = doc_sub.add_parser(
        "ls", parents=[_common_conn_parser()], help="List a group's docs"
    )
    p_d_ls.add_argument("path")
    p_d_get = doc_sub.add_parser(
        "get", parents=[_common_conn_parser()], help="Print a doc's markdown"
    )
    p_d_get.add_argument("path")
    p_d_get.add_argument("name")
    p_d_set = doc_sub.add_parser(
        "set", parents=[_common_conn_parser()], help="Write a doc (--file or --text)"
    )
    p_d_set.add_argument("path")
    p_d_set.add_argument("name")
    _d_src = p_d_set.add_mutually_exclusive_group(required=True)
    _d_src.add_argument("--file", help="Read the doc body from this file")
    _d_src.add_argument("--text", help="Doc body given inline")
    p_d_rm = doc_sub.add_parser(
        "rm", parents=[_common_conn_parser()], help="Delete a doc"
    )
    p_d_rm.add_argument("path")
    p_d_rm.add_argument("name")

    # graph
    p_graph = subparsers.add_parser("graph", help="Inspect the DAG")
    graph_sub = p_graph.add_subparsers(dest="graph_action", required=True)
    p_gs = graph_sub.add_parser("show", parents=[_common_conn_parser()], help="Show DAG nodes and edges")
    p_gs.add_argument("--run", help="Run id (latest if omitted)")

    # loggables
    p_logg = subparsers.add_parser("loggables", help="Inspect a loggable")
    logg_sub = p_logg.add_subparsers(dest="loggables_action", required=True)
    p_ls = logg_sub.add_parser("show", parents=[_common_conn_parser()], help="Show a loggable's status")
    p_ls.add_argument("loggable_id")
    p_ls.add_argument("--run", help="Run id (latest if omitted)")

    # describe
    p_desc = subparsers.add_parser("describe", parents=[_common_conn_parser()], help="Print the workflow description")
    p_desc.add_argument("--run", help="Run id (latest if omitted)")

    # alerts
    p_alerts = subparsers.add_parser("alerts", help="Manage alert rules")
    alerts_sub = p_alerts.add_subparsers(dest="alerts_action", required=True)

    p_als = alerts_sub.add_parser("ls", parents=[_common_conn_parser()], help="List alert rules and code-fired alerts")
    p_als.add_argument("--run", help="Scope to one run id")

    p_alg = alerts_sub.add_parser("get", parents=[_common_conn_parser()], help="Show one alert rule")
    p_alg.add_argument("rule_id")

    p_alset = alerts_sub.add_parser("set", parents=[_common_conn_parser()], help="Create an alert rule on a metric condition")
    p_alset.add_argument("--title", required=True, help="Alert headline")
    p_alset.add_argument("--text", help="Optional body / details")
    p_alset.add_argument(
        "--condition", required=True,
        help=(
            "Metric condition, e.g. 'train/loss > 5'. Ops: > >= < <= == !=. "
            "The reserved metric 'last_event' fires on idle seconds — "
            "'last_event > 60' fires once a run has been quiet for 60s "
            "(ops > >= only; nebo's completion signal)"
        ),
    )
    p_alset.add_argument(
        "--level", type=_parse_alert_level, default=20,
        help="Severity: DEBUG/INFO/WARN/ERROR or an integer (default INFO)",
    )
    p_alset.add_argument("--loggable", help="Only match the metric on this loggable id")
    p_alset.add_argument("--run", help="Only apply to this run id (default: all runs)")

    p_alrm = alerts_sub.add_parser("rm", parents=[_common_conn_parser()], help="Delete an alert rule")
    p_alrm.add_argument("rule_id")

    # metrics
    p_metrics = subparsers.add_parser("metrics", help="Read or write metrics")
    metrics_sub = p_metrics.add_subparsers(dest="metrics_action", required=True)

    p_ml = metrics_sub.add_parser("list", parents=[_common_conn_parser()], help="List metric names per loggable")
    p_ml.add_argument("--run", help="Run id (latest if omitted)")

    p_mg = metrics_sub.add_parser("get", parents=[_common_conn_parser()], help="Fetch metric entries for a loggable")
    p_mg.add_argument("loggable_id")
    p_mg.add_argument("--name", help="Filter to a specific metric name")
    p_mg.add_argument("--tag", help="Filter line/scatter entries by tag")
    p_mg.add_argument("--step", type=int, help="Filter entries by exact step")
    p_mg.add_argument("--run", help="Run id (latest if omitted)")
    p_mg.add_argument(
        "--runs",
        help="Comma-separated run ids for a cross-run query; emits {run_id: series} keyed by run.",
    )
    p_mg.add_argument(
        "--values-only",
        action="store_true",
        help="With --name: emit just the entries array [{step, value, tags, timestamp}, ...].",
    )

    p_mlog = metrics_sub.add_parser("log", parents=[_common_conn_parser()], help="Write metric entries")
    p_mlog.add_argument("--entries-json", required=True, help="JSON list of metric entries")
    p_mlog.add_argument("--run", help="Run id")

    # text / images / audio  (each has a "log" write action; text also reads)
    def _add_log_subparser(name: str):
        p = subparsers.add_parser(name, help=f"Read/write {name} entries")
        sub = p.add_subparsers(dest=f"{name}_action", required=True)
        plog = sub.add_parser("log", parents=[_common_conn_parser()], help=f"Write {name} entries")
        plog.add_argument("--entries-json", required=True, help="JSON list of entries")
        plog.add_argument("--run", help="Run id")
        return sub

    text_sub = _add_log_subparser("text")
    p_text_ls = text_sub.add_parser(
        "ls", parents=[_common_conn_parser()], help="View text entries",
    )
    p_text_ls.add_argument("--run", help="Run ID")
    p_text_ls.add_argument("--node", help="Filter by node")
    p_text_ls.add_argument("--limit", type=int, default=100)

    # images/audio `ls` lists a run's media with content-addressed
    # media_ids; `get` then downloads one object by id.
    for name, sub in (("images", _add_log_subparser("images")),
                      ("audio", _add_log_subparser("audio"))):
        p_media_ls = sub.add_parser(
            "ls", parents=[_common_conn_parser()], help=f"List a run's {name}",
        )
        p_media_ls.add_argument("--run", required=True, help="Run ID")
        p_media_get = sub.add_parser(
            "get", parents=[_common_conn_parser()],
            help=f"Download one of a run's {name} (id from `{name} ls`) to a file",
        )
        p_media_get.add_argument("media_id", help="Content-addressed media id")
        p_media_get.add_argument("--run", required=True, help="Run ID")
        p_media_get.add_argument(
            "-o", "--out",
            help="Output path (default: <media_id> + extension from content type)",
        )

    # actions: read-only view of the 3D scene modality
    p_actions = subparsers.add_parser(
        "actions", help="List a run's 3D scenes and body models",
    )
    actions_sub = p_actions.add_subparsers(dest="actions_action", required=True)
    p_actions_ls = actions_sub.add_parser(
        "ls", parents=[_common_conn_parser()],
        help="List a run's action scenes, instances and body models",
    )
    p_actions_ls.add_argument("--run", required=True, help="Run ID")
    p_actions_get = actions_sub.add_parser(
        "get", parents=[_common_conn_parser()],
        help="Download one body model's GLB (id from `actions ls`) to a file",
    )
    p_actions_get.add_argument("model_id", help="Body model id from `actions ls`")
    p_actions_get.add_argument("--run", required=True, help="Run ID")
    p_actions_get.add_argument(
        "-o", "--out", help="Output path (default: <model_id>.glb)",
    )

    # deploy
    p_deploy = subparsers.add_parser(
        "deploy",
        help="Deploy the nebo daemon to a Hugging Face Space",
    )
    p_deploy.add_argument("--space-id", required=True, help="Hugging Face Space ID, e.g. 'username/my-dashboard'")
    p_deploy.add_argument("--hf-token", help="Hugging Face write token (defaults to HF_TOKEN env / cached login)")
    p_deploy.add_argument("--api-token", help="Token clients must send via X-Nebo-Token. Random if omitted.")
    p_deploy.add_argument("--private", action="store_true", help="Create the Space as private")
    p_deploy.add_argument(
        "--logdir",
        help="Serve runs from a Hugging Face archive, e.g. "
             "hf://buckets/acme/runs, instead of the Space's ephemeral /data "
             "volume. Runs then survive rebuilds and scale-to-zero. The Space "
             "only reads the archive; publish to it separately.",
    )
    p_deploy.add_argument("--from-source", action="store_true", help="Build a wheel from this checkout and ship it instead of installing from PyPI")
    p_deploy.add_argument("--read", choices=["public", "private"], default="public", help="Read access mode (default: public — anyone can view).")
    p_deploy.add_argument("--write", choices=["public", "private"], default="private", help="Write access mode (default: private — token required to push events / control runs).")
    p_deploy.add_argument("--no-wait", dest="wait", action="store_false", default=True, help="Return as soon as files are uploaded; don't wait for the Space rebuild to finish.")

    args = parser.parse_args()

    commands = {
        "serve": cmd_serve,
        "cache": cmd_cache,
        "status": cmd_status,
        "stop": cmd_stop,
        "load": cmd_load,
        "mcp": cmd_mcp,
        "mcp-stdio": cmd_mcp_stdio,
        "skills": cmd_skills,
        "deploy": _lazy_deploy,
        "runs": cmd_runs,
        "tree": cmd_tree,
        "groups": cmd_groups,
        "graph": cmd_graph,
        "loggables": cmd_loggables,
        "describe": cmd_describe,
        "alerts": cmd_alerts,
        "metrics": cmd_metrics,
        "text": cmd_text,
        "images": cmd_images,
        "actions": cmd_actions,
        "audio": cmd_audio,
    }

    handler = commands.get(args.command)
    if not handler:
        parser.print_help()
        return

    # Translate daemon errors into clean messages + non-zero exit codes so
    # agents and shell pipelines see a usable error instead of a Python
    # traceback. HTTPError covers auth (401), missing routes (404), and
    # malformed query params (422). URLError covers daemon-not-running.
    import urllib.error

    try:
        handler(args)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        msg = f"daemon returned HTTP {e.code}"
        if e.code == 401:
            msg += " (unauthorized — pass --api-token or set NEBO_API_TOKEN)"
        if body:
            msg += f": {body}"
        print(msg, file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(
            f"could not reach the nebo daemon: {e.reason}. "
            "Start it with `nebo serve` (default port 7861).",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
