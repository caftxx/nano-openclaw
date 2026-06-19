"""Skills runtime port used by the core loop."""

from __future__ import annotations

from typing import Any

from nano_openclaw.features.skills import (
    build_skill_registry_from_entries,
    build_slash_command_context,
    filter_eligible_skills,
    filter_visible_skills,
    get_or_load_skills,
    parse_slash_command,
)
from nano_openclaw.features.skills.usage import record_event


class SkillRuntime:
    def load_turn_skills(self, cfg: Any) -> tuple[list[Any], list[Any] | None]:
        if not cfg.workspace_dir:
            return [], None

        skill_entries = get_or_load_skills(
            cfg.workspace_dir,
            cfg.session_key,
            extra_dirs=cfg.extra_skill_dirs,
            max_bytes=cfg.max_skill_file_bytes,
        )
        if not skill_entries:
            return [], None
        eligible_entries = filter_eligible_skills(
            skill_entries,
            skill_filter=cfg.skill_filter,
        )
        visible_skills = filter_visible_skills(eligible_entries)
        return eligible_entries, visible_skills

    def prepare_skill_command(
        self,
        user_input: str,
        eligible_entries: list[Any],
        cfg: Any,
        registry: Any,
    ) -> tuple[Any | None, str, dict[str, Any]]:
        if not eligible_entries:
            return None, user_input, {}

        runtime_registry = build_skill_registry_from_entries(eligible_entries)
        skill_registry = runtime_registry if runtime_registry else {}
        command, remaining_text = parse_slash_command(user_input, skill_registry)

        model_registry = build_skill_registry_from_entries(
            eligible_entries,
            user_invocable_only=False,
        )
        try:
            for skill_name, skill in model_registry.items():
                record_event(
                    cfg.state_dir,
                    skill_name,
                    "load",
                    source=skill.source,
                    path=skill.filePath,
                )
        except Exception:
            pass
        registry.set_eligible_skills(model_registry)
        return command, remaining_text, skill_registry

    def build_slash_command_context(self, command: Any) -> str:
        return build_slash_command_context(command)

    def record_command_use(self, command: Any, state_dir: Any) -> None:
        record_event(
            state_dir,
            command.name,
            "use",
            source=command.skill.source,
            path=command.skill.filePath,
        )
