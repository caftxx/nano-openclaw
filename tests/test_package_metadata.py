from __future__ import annotations

import re
from pathlib import Path


def test_mcp_sdk_dependency_excludes_breaking_v2() -> None:
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    metadata = pyproject.read_text(encoding="utf-8")

    assert re.search(r'^\s*"mcp>=1\.28,<2",\s*$', metadata, re.MULTILINE)
