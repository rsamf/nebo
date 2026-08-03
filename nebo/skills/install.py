"""Install nebo skills into agent-platform-specific locations.

Three platforms are supported:

- ``claude-code``: copies ``SKILL.md`` to ``~/.claude/skills/nebo-<name>/SKILL.md``
  (user-level, the default) or ``.claude/skills/nebo-<name>/SKILL.md`` under
  the current directory (project-level, ``--project``). The ``nebo-`` prefix
  on the directory keeps these visually grouped and distinguishable from
  other skills installed in the same tree.

- ``codex``: same layout under Codex CLI's skills tree —
  ``~/.codex/skills/nebo-<name>/SKILL.md`` (user-level) or
  ``.codex/skills/…`` (``--project``). Codex discovers any directory in
  that tree containing a ``SKILL.md``, and its frontmatter (name +
  description) is the same format nebo skills already carry.

- ``agents-md``: upserts the skill content into ``AGENTS.md`` in the current
  directory. A pair of HTML markers (``<!-- nebo-skill:<name> start -->`` …
  ``<!-- nebo-skill:<name> end -->``) brackets the section so subsequent
  installs replace it instead of duplicating.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable

from . import available_skills, read_skill


PLATFORMS = ("claude-code", "codex", "agents-md")


# Allow tests / power users to override the default platform homes.
_CLAUDE_HOME_ENV = "NEBO_CLAUDE_HOME"
_CODEX_HOME_ENV = "NEBO_CODEX_HOME"
_AGENTS_MD_ENV = "NEBO_AGENTS_MD_DIR"


def _resolve_skills(skill: str | None) -> list[str]:
    all_skills = available_skills()
    if skill is None or skill == "all":
        return all_skills
    if skill not in all_skills:
        raise ValueError(
            f"unknown skill {skill!r}; available: {', '.join(all_skills) or 'none'}"
        )
    return [skill]


def _platform_skills_dir(project: bool, dot_dir: str, home_env: str) -> Path:
    """Resolve a skills-tree platform's target directory.

    ``project=True`` → ``<cwd>/<dot_dir>/skills``; otherwise
    ``~/<dot_dir>/skills``, with ``home_env`` overriding the home for
    tests (its value replaces the ``~/<dot_dir>`` part).
    """
    if project:
        return Path.cwd() / dot_dir / "skills"
    home = os.environ.get(home_env)
    if home:
        return Path(home) / "skills"
    return Path.home() / dot_dir / "skills"


def _agents_md_path() -> Path:
    """Resolve the AGENTS.md path. ``NEBO_AGENTS_MD_DIR`` overrides cwd."""
    base = os.environ.get(_AGENTS_MD_ENV)
    if base:
        return Path(base) / "AGENTS.md"
    return Path.cwd() / "AGENTS.md"


def _claude_dirname(name: str) -> str:
    """Directory name to use under Claude Code's skills/ for `name`.

    Skills shipped with nebo land under ``nebo-<name>`` so they group
    visually alongside other ``nebo-*`` entries in the user's skills dir.
    Names that already carry the prefix (e.g. someone calling with
    ``skill="nebo-runs-qa"``) are kept as-is rather than double-prefixed.
    """
    return name if name.startswith("nebo-") else f"nebo-{name}"


def _install_skill_tree(base: Path, skill: str | None) -> list[Path]:
    """Write skills into a ``<base>/nebo-<name>/SKILL.md`` tree.

    Shared by the claude-code and codex platforms — both discover any
    directory containing a ``SKILL.md`` under their skills root.
    Returns the list of written file paths.
    """
    base.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name in _resolve_skills(skill):
        target_dir = base / _claude_dirname(name)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "SKILL.md"
        target.write_text(read_skill(name), encoding="utf-8")
        written.append(target)
    return written


def install_claude_code(skill: str | None = None, project: bool = False) -> list[Path]:
    """Install one or more skills into the Claude Code skills directory."""
    return _install_skill_tree(
        _platform_skills_dir(project, ".claude", _CLAUDE_HOME_ENV), skill,
    )


def install_codex(skill: str | None = None, project: bool = False) -> list[Path]:
    """Install one or more skills into the Codex CLI skills directory."""
    return _install_skill_tree(
        _platform_skills_dir(project, ".codex", _CODEX_HOME_ENV), skill,
    )


def install_agents_md(skill: str | None = None) -> Path:
    """Upsert one or more skills into ``AGENTS.md`` in the current dir.

    Idempotent: re-running replaces the bracketed section instead of duplicating.
    """
    path = _agents_md_path()
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    body = existing
    for name in _resolve_skills(skill):
        body = _upsert_section(body, name, read_skill(name))
    if not body.endswith("\n"):
        body += "\n"
    path.write_text(body, encoding="utf-8")
    return path


def _upsert_section(doc: str, name: str, content: str) -> str:
    """Replace or append the ``nebo-skill:<name>`` section in ``doc``."""
    start = f"<!-- nebo-skill:{name} start -->"
    end = f"<!-- nebo-skill:{name} end -->"
    block = f"{start}\n{content.rstrip()}\n{end}"
    pattern = re.compile(
        re.escape(start) + r".*?" + re.escape(end),
        flags=re.DOTALL,
    )
    if pattern.search(doc):
        return pattern.sub(block, doc)
    sep = "" if not doc or doc.endswith("\n\n") else ("\n" if doc.endswith("\n") else "\n\n")
    return doc + sep + block + "\n"


def install(
    platforms: Iterable[str],
    skill: str | None = None,
    project: bool = False,
) -> dict[str, list[Path] | Path]:
    """Install ``skill`` (or all) on each of ``platforms``.

    Returns a dict mapping platform -> path(s) written.
    """
    results: dict[str, list[Path] | Path] = {}
    for platform in platforms:
        if platform == "claude-code":
            results[platform] = install_claude_code(skill=skill, project=project)
        elif platform == "codex":
            results[platform] = install_codex(skill=skill, project=project)
        elif platform == "agents-md":
            results[platform] = install_agents_md(skill=skill)
        else:
            raise ValueError(
                f"unknown platform {platform!r}; supported: {', '.join(PLATFORMS)}"
            )
    return results
