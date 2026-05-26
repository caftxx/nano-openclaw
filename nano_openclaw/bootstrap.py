"""First-run state_dir bootstrap.

When the user installs via ``pip install nano-openclaw`` / ``uv tool install``,
the source tree's ``.nano-openclaw-dev/`` template is bundled into the wheel
at ``nano_openclaw/_template/`` (see pyproject.toml ``force-include``). On
first launch we copy it into ``~/.nano-openclaw/`` so the user has a working
config skeleton to edit instead of staring at "config not found" errors.

Safety: only the global-home fallback branch (``source == "home"``) gets the
template treatment. If the user pointed ``NANO_OPENCLAW_STATE_DIR`` somewhere
explicit, or if they ran the CLI from a project that has its own
``.nano-openclaw/nano-openclaw.json5``, we don't touch the filesystem beyond
``mkdir -p`` — silently writing files into a user-chosen directory would be
surprising at best, destructive at worst.
"""

from __future__ import annotations

import shutil
from importlib.resources import as_file, files
from pathlib import Path

from nano_openclaw.config import StateDirSource

TEMPLATE_PACKAGE = "nano_openclaw"
TEMPLATE_DIRNAME = "_template"


def ensure_state_dir_initialized(state_dir: Path, *, source: StateDirSource) -> bool:
    """Make ``state_dir`` exist; return ``True`` if we wrote the template.

    Behavior matrix:

    =========================  ============================================
    state_dir exists?  source  Action
    =========================  ============================================
    yes                any     no-op, return False
    no                 home    copy bundled _template/* into state_dir,
                               return True
    no                 env|cwd mkdir state_dir only, return False
    =========================  ============================================
    """
    if state_dir.exists():
        return False

    state_dir.parent.mkdir(parents=True, exist_ok=True)

    if source != "home":
        state_dir.mkdir(parents=True, exist_ok=True)
        return False

    template_ref = files(TEMPLATE_PACKAGE).joinpath(TEMPLATE_DIRNAME)
    if not template_ref.is_dir():
        # Editable installs without the template bundled (e.g., a dev
        # checkout) — fall back to mkdir so the CLI still starts. The
        # repo-root ``.nano-openclaw-dev/`` is still discoverable manually.
        state_dir.mkdir(parents=True, exist_ok=True)
        return False

    with as_file(template_ref) as template_path:
        shutil.copytree(str(template_path), str(state_dir))
    return True
