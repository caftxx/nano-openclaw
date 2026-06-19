"""Runtime integration helpers for the skills feature."""

from __future__ import annotations

from typing import Any

from nano_openclaw.core.tools import ToolExecutionContext
from nano_openclaw.features.skills import install as skill_install
from nano_openclaw.features.skills.usage import record_event


def record_skill_usage(skill_name: str, skill: Any, context: ToolExecutionContext) -> None:
    record_event(
        context.state_dir,
        skill_name,
        "use",
        source=getattr(skill, "source", "unknown"),
        path=getattr(skill, "filePath", ""),
    )


async def run_skill_install(
    *,
    workspace_dir: str,
    state_dir: str,
    skill_name: str,
    install_id: str,
    timeout: int,
) -> str:
    result = await skill_install.install_skill(
        workspace_dir=workspace_dir,
        state_dir=state_dir,
        skill_name=skill_name,
        install_id=install_id,
        timeout=timeout,
    )
    parts = [f"ok={str(result.ok).lower()}", f"message={result.message}"]
    if result.ok:
        env_info = skill_install.resolve_skill_python_env(state_dir, skill_name)
        parts.append(f"python={env_info.python_executable}")
        parts.append(f"venv={env_info.venv_dir}")
    if result.code is not None:
        parts.append(f"code={result.code}")
    if result.stdout:
        parts.append(f"--- stdout ---\n{result.stdout}")
    if result.stderr:
        parts.append(f"--- stderr ---\n{result.stderr}")
    return "\n".join(parts)


def bind_skill_runtime(registry: Any) -> None:
    registry.set_skill_usage_recorder(record_skill_usage)
    registry.set_skill_installer(run_skill_install)
