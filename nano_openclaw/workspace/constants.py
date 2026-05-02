"""Bootstrap file name constants and ordering.

Mirrors openclaw workspace.ts:19-26 (filename constants) and
system-prompt.ts:44-52 (CONTEXT_FILE_ORDER).
"""

from __future__ import annotations

# Standard bootstrap file names (openclaw workspace.ts:19-26)
DEFAULT_AGENTS_FILENAME = "AGENTS.md"
DEFAULT_SOUL_FILENAME = "SOUL.md"
DEFAULT_TOOLS_FILENAME = "TOOLS.md"
DEFAULT_IDENTITY_FILENAME = "IDENTITY.md"
DEFAULT_USER_FILENAME = "USER.md"
DEFAULT_HEARTBEAT_FILENAME = "HEARTBEAT.md"
DEFAULT_BOOTSTRAP_FILENAME = "BOOTSTRAP.md"
DEFAULT_MEMORY_FILENAME = "MEMORY.md"

BOOTSTRAP_FILES: list[str] = [
    DEFAULT_AGENTS_FILENAME,
    DEFAULT_SOUL_FILENAME,
    DEFAULT_IDENTITY_FILENAME,
    DEFAULT_USER_FILENAME,
    DEFAULT_TOOLS_FILENAME,
    DEFAULT_BOOTSTRAP_FILENAME,
    DEFAULT_MEMORY_FILENAME,
    DEFAULT_HEARTBEAT_FILENAME,
]

# Injection order priority (lower = earlier in system prompt)
# Mirrors openclaw system-prompt.ts:44-52
CONTEXT_FILE_ORDER: dict[str, int] = {
    "agents.md": 10,
    "soul.md": 20,
    "identity.md": 30,
    "user.md": 40,
    "tools.md": 50,
    "bootstrap.md": 60,
    "memory.md": 70,
}

# Sub-agent session whitelist (openclaw workspace.ts:669-685)
# Sub-agents only receive these 5 core files to keep context lean.
MINIMAL_BOOTSTRAP_ALLOWLIST: set[str] = {
    DEFAULT_AGENTS_FILENAME,
    DEFAULT_TOOLS_FILENAME,
    DEFAULT_SOUL_FILENAME,
    DEFAULT_IDENTITY_FILENAME,
    DEFAULT_USER_FILENAME,
}

# Daily memory directory name (openclaw startup-context.ts:227)
DEFAULT_MEMORY_DIR = "memory"

# Daily memory file pattern: YYYY-MM-DD.md or YYYY-MM-DD-slug.md
DAILY_MEMORY_PATTERN = r"^\d{4}-\d{2}-\d{2}"  # regex prefix for date stamps

# Default days to load (today + yesterday, openclaw startup-context.ts:10)
DEFAULT_DAILY_MEMORY_DAYS = 2
MAX_DAILY_MEMORY_DAYS = 14

# Character limits (openclaw startup-context.ts:7-9)
DAILY_MEMORY_FILE_MAX_CHARS = 1200
DAILY_MEMORY_TOTAL_MAX_CHARS = 2800
MAX_SLUGGED_FILES_PER_DAY = 4
