"""Environment variable substitution for config values.

Mirrors openclaw/src/config/env-substitution.ts:
- Supports ${VAR_NAME} syntax in string values
- Only uppercase env vars are matched: [A-Z_][A-Z0-9_]*
- Escape with $${VAR} to output literal ${VAR}
- Missing env vars can be collected as warnings instead of throwing

Example:
    {
        apiKey: "${ANTHROPIC_API_KEY}",
        baseUrl: "$${KEEP_THIS_LITERAL}",
    }
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable, Optional

ENV_VAR_NAME_PATTERN = re.compile(r'^[A-Z_][A-Z0-9_]*$')


class MissingEnvVarError(Exception):
    """Missing environment variable error."""
    def __init__(self, var_name: str, config_path: str):
        super().__init__(f"Missing env var \"{var_name}\" referenced at config path: {config_path}")
        self.var_name = var_name
        self.config_path = config_path


EnvSubstitutionWarning = tuple[str, str]


def _parse_env_token_at(value: str, index: int) -> Optional[dict]:
    if value[index] != "$":
        return None
    
    next_char = value[index + 1] if index + 1 < len(value) else None
    after_next = value[index + 2] if index + 2 < len(value) else None
    
    if next_char == "$" and after_next == "{":
        start = index + 3
        end = value.find("}", start)
        if end != -1:
            name = value[start:end]
            if ENV_VAR_NAME_PATTERN.match(name):
                return {"kind": "escaped", "name": name, "end": end}
    
    if next_char == "{":
        start = index + 2
        end = value.find("}", start)
        if end != -1:
            name = value[start:end]
            if ENV_VAR_NAME_PATTERN.match(name):
                return {"kind": "substitution", "name": name, "end": end}
    
    return None


def _substitute_string(
    value: str,
    env: dict[str, str],
    config_path: str,
    on_missing: Optional[Callable[[str, str], None]] = None,
) -> str:
    if "$" not in value:
        return value
    
    chunks: list[str] = []
    
    i = 0
    while i < len(value):
        char = value[i]
        if char != "$":
            chunks.append(char)
            i += 1
            continue
        
        token = _parse_env_token_at(value, i)
        if token is None:
            chunks.append(char)
            i += 1
            continue
        
        if token["kind"] == "escaped":
            chunks.append("${" + token["name"] + "}")
            i = token["end"] + 1
            continue
        
        if token["kind"] == "substitution":
            env_value = env.get(token["name"])
            if env_value is None or env_value == "":
                if on_missing:
                    on_missing(token["name"], config_path)
                    chunks.append("${" + token["name"] + "}")
                    i = token["end"] + 1
                    continue
                raise MissingEnvVarError(token["name"], config_path)
            chunks.append(env_value)
            i = token["end"] + 1
            continue
    
    return "".join(chunks)


def contains_env_var_reference(value: str) -> bool:
    if "$" not in value:
        return False
    
    i = 0
    while i < len(value):
        char = value[i]
        if char != "$":
            i += 1
            continue
        
        token = _parse_env_token_at(value, i)
        if token is None:
            i += 1
            continue
        
        if token["kind"] == "escaped":
            i = token["end"] + 1
            continue
        
        if token["kind"] == "substitution":
            return True
    
    return False


def _is_plain_object(value: Any) -> bool:
    return isinstance(value, dict) and not isinstance(value, (str, list, tuple, set))


def _substitute_any(
    value: Any,
    env: dict[str, str],
    path: str,
    on_missing: Optional[Callable[[str, str], None]] = None,
) -> Any:
    if isinstance(value, str):
        return _substitute_string(value, env, path, on_missing)
    
    if isinstance(value, list):
        return [_substitute_any(item, env, f"{path}[{idx}]", on_missing) for idx, item in enumerate(value)]
    
    if _is_plain_object(value):
        result: dict[str, Any] = {}
        for key, val in value.items():
            child_path = f"{path}.{key}" if path else key
            result[key] = _substitute_any(val, env, child_path, on_missing)
        return result
    
    return value


def resolve_config_env_vars(
    obj: Any,
    env: Optional[dict[str, str]] = None,
    on_missing: Optional[Callable[[str, str], None]] = None,
) -> Any:
    """
    Resolves ${VAR_NAME} environment variable references in config values.
    
    Args:
        obj: The parsed config object (after JSON5 parse)
        env: Environment variables to use for substitution
        on_missing: Callback to collect warnings instead of throwing
    
    Returns:
        The config object with env vars substituted
    
    Raises:
        MissingEnvVarError: If a referenced env var is not set (unless on_missing is set)
    """
    if env is None:
        env = dict(os.environ)
    
    return _substitute_any(obj, env, "", on_missing)