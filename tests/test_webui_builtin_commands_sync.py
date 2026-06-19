"""Drift detection: WebUI's built-in slash catalogue matches core handlers.

The WebUI front-end (``nano_openclaw/adapters/webui/static/app.js``) maintains a
hand-written slash catalogue for UX affordances. All slash text is routed
through ``command.run``; this test keeps the static catalogue aligned with
the core server-side handlers while ignoring dynamic/plugin commands.

This test parses ``BUILTIN_COMMANDS`` straight out of app.js and asserts
that every verb in ``services/slash.py::_HANDLERS`` is present. New slash
commands therefore have to be wired into the front-end before this passes
green again.
"""

from __future__ import annotations

import re
from pathlib import Path

from nano_openclaw.services.slash import _HANDLERS

APP_JS = Path(__file__).resolve().parent.parent / "nano_openclaw" / "adapters" / "webui" / "static" / "app.js"


def _parse_builtin_commands(source: str) -> set[str]:
    """Pull verbs out of the ``const BUILTIN_COMMANDS = new Set([...])``
    declaration. Tolerates comments + multi-line layout."""
    match = re.search(
        r"const\s+BUILTIN_COMMANDS\s*=\s*new\s+Set\s*\(\s*\[(.*?)\]\s*\)\s*;",
        source,
        flags=re.DOTALL,
    )
    if not match:
        raise AssertionError("BUILTIN_COMMANDS literal not found in app.js")
    body = match.group(1)
    # Strip line comments before extracting strings.
    body = re.sub(r"//[^\n]*", "", body)
    return set(re.findall(r'"([^"]+)"', body))


def test_webui_builtin_commands_cover_slash_handlers():
    source = APP_JS.read_text(encoding="utf-8")
    front_end = _parse_builtin_commands(source)
    server_verbs = {key.lstrip("/") for key in _HANDLERS}

    missing = server_verbs - front_end
    assert not missing, (
        f"app.js BUILTIN_COMMANDS missing {sorted(missing)} — these slash "
        f"handlers exist server-side but the WebUI will leak them to "
        f"chat.send. Add them to BUILTIN_COMMANDS in app.js."
    )


def test_webui_builtin_commands_are_a_subset_or_known_extras():
    """Front-end may carry a few extras beyond _HANDLERS (banner verbs like
    ``help`` / ``quit`` that handle_slash recognizes specially, plus
    legacy ``save``). Anything else is a typo or a stale entry."""
    source = APP_JS.read_text(encoding="utf-8")
    front_end = _parse_builtin_commands(source)
    server_verbs = {key.lstrip("/") for key in _HANDLERS}
    known_extras = {"help", "quit", "exit", "q", "save"}
    extras = front_end - server_verbs - known_extras
    assert not extras, (
        f"app.js BUILTIN_COMMANDS has unexpected verbs {sorted(extras)} "
        f"that don't exist server-side. Either remove them from app.js or "
        f"add the matching handler to services/slash.py::_HANDLERS."
    )
