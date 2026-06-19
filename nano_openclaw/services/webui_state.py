"""WebUI-facing runtime state projection.

This module keeps browser payload shaping in the service layer so the WebUI
adapter does not need to reach into ``AgentRuntime`` internals.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nano_openclaw.services.event_payload import jsonable


def state_payload(runtime: Any) -> dict[str, Any]:
    hook_registry = runtime.registry.hook_registry()
    return {
        "agent_id": runtime.agent_id,
        "agent_options": agent_options(runtime.config),
        "model": runtime.model_id,
        "model_ref": runtime.model_ref,
        "model_options": model_options(runtime.config),
        "image_model": runtime.cfg.image_model,
        "image_model_ref": runtime.image_model_ref or "",
        "image_model_options": image_model_options(runtime.config),
        "thinking_level": runtime.cfg.thinking_level,
        "thinking_options": list(thinking_levels()),
        "assistant_name": read_assistant_name(runtime.workspace_dir),
        "user_name": read_user_name(runtime.workspace_dir),
        "workspace_dir": str(runtime.workspace_dir),
        "tools": runtime.registry.names(),
        "plugins": [jsonable(plugin) for plugin in getattr(hook_registry, "plugins", lambda: [])()],
        "hooks": getattr(hook_registry, "handler_counts", lambda: {})(),
        "skills": {
            "filter": runtime.cfg.skill_filter,
            "extra_dirs": runtime.cfg.extra_skill_dirs,
        },
        "warnings": runtime.warnings,
    }


def thinking_levels() -> tuple[str, ...]:
    return ("off", "minimal", "low", "medium", "high", "xhigh", "adaptive", "max")


def agent_options(config: Any) -> list[dict[str, Any]]:
    agents = list(config.agents.list or [])
    default_id = None
    for agent in agents:
        if agent.default:
            default_id = agent.id
            break
    if default_id is None:
        default_id = agents[0].id if agents else "default"

    if not agents:
        return [{"id": "default", "name": "Default Agent", "default": True}]

    return [
        {
            "id": agent.id,
            "name": agent.name or agent.id,
            "default": agent.id == default_id,
        }
        for agent in agents
    ]


def model_options(config: Any) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []

    def add(ref: str | None, name: str | None = None, input: list[str] | None = None) -> None:
        if not ref or "/" not in ref:
            return
        if ref in seen:
            if name or input:
                for item in result:
                    if item["ref"] != ref:
                        continue
                    if name and item["name"] == ref:
                        item["name"] = name
                    if input and not item.get("input"):
                        item["input"] = input
                        break
            return
        seen.add(ref)
        result.append({"ref": ref, "name": name or ref, "input": input or []})

    add(config.resolve_primary_model())
    for agent in config.agents.list:
        add(config.resolve_primary_model(agent.id))
    for provider_id, provider in config.models.providers.items():
        for model in provider.models:
            add(f"{provider_id}/{model.id}", model.name or model.id, list(model.input or []))

    return result


def image_model_options(config: Any) -> list[dict[str, Any]]:
    seen: set[str] = {""}
    result: list[dict[str, Any]] = [{"ref": "", "name": "Native Vision", "input": ["image"]}]

    def add(ref: str | None, name: str | None = None, input: list[str] | None = None) -> None:
        if not ref or "/" not in ref:
            return
        if "image" not in (input or []):
            return
        if ref in seen:
            if name or input:
                for item in result:
                    if item["ref"] != ref:
                        continue
                    if name and item["name"] == ref:
                        item["name"] = name
                    if input and not item.get("input"):
                        item["input"] = input
                        break
            return
        seen.add(ref)
        result.append({"ref": ref, "name": name or ref, "input": input or []})

    for provider_id, provider in config.models.providers.items():
        for model in provider.models:
            add(f"{provider_id}/{model.id}", model.name or model.id, list(model.input or []))

    return result


def has_active_turn(manager: Any) -> bool:
    return any(session.active_turn_id for session in manager._loaded.values())


def read_assistant_name(workspace_dir: Path) -> str:
    return read_profile_field(workspace_dir / "IDENTITY.md", "Name", "Assistant")


def read_user_name(workspace_dir: Path) -> str:
    return read_profile_field(workspace_dir / "USER.md", "What to call them", "User")


def read_profile_field(path: Path, field_name: str, fallback: str) -> str:
    if not path.exists() or not path.is_file():
        return fallback
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return fallback

    for index, line in enumerate(lines):
        parsed = parse_profile_field_line(line, field_name)
        if parsed:
            return parsed
        if profile_line_label(line).lower() == field_name.lower():
            for follow in lines[index + 1:index + 4]:
                candidate = clean_profile_value(follow)
                if candidate:
                    return candidate
    return fallback


def parse_profile_field_line(line: str, field_name: str) -> str:
    normalized = line.strip().lstrip("-").strip()
    normalized = normalized.replace("**", "")
    if not normalized.lower().startswith(field_name.lower()):
        return ""
    label, sep, value = normalized.partition(":")
    if not sep:
        return ""
    if label.strip().lower() != field_name.lower():
        return ""
    return clean_profile_value(value)


def profile_line_label(line: str) -> str:
    normalized = line.strip().lstrip("-").strip().replace("**", "")
    label, sep, _value = normalized.partition(":")
    return label.strip() if sep else ""


def clean_profile_value(value: str) -> str:
    cleaned = value.strip().lstrip("-").strip()
    cleaned = cleaned.replace("**", "").strip()
    if not cleaned or cleaned.startswith("_(") or cleaned.startswith("("):
        return ""
    return cleaned[:80]
