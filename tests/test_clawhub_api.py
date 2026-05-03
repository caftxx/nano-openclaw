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
