from __future__ import annotations

import asyncio
from pathlib import Path

from nano_openclaw.skills.install import (
    SkillInstallResult,
    install_skill,
    resolve_skill_python_env,
)


def _write_skill(workspace: Path, name: str, install: str) -> None:
    skill_dir = workspace / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"""---
name: {name}
description: Test skill
metadata: {{"openclaw":{{"install":[{install}]}}}}
---

# {name}
""",
        encoding="utf-8",
    )


def test_resolve_skill_python_env_windows_paths(tmp_path: Path) -> None:
    env = resolve_skill_python_env(tmp_path, "pdf tool", base_env={"PATH": "C:\\bin"}, platform="win32")

    assert env.venv_dir == tmp_path / "tools" / "python" / "skills" / "pdf-tool" / "venv"
    assert env.python_executable == env.venv_dir / "Scripts" / "python.exe"
    assert env.bin_dir == env.venv_dir / "Scripts"
    assert env.env["VIRTUAL_ENV"] == str(env.venv_dir)
    assert env.env["PATH"].startswith(str(env.bin_dir))


def test_uv_install_uses_isolated_venv_python_without_global_pip(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    _write_skill(
        workspace,
        "pdf-tool",
        '{"id":"deps","kind":"uv","package":"pypdf==4.0.0"}',
    )

    calls: list[list[str]] = []

    monkeypatch.setattr("nano_openclaw.skills.install.shutil.which", lambda _name: None)

    async def fake_run(argv: list[str], *, timeout: int, env=None) -> SkillInstallResult:
        calls.append(argv)
        if argv[:3] == [__import__("sys").executable, "-m", "venv"]:
            Path(argv[3]).mkdir(parents=True, exist_ok=True)
        return SkillInstallResult(ok=True, message="Installed", code=0)

    monkeypatch.setattr("nano_openclaw.skills.install._run_command", fake_run)

    result = asyncio.run(
        install_skill(
            workspace_dir=workspace,
            state_dir=state,
            skill_name="pdf-tool",
            install_id="deps",
        )
    )

    venv = state / "tools" / "python" / "skills" / "pdf-tool" / "venv"
    venv_python = resolve_skill_python_env(state, "pdf-tool").python_executable
    assert result.ok is True
    assert venv.exists()
    assert calls[0] == [__import__("sys").executable, "-m", "venv", str(venv)]
    assert calls[1][0] == str(venv_python)
    assert calls[1][1:] == ["-m", "pip", "install", "pypdf==4.0.0"]
    assert calls[1][0] != "pip"


def test_uv_binary_install_targets_venv_python(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    _write_skill(workspace, "http", '{"id":"deps","kind":"uv","package":"httpx"}')
    calls: list[list[str]] = []

    monkeypatch.setattr("nano_openclaw.skills.install.shutil.which", lambda _name: "uv")

    async def fake_run(argv: list[str], *, timeout: int, env=None) -> SkillInstallResult:
        calls.append(argv)
        return SkillInstallResult(ok=True, message="Installed", code=0)

    monkeypatch.setattr("nano_openclaw.skills.install._run_command", fake_run)

    result = asyncio.run(
        install_skill(workspace_dir=workspace, state_dir=state, skill_name="http", install_id="deps")
    )

    venv_python = resolve_skill_python_env(state, "http").python_executable
    assert result.ok is True
    assert calls[-1] == ["uv", "pip", "install", "--python", str(venv_python), "httpx"]


def test_install_returns_failures_for_missing_targets(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    _write_skill(workspace, "demo", '{"id":"deps","kind":"uv","package":"httpx"}')

    missing_skill = asyncio.run(
        install_skill(workspace_dir=workspace, state_dir=state, skill_name="nope", install_id="deps")
    )
    missing_installer = asyncio.run(
        install_skill(workspace_dir=workspace, state_dir=state, skill_name="demo", install_id="other")
    )

    assert missing_skill.ok is False
    assert "Skill not found" in missing_skill.message
    assert missing_installer.ok is False
    assert "Installer not found" in missing_installer.message


def test_install_rejects_missing_or_unsafe_package(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    _write_skill(workspace, "bad1", '{"id":"deps","kind":"uv"}')
    _write_skill(workspace, "bad2", '{"id":"deps","kind":"uv","package":"-r requirements.txt"}')
    _write_skill(workspace, "bad3", '{"id":"deps","kind":"uv","package":"httpx; rm -rf /"}')

    for skill in ["bad1", "bad2", "bad3"]:
        result = asyncio.run(
            install_skill(workspace_dir=workspace, state_dir=state, skill_name=skill, install_id="deps")
        )
        assert result.ok is False
        assert "unsafe uv package" in result.message
