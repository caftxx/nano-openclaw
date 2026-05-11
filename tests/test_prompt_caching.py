"""Tests for Stage 4 Anthropic prompt caching.

Covers:
  - apply_anthropic_cache_control places ≤4 markers in the right slots
  - String content is converted to a single text block with cache_control
  - The 1h TTL marker carries the ttl field; default 5m does not
  - Input list is not mutated (deep copy semantics)
  - Empty / no system / many messages edge cases
  - build_cacheable_system shape
  - PromptCachingConfig defaults + cache_ttl alias roundtrip
"""

from __future__ import annotations

from nano_openclaw.config.types import PromptCachingConfig
from nano_openclaw.prompt_caching import (
    apply_anthropic_cache_control,
    build_cacheable_system,
)


# ---------------------------------------------------------------------------
# apply_anthropic_cache_control
# ---------------------------------------------------------------------------


def _markers(messages):
    """Walk every block / message and return a list of cache_control markers
    found, paired with the message index that carries them."""
    found = []
    for i, msg in enumerate(messages):
        if "cache_control" in msg:
            found.append((i, "msg", msg["cache_control"]))
        content = msg.get("content")
        if isinstance(content, list):
            for j, block in enumerate(content):
                if isinstance(block, dict) and "cache_control" in block:
                    found.append((i, j, block["cache_control"]))
    return found


def test_system_and_3_places_4_breakpoints_max():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "u4"},  # last
    ]
    out = apply_anthropic_cache_control(msgs, cache_ttl="5m")
    found = _markers(out)
    # Exactly 4 markers: system + last 3 non-system
    assert len(found) == 4
    indices = sorted({entry[0] for entry in found})
    # System (0), and last 3 user/assistant (5, 6, 7)
    assert indices == [0, 5, 6, 7]


def test_system_and_3_with_no_system_uses_4_recent_messages():
    msgs = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
    ]
    out = apply_anthropic_cache_control(msgs, cache_ttl="5m")
    found = _markers(out)
    # 4 markers on the last 4 messages (1, 2, 3, 4)
    indices = sorted({entry[0] for entry in found})
    assert indices == [1, 2, 3, 4]


def test_string_content_is_converted_to_block_list():
    msgs = [{"role": "user", "content": "plain string"}]
    out = apply_anthropic_cache_control(msgs, cache_ttl="5m")
    content = out[0]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[0]["text"] == "plain string"
    assert content[0]["cache_control"] == {"type": "ephemeral"}


def test_list_content_marker_lands_on_last_block():
    msgs = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "first"},
            {"type": "text", "text": "last"},
        ],
    }]
    out = apply_anthropic_cache_control(msgs, cache_ttl="5m")
    content = out[0]["content"]
    assert "cache_control" not in content[0]
    assert content[1]["cache_control"] == {"type": "ephemeral"}


def test_1h_ttl_attaches_ttl_field():
    msgs = [{"role": "user", "content": "x"}]
    out = apply_anthropic_cache_control(msgs, cache_ttl="1h")
    marker = out[0]["content"][0]["cache_control"]
    assert marker["type"] == "ephemeral"
    assert marker["ttl"] == "1h"


def test_default_5m_ttl_does_not_attach_ttl_field():
    msgs = [{"role": "user", "content": "x"}]
    out = apply_anthropic_cache_control(msgs, cache_ttl="5m")
    marker = out[0]["content"][0]["cache_control"]
    assert "ttl" not in marker


def test_empty_message_list_is_passthrough():
    out = apply_anthropic_cache_control([], cache_ttl="5m")
    assert out == []


def test_does_not_mutate_input_list():
    """Deep copy semantics are critical — otherwise the cache markers leak
    into the conversation history that compaction reuses each turn."""
    original = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
    ]
    snapshot = [dict(m) for m in original]
    out = apply_anthropic_cache_control(original, cache_ttl="5m")
    # Original messages unchanged
    assert original == snapshot
    # Output is distinct object
    assert out is not original
    assert out[0] is not original[0]


def test_empty_string_content_marker_attaches_to_message():
    msgs = [{"role": "assistant", "content": ""}]
    out = apply_anthropic_cache_control(msgs, cache_ttl="5m")
    # Empty string isn't promoted to a list — marker goes on the message itself
    assert out[0].get("cache_control") == {"type": "ephemeral"}


def test_only_one_message_gets_marker_when_history_short():
    msgs = [{"role": "user", "content": "single"}]
    out = apply_anthropic_cache_control(msgs, cache_ttl="5m")
    found = _markers(out)
    assert len(found) == 1


# ---------------------------------------------------------------------------
# build_cacheable_system
# ---------------------------------------------------------------------------


def test_build_cacheable_system_5m():
    blocks = build_cacheable_system("you are helpful", cache_ttl="5m")
    assert blocks == [{
        "type": "text",
        "text": "you are helpful",
        "cache_control": {"type": "ephemeral"},
    }]


def test_build_cacheable_system_1h():
    blocks = build_cacheable_system("you are helpful", cache_ttl="1h")
    assert blocks[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


# ---------------------------------------------------------------------------
# PromptCachingConfig
# ---------------------------------------------------------------------------


def test_prompt_caching_config_defaults():
    cfg = PromptCachingConfig()
    assert cfg.enabled is True
    assert cfg.cache_ttl == "5m"


def test_prompt_caching_config_cache_ttl_alias_roundtrip():
    # Both pythonic and JSON5-config (cache_ttl alias) should work
    a = PromptCachingConfig(enabled=False, cache_ttl="1h")
    b = PromptCachingConfig.model_validate({"enabled": False, "cache_ttl": "1h"})
    assert a.enabled is False
    assert a.cache_ttl == "1h"
    assert b.enabled is False
    assert b.cache_ttl == "1h"


# ---------------------------------------------------------------------------
# Provider integration shape
# ---------------------------------------------------------------------------


def test_provider_signature_accepts_cache_ttl_kwarg():
    """Stream_response in both routing and Anthropic transport must accept
    cache_ttl so loop.py can plumb it through unconditionally."""
    import inspect
    from nano_openclaw import _provider_anthropic, provider

    assert "cache_ttl" in inspect.signature(provider.stream_response).parameters
    assert "cache_ttl" in inspect.signature(_provider_anthropic.stream_response).parameters
