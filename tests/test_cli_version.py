"""`-v` / `--version` print the package version and exit cleanly.

Spawns the real CLI as a subprocess (matching the gateway lifecycle tests)
so the argparse ``version`` action is exercised end-to-end, including its
exit-0 behavior, without polluting the test process.
"""

from __future__ import annotations

import subprocess
import sys
from importlib.metadata import version

import pytest


@pytest.mark.parametrize("flag", ["-v", "--version"])
def test_version_flag_prints_version_and_exits_zero(flag: str) -> None:
    expected = version("nano-openclaw")
    result = subprocess.run(
        [sys.executable, "-m", "nano_openclaw", flag],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"nano-openclaw {expected}"
