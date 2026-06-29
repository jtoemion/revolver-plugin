from __future__ import annotations

import subprocess
import sys


def test_module_selftest_passes() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "revolver"],
        check=False,
        capture_output=True,
        encoding="utf-8",
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "=== all tests passed ===" in result.stdout
