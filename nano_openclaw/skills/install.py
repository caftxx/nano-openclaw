"""Skill dependency installation helpers.

Python dependencies are installed into OpenClaw-managed virtualenvs instead of
the interpreter's global site-packages.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nano_openclaw.skills.loader import load_skill_entries
from nano_openclaw.skills.types import SkillEntry, SkillInstallSpec


@dataclass
class SkillPythonEnv:
    venv_dir: Path
    python_executable: Path
    bin_dir: Path
    env: dict[str, str]


@dataclass
class SkillInstallResult:
    ok: bool
    message: str
    stdout: str = ""
    stderr: str = ""
    code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "code": self.code,
        }


_SAFE_SKILL_SEGMENT_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SAFE_UV_PACKAGE_RE = re.compile(
    r"^[a-z0-9][a-z0-9._-]*(\[[a-z0-9,._-]+\])?(([><=!~]=?|===?)[a-z0-9.*_-]+)?$",
    re.IGNORECASE,
)


def _safe_skill_segment(skill_name: str) -> str:
    segment = _SAFE_SKILL_SEGMENT_RE.sub("-", skill_name.strip()).strip(".-")
    return segment or "skill"


def resolve_skill_python_env(
    state_dir: str | Path,
    skill_name: str,
    *,
    base_env: dict[str, str] | None = None,
    platform: str | None = None,
) -> SkillPythonEnv:
    """Return the isolated Python environment path for a skill."""
    platform_name = platform or sys.platform
    venv_dir = Path(state_dir) / "tools" / "python" / "skills" / _safe_skill_segment(skill_name) / "venv"
    if platform_name.startswith("win"):
        bin_dir = venv_dir / "Scripts"
        python_executable = bin_dir / "python.exe"
    else:
        bin_dir = venv_dir / "bin"
        python_executable = bin_dir / "python"

    env = dict(base_env if base_env is not None else os.environ)
    old_path = env.get("PATH", "")
    env["VIRTUAL_ENV"] = str(venv_dir)
    env["PATH"] = str(bin_dir) + (os.pathsep + old_path if old_path else "")
    return SkillPythonEnv(
        venv_dir=venv_dir,
        python_executable=python_executable,
        bin_dir=bin_dir,
        env=env,
    )


def _resolve_install_id(spec: SkillInstallSpec, index: int) -> str:
    return (spec.id or f"{spec.kind}-{index}").strip()


def _find_entry(entries: list[SkillEntry], skill_name: str) -> SkillEntry | None:
    for entry in entries:
        if entry.skill.name == skill_name:
            return entry
    return None


def _find_install_spec(entry: SkillEntry, install_id: str) -> SkillInstallSpec | None:
    for index, spec in enumerate(entry.metadata.install if entry.metadata and entry.metadata.install else []):
        if _resolve_install_id(spec, index) == install_id:
            return spec
    return None


def _validate_uv_package(package: str | None) -> str | None:
    value = (package or "").strip()
    if not value:
        return None
    if value.startswith("-"):
        return None
    if not _SAFE_UV_PACKAGE_RE.fullmatch(value):
        return None
    return value


async def _run_command(argv: list[str], *, timeout: int, env: dict[str, str] | None = None) -> SkillInstallResult:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return SkillInstallResult(
            ok=False,
            message=f"Command timed out after {timeout}s",
            code=1,
        )
    stdout = stdout_b.decode(errors="replace")
    stderr = stderr_b.decode(errors="replace")
    return SkillInstallResult(
        ok=proc.returncode == 0,
        message="Installed" if proc.returncode == 0 else "Install command failed",
        stdout=stdout.strip(),
        stderr=stderr.strip(),
        code=proc.returncode,
    )


async def install_skill(
    *,
    workspace_dir: str | Path,
    state_dir: str | Path,
    skill_name: str,
    install_id: str,
    timeout: int = 300,
) -> SkillInstallResult:
    """Install a skill dependency into an isolated OpenClaw-managed location."""
    entries = load_skill_entries(Path(workspace_dir))
    entry = _find_entry(entries, skill_name)
    if entry is None:
        return SkillInstallResult(ok=False, message=f"Skill not found: {skill_name}", code=None)

    spec = _find_install_spec(entry, install_id)
    if spec is None:
        return SkillInstallResult(ok=False, message=f"Installer not found: {install_id}", code=None)

    if spec.kind != "uv":
        return SkillInstallResult(
            ok=False,
            message=f"Installer kind not supported in nano-openclaw yet: {spec.kind}",
            code=None,
        )

    package = _validate_uv_package(spec.package)
    if package is None:
        return SkillInstallResult(ok=False, message="missing or unsafe uv package", code=None)

    env_info = resolve_skill_python_env(state_dir, skill_name)
    env_info.venv_dir.parent.mkdir(parents=True, exist_ok=True)

    uv_exe = shutil.which("uv")
    if uv_exe:
        if not env_info.python_executable.exists():
            create = await _run_command(
                [sys.executable, "-m", "venv", str(env_info.venv_dir)],
                timeout=timeout,
            )
            if not create.ok:
                create.message = f"Failed to create skill virtualenv: {create.message}"
                return create
        return await _run_command(
            [uv_exe, "pip", "install", "--python", str(env_info.python_executable), package],
            timeout=timeout,
            env=env_info.env,
        )

    create = await _run_command(
        [sys.executable, "-m", "venv", str(env_info.venv_dir)],
        timeout=timeout,
    )
    if not create.ok:
        create.message = f"Failed to create skill virtualenv: {create.message}"
        return create

    return await _run_command(
        [str(env_info.python_executable), "-m", "pip", "install", package],
        timeout=timeout,
        env=env_info.env,
    )

