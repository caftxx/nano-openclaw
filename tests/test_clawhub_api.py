"""Tests for the bundled ClawHub CLI."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import pytest

from nano_openclaw.bundled_skills.clawhub.scripts import clawhub_api


def _make_workspace(name: str) -> Path:
    root = Path(__file__).resolve().parent / ".tmp_clawhub_api" / name
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    return root


def test_cmd_install_requires_overwrite_flag_for_existing_skill(capsys: pytest.CaptureFixture[str]) -> None:
    """Existing installs should fail fast with a rerun hint instead of prompting."""
    workspace = _make_workspace("install_existing")
    try:
        skill_dir = workspace / "skills" / "pdf-tool"
        skill_dir.mkdir(parents=True)

        args = argparse.Namespace(slug="pdf-tool", workspace=str(workspace), overwrite=False)

        with pytest.raises(SystemExit) as exc:
            clawhub_api.cmd_install(args)

        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "already installed" in captured.err
        assert "User confirmation required" in captured.err
        assert "--overwrite" in captured.err
        assert "If the user confirms" in captured.err
        assert "install pdf-tool --workspace" in captured.err
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_cmd_uninstall_requires_yes_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """Uninstall should fail fast with a rerun hint instead of prompting."""
    workspace = _make_workspace("uninstall_existing")
    try:
        skill_dir = workspace / "skills" / "pdf-tool"
        skill_dir.mkdir(parents=True)

        args = argparse.Namespace(slug="pdf-tool", workspace=str(workspace), yes=False)

        with pytest.raises(SystemExit) as exc:
            clawhub_api.cmd_uninstall(args)

        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert "is installed at" in captured.err
        assert "User confirmation required" in captured.err
        assert "--yes" in captured.err
        assert "If the user confirms" in captured.err
        assert "uninstall pdf-tool --workspace" in captured.err
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_cmd_info_prints_skill_detail(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Info should show ClawHub detail fields and the public detail URL."""
    detail = clawhub_api.ClawHubSkillDetail(
        slug="memory",
        displayName="Memory",
        summary="Organized memory",
        latestVersion="1.0.2",
        changelog="Updated docs",
        ownerHandle="ivangdavila",
        ownerName="Ivan",
        downloads=1234,
        installsCurrent=12,
        installsAllTime=34,
        stars=5,
        versions=3,
        updatedAt=1777866263220,
        license="MIT",
        os=["linux", "darwin", "win32"],
    )
    monkeypatch.setattr(clawhub_api, "get_skill_detail", lambda slug: detail)

    args = argparse.Namespace(slug="memory")

    clawhub_api.cmd_info(args)

    captured = capsys.readouterr()
    assert "Memory (memory)" in captured.out
    assert "https://clawhub.ai/skill/memory" in captured.out
    assert "Version: 1.0.2" in captured.out
    assert "Owner: Ivan (@ivangdavila)" in captured.out
    assert "1,234 downloads" in captured.out


def test_cmd_update_skips_when_versions_match(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Update should compare local and remote versions before downloading."""
    workspace = _make_workspace("update_current")
    try:
        skill_dir = workspace / "skills" / "memory"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: Memory\nversion: 1.0.2\n---\n# Memory\n")
        detail = clawhub_api.ClawHubSkillDetail(
            slug="memory",
            displayName="Memory",
            summary="",
            latestVersion="1.0.2",
            changelog=None,
            ownerHandle=None,
            ownerName=None,
        )
        monkeypatch.setattr(clawhub_api, "get_skill_detail", lambda slug: detail)

        def fail_install(*_args: object, **_kwargs: object) -> tuple[bool, str]:
            raise AssertionError("install_skill should not be called")

        monkeypatch.setattr(clawhub_api, "install_skill", fail_install)

        args = argparse.Namespace(slug="memory", workspace=str(workspace), force=False)

        clawhub_api.cmd_update(args)

        captured = capsys.readouterr()
        assert "Local version: 1.0.2" in captured.out
        assert "ClawHub version: 1.0.2" in captured.out
        assert "already up to date" in captured.out
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_cmd_update_installs_when_remote_version_differs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Update should replace the installed skill when ClawHub has a different version."""
    workspace = _make_workspace("update_newer")
    try:
        skill_dir = workspace / "skills" / "memory"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: Memory\nversion: 1.0.1\n---\n# Memory\n")
        detail = clawhub_api.ClawHubSkillDetail(
            slug="memory",
            displayName="Memory",
            summary="",
            latestVersion="1.0.2",
            changelog=None,
            ownerHandle=None,
            ownerName=None,
        )
        install_calls: list[tuple[str, Path, bool]] = []
        monkeypatch.setattr(clawhub_api, "get_skill_detail", lambda slug: detail)

        def fake_install(slug: str, workspace_dir: Path, overwrite: bool = False) -> tuple[bool, str]:
            install_calls.append((slug, workspace_dir, overwrite))
            return True, f"Skill '{slug}' installed"

        monkeypatch.setattr(clawhub_api, "install_skill", fake_install)

        args = argparse.Namespace(slug="memory", workspace=str(workspace), force=False)

        clawhub_api.cmd_update(args)

        captured = capsys.readouterr()
        assert "Local version: 1.0.1" in captured.out
        assert "ClawHub version: 1.0.2" in captured.out
        assert "Updating 'memory'" in captured.out
        assert install_calls == [("memory", workspace, True)]
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
