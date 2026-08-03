"""Tests for the `nebo skill install` machinery."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from nebo import skills
from nebo.skills import install as skill_install


def test_available_skills_includes_runs_qa_and_instrumentation():
    names = skills.available_skills()
    assert "runs-qa" in names
    assert "instrumentation" in names


def test_read_skill_returns_frontmatter():
    body = skills.read_skill("runs-qa")
    assert body.startswith("---")
    assert "name: nebo-runs-qa" in body


def test_read_skill_unknown_raises():
    with pytest.raises(FileNotFoundError):
        skills.read_skill("not-a-real-skill")


class TestClaudeCodeInstall:
    def test_user_level_install_writes_prefixed_skill_dir(self, tmp_path, monkeypatch):
        # Each skill lands under `skills/nebo-<name>/SKILL.md` so it's
        # visually distinguishable from other Claude Code skills.
        monkeypatch.setenv("NEBO_CLAUDE_HOME", str(tmp_path))
        written = skill_install.install_claude_code(skill="runs-qa")
        assert len(written) == 1
        target = tmp_path / "skills" / "nebo-runs-qa" / "SKILL.md"
        assert target.exists()
        assert written[0] == target
        assert "nebo-runs-qa" in target.read_text(encoding="utf-8")
        # And NOT at the un-prefixed location.
        assert not (tmp_path / "skills" / "runs-qa").exists()

    def test_install_all_uses_nebo_prefix_on_every_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEBO_CLAUDE_HOME", str(tmp_path))
        written = skill_install.install_claude_code(skill="all")
        names = {p.parent.name for p in written}
        expected = {f"nebo-{s}" for s in skills.available_skills()}
        assert names == expected

    def test_unknown_skill_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEBO_CLAUDE_HOME", str(tmp_path))
        with pytest.raises(ValueError):
            skill_install.install_claude_code(skill="bogus")

    def test_project_level_install_uses_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # NEBO_CLAUDE_HOME should be ignored when --project is set.
        monkeypatch.setenv("NEBO_CLAUDE_HOME", str(tmp_path / "elsewhere"))
        written = skill_install.install_claude_code(skill="runs-qa", project=True)
        target = tmp_path / ".claude" / "skills" / "nebo-runs-qa" / "SKILL.md"
        assert target.exists()
        assert written[0] == target
        # Elsewhere dir should NOT have been created.
        assert not (tmp_path / "elsewhere" / "skills" / "nebo-runs-qa").exists()


class TestAgentsMdInstall:
    def test_writes_new_agents_md(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEBO_AGENTS_MD_DIR", str(tmp_path))
        path = skill_install.install_agents_md(skill="runs-qa")
        assert path == tmp_path / "AGENTS.md"
        body = path.read_text(encoding="utf-8")
        assert "<!-- nebo-skill:runs-qa start -->" in body
        assert "<!-- nebo-skill:runs-qa end -->" in body
        assert "nebo-runs-qa" in body

    def test_reinstall_is_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEBO_AGENTS_MD_DIR", str(tmp_path))
        skill_install.install_agents_md(skill="runs-qa")
        first = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
        skill_install.install_agents_md(skill="runs-qa")
        second = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
        assert first == second

    def test_install_all_adds_two_sections(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEBO_AGENTS_MD_DIR", str(tmp_path))
        skill_install.install_agents_md(skill="all")
        body = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
        assert "<!-- nebo-skill:runs-qa start -->" in body
        assert "<!-- nebo-skill:instrumentation start -->" in body

    def test_preserves_unrelated_content(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEBO_AGENTS_MD_DIR", str(tmp_path))
        existing = "# Project notes\n\nSome existing content.\n"
        (tmp_path / "AGENTS.md").write_text(existing, encoding="utf-8")
        skill_install.install_agents_md(skill="runs-qa")
        body = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
        assert "# Project notes" in body
        assert "Some existing content." in body
        assert "<!-- nebo-skill:runs-qa start -->" in body

    def test_replacing_section_keeps_others(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEBO_AGENTS_MD_DIR", str(tmp_path))
        # Seed with both, then re-install just one — the other survives.
        skill_install.install_agents_md(skill="all")
        skill_install.install_agents_md(skill="runs-qa")
        body = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
        assert "<!-- nebo-skill:runs-qa start -->" in body
        assert "<!-- nebo-skill:instrumentation start -->" in body


class TestCodexInstall:
    """Codex CLI adopted the SKILL.md standard: skills are discovered in
    any directory under ~/.codex/skills (or .codex/skills per-project)."""

    def test_user_level_install_writes_prefixed_skill_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEBO_CODEX_HOME", str(tmp_path))
        written = skill_install.install_codex(skill="runs-qa")
        target = tmp_path / "skills" / "nebo-runs-qa" / "SKILL.md"
        assert written == [target]
        assert target.read_text().startswith("---")

    def test_default_installs_every_skill(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEBO_CODEX_HOME", str(tmp_path))
        written = skill_install.install_codex()
        assert {p.parent.name for p in written} == {
            "nebo-runs-qa", "nebo-instrumentation",
        }

    def test_project_level_install_uses_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        skill_install.install_codex(skill="runs-qa", project=True)
        assert (tmp_path / ".codex" / "skills" / "nebo-runs-qa" / "SKILL.md").exists()

    def test_platform_registered(self):
        assert "codex" in skill_install.PLATFORMS


class TestInstallDispatcher:
    def test_install_all_platforms(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEBO_CLAUDE_HOME", str(tmp_path / "claude"))
        monkeypatch.setenv("NEBO_CODEX_HOME", str(tmp_path / "codex"))
        monkeypatch.setenv("NEBO_AGENTS_MD_DIR", str(tmp_path / "project"))
        os.makedirs(tmp_path / "project", exist_ok=True)
        results = skill_install.install(
            platforms=list(skill_install.PLATFORMS),
            skill="runs-qa",
        )
        assert set(results) == set(skill_install.PLATFORMS)
        cc_path = tmp_path / "claude" / "skills" / "nebo-runs-qa" / "SKILL.md"
        assert cc_path.exists()
        codex_path = tmp_path / "codex" / "skills" / "nebo-runs-qa" / "SKILL.md"
        assert codex_path.exists()
        agents_path = tmp_path / "project" / "AGENTS.md"
        assert agents_path.exists()

    def test_unknown_platform_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEBO_CLAUDE_HOME", str(tmp_path))
        with pytest.raises(ValueError):
            skill_install.install(platforms=["sublime"], skill="runs-qa")


def test_runs_qa_skill_uses_cli_primarily():
    body = skills.read_skill("runs-qa")
    # Primary commands must be CLI.
    assert "nebo runs list" in body
    assert "nebo metrics get" in body
    assert "nebo runs wait" in body
    # MCP tools should appear only in the appendix.
    appendix_start = body.find("Optional: MCP")
    assert appendix_start != -1, "skill is missing the 'Optional: MCP' appendix"
    primary = body[:appendix_start]
    # No MCP tool names in the primary playbook.
    assert "nebo_get_metrics" not in primary
    assert "nebo_log_metric" not in primary
    assert "nebo_wait_for_alert" not in primary
