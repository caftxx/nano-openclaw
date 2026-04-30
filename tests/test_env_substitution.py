"""Tests for config/env_substitution.py.

Mirrors openclaw/src/config/env-substitution.test.ts behavior.
"""

import pytest
from nano_openclaw.config.env_substitution import (
    MissingEnvVarError,
    contains_env_var_reference,
    resolve_config_env_vars,
)


class TestEnvSubstitution:
    def test_simple_substitution(self):
        env = {"API_KEY": "secret123"}
        result = resolve_config_env_vars({"apiKey": "${API_KEY}"}, env)
        assert result["apiKey"] == "secret123"
    
    def test_missing_env_var_throws(self):
        with pytest.raises(MissingEnvVarError) as exc_info:
            resolve_config_env_vars({"key": "${MISSING_VAR}"}, {})
        assert exc_info.value.var_name == "MISSING_VAR"
    
    def test_missing_env_var_with_callback(self):
        warnings = []
        result = resolve_config_env_vars(
            {"key": "${MISSING_VAR}"},
            {},
            on_missing=lambda v, p: warnings.append((v, p))
        )
        assert len(warnings) == 1
        assert warnings[0][0] == "MISSING_VAR"
        assert result["key"] == "${MISSING_VAR}"
    
    def test_escaped_dollar(self):
        env = {"VAR": "value"}
        result = resolve_config_env_vars({"key": "$${VAR}"}, env)
        assert result["key"] == "${VAR}"
    
    def test_nested_object(self):
        env = {"API_KEY": "secret"}
        result = resolve_config_env_vars(
            {"models": {"providers": {"custom": {"apiKey": "${API_KEY}"}}}},
            env
        )
        assert result["models"]["providers"]["custom"]["apiKey"] == "secret"
    
    def test_array_values(self):
        env = {"KEY1": "v1", "KEY2": "v2"}
        result = resolve_config_env_vars(
            {"models": ["${KEY1}", "${KEY2}"]},
            env
        )
        assert result["models"] == ["v1", "v2"]
    
    def test_non_string_values_unchanged(self):
        env = {}
        result = resolve_config_env_vars(
            {"int": 123, "bool": True, "null": None, "float": 3.14},
            env
        )
        assert result["int"] == 123
        assert result["bool"] is True
        assert result["null"] is None
        assert result["float"] == 3.14
    
    def test_only_uppercase_vars_matched(self):
        env = {"lowercase": "value", "UPPERCASE": "upper_value"}
        result = resolve_config_env_vars(
            {"key1": "${lowercase}", "key2": "${UPPERCASE}"},
            env
        )
        assert result["key1"] == "${lowercase}"
        assert result["key2"] == "upper_value"
    
    def test_contains_env_var_reference(self):
        assert contains_env_var_reference("${VAR}") is True
        assert contains_env_var_reference("$${VAR}") is False
        assert contains_env_var_reference("no dollar") is False
        assert contains_env_var_reference("prefix ${VAR} suffix") is True
    
    def test_empty_env_var(self):
        env = {"EMPTY_VAR": ""}
        with pytest.raises(MissingEnvVarError):
            resolve_config_env_vars({"key": "${EMPTY_VAR}"}, env)
    
    def test_multiple_vars_in_one_string(self):
        env = {"A": "1", "B": "2"}
        result = resolve_config_env_vars({"key": "${A}-${B}"}, env)
        assert result["key"] == "1-2"
    
    def test_underscore_and_digits_in_var_name(self):
        env = {"MY_API_KEY_123": "value"}
        result = resolve_config_env_vars({"key": "${MY_API_KEY_123}"}, env)
        assert result["key"] == "value"
    
    def test_config_path_in_warning(self):
        warnings = []
        resolve_config_env_vars(
            {"nested": {"deep": {"key": "${MISSING}"}}},
            {},
            on_missing=lambda v, p: warnings.append((v, p))
        )
        assert warnings[0][1] == "nested.deep.key"