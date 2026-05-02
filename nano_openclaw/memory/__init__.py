"""Memory modules for nano-openclaw.

Mirrors openclaw memory-core plugin functionality:
- Daily memory file loading (startup context)
- Memory tools (memory_get, memory_search)
- Active Memory (automatic recall before main reply)
"""

from nano_openclaw.memory.daily import build_daily_memory_prelude
from nano_openclaw.memory.active import (
    ActiveMemoryManager,
    ActiveMemoryConfig,
    ActiveMemoryResult,
    QueryMode,
    PromptStyle,
)