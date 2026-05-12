"""Fuzzy matching for V4A patch hunk localization.

Slimmed-down 4-strategy chain ported from hermes-agent/tools/fuzzy_match.py.
Strategies tried in order:

1. ``exact`` — direct string comparison.
2. ``line_trimmed`` — strip leading/trailing whitespace per line.
3. ``whitespace_normalized`` — collapse runs of spaces/tabs to a single space.
4. ``indentation_flexible`` — strip leading whitespace from every line.

The richer strategies in hermes (unicode_map / escape_normalized /
trimmed_boundary / block_anchor / context_aware) are intentionally **not**
ported — they bring more false-positive risk than benefit at this scale.

Usage::

    new_content, count, strategy, error = fuzzy_find_and_replace(
        content, old_string, new_string, replace_all=False
    )
"""

from __future__ import annotations

import re
from typing import Callable, List, Optional, Tuple


def fuzzy_find_and_replace(
    content: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> Tuple[str, int, Optional[str], Optional[str]]:
    """Find and replace text using a chain of fuzzy matching strategies.

    Args:
        content: The file content to search in.
        old_string: The text to find.
        new_string: The replacement text.
        replace_all: If True, replace all occurrences; if False, require uniqueness.

    Returns:
        Tuple of ``(new_content, match_count, strategy_name, error_message)``.
        On success: ``(modified_content, count, strategy_used, None)``.
        On failure: ``(original_content, 0, None, error_description)``.
    """
    if not old_string:
        return content, 0, None, "old_string cannot be empty"

    if old_string == new_string:
        return content, 0, None, "old_string and new_string are identical"

    strategies: List[Tuple[str, Callable[[str, str], List[Tuple[int, int]]]]] = [
        ("exact", _strategy_exact),
        ("line_trimmed", _strategy_line_trimmed),
        ("whitespace_normalized", _strategy_whitespace_normalized),
        ("indentation_flexible", _strategy_indentation_flexible),
    ]

    for strategy_name, strategy_fn in strategies:
        matches = strategy_fn(content, old_string)

        if matches:
            if len(matches) > 1 and not replace_all:
                return content, 0, None, (
                    f"Found {len(matches)} matches for old_string. "
                    "Provide more context to make it unique, or use replace_all=True."
                )

            new_content = _apply_replacements(content, matches, new_string)
            return new_content, len(matches), strategy_name, None

    return content, 0, None, "Could not find a match for old_string in the file"


def _apply_replacements(
    content: str, matches: List[Tuple[int, int]], new_string: str
) -> str:
    """Apply replacements at the given positions (right-to-left to preserve offsets)."""
    sorted_matches = sorted(matches, key=lambda x: x[0], reverse=True)
    result = content
    for start, end in sorted_matches:
        result = result[:start] + new_string + result[end:]
    return result


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


def _strategy_exact(content: str, pattern: str) -> List[Tuple[int, int]]:
    """Strategy 1: exact string match."""
    matches: List[Tuple[int, int]] = []
    start = 0
    while True:
        pos = content.find(pattern, start)
        if pos == -1:
            break
        matches.append((pos, pos + len(pattern)))
        start = pos + 1
    return matches


def _strategy_line_trimmed(content: str, pattern: str) -> List[Tuple[int, int]]:
    """Strategy 2: per-line leading/trailing whitespace stripping."""
    pattern_lines = [line.strip() for line in pattern.split("\n")]
    pattern_normalized = "\n".join(pattern_lines)

    content_lines = content.split("\n")
    content_normalized_lines = [line.strip() for line in content_lines]

    return _find_normalized_matches(
        content,
        content_lines,
        content_normalized_lines,
        pattern,
        pattern_normalized,
    )


def _strategy_whitespace_normalized(content: str, pattern: str) -> List[Tuple[int, int]]:
    """Strategy 3: collapse runs of spaces/tabs to a single space."""

    def normalize(s: str) -> str:
        return re.sub(r"[ \t]+", " ", s)

    pattern_normalized = normalize(pattern)
    content_normalized = normalize(content)

    matches_in_normalized = _strategy_exact(content_normalized, pattern_normalized)
    if not matches_in_normalized:
        return []

    return _map_normalized_positions(content, content_normalized, matches_in_normalized)


def _strategy_indentation_flexible(content: str, pattern: str) -> List[Tuple[int, int]]:
    """Strategy 4: ignore indentation differences entirely."""
    content_lines = content.split("\n")
    content_stripped_lines = [line.lstrip() for line in content_lines]
    pattern_lines = [line.lstrip() for line in pattern.split("\n")]

    return _find_normalized_matches(
        content,
        content_lines,
        content_stripped_lines,
        pattern,
        "\n".join(pattern_lines),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _calculate_line_positions(
    content_lines: List[str],
    start_line: int,
    end_line: int,
    content_length: int,
) -> Tuple[int, int]:
    """Calculate start and end character positions from line indices."""
    start_pos = sum(len(line) + 1 for line in content_lines[:start_line])
    end_pos = sum(len(line) + 1 for line in content_lines[:end_line]) - 1
    if end_pos >= content_length:
        end_pos = content_length
    return start_pos, end_pos


def _find_normalized_matches(
    content: str,
    content_lines: List[str],
    content_normalized_lines: List[str],
    pattern: str,
    pattern_normalized: str,
) -> List[Tuple[int, int]]:
    """Find matches in normalized line lists and map back to original positions."""
    pattern_norm_lines = pattern_normalized.split("\n")
    num_pattern_lines = len(pattern_norm_lines)

    matches: List[Tuple[int, int]] = []

    for i in range(len(content_normalized_lines) - num_pattern_lines + 1):
        block = "\n".join(content_normalized_lines[i : i + num_pattern_lines])
        if block == pattern_normalized:
            start_pos, end_pos = _calculate_line_positions(
                content_lines, i, i + num_pattern_lines, len(content)
            )
            matches.append((start_pos, end_pos))

    return matches


def _map_normalized_positions(
    original: str,
    normalized: str,
    normalized_matches: List[Tuple[int, int]],
) -> List[Tuple[int, int]]:
    """Map positions from a whitespace-normalized string back to the original."""
    if not normalized_matches:
        return []

    orig_to_norm: List[int] = []

    orig_idx = 0
    norm_idx = 0

    while orig_idx < len(original) and norm_idx < len(normalized):
        if original[orig_idx] == normalized[norm_idx]:
            orig_to_norm.append(norm_idx)
            orig_idx += 1
            norm_idx += 1
        elif original[orig_idx] in " \t" and normalized[norm_idx] == " ":
            orig_to_norm.append(norm_idx)
            orig_idx += 1
            if orig_idx < len(original) and original[orig_idx] not in " \t":
                norm_idx += 1
        elif original[orig_idx] in " \t":
            orig_to_norm.append(norm_idx)
            orig_idx += 1
        else:
            orig_to_norm.append(norm_idx)
            orig_idx += 1

    while orig_idx < len(original):
        orig_to_norm.append(len(normalized))
        orig_idx += 1

    norm_to_orig_start: dict[int, int] = {}
    norm_to_orig_end: dict[int, int] = {}

    for orig_pos, norm_pos in enumerate(orig_to_norm):
        if norm_pos not in norm_to_orig_start:
            norm_to_orig_start[norm_pos] = orig_pos
        norm_to_orig_end[norm_pos] = orig_pos

    original_matches: List[Tuple[int, int]] = []
    for norm_start, norm_end in normalized_matches:
        if norm_start in norm_to_orig_start:
            orig_start = norm_to_orig_start[norm_start]
        else:
            orig_start = min(i for i, n in enumerate(orig_to_norm) if n >= norm_start)

        if norm_end - 1 in norm_to_orig_end:
            orig_end = norm_to_orig_end[norm_end - 1] + 1
        else:
            orig_end = orig_start + (norm_end - norm_start)

        while orig_end < len(original) and original[orig_end] in " \t":
            orig_end += 1

        original_matches.append((orig_start, min(orig_end, len(original))))

    return original_matches
