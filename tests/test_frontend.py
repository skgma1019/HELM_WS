from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_dashboard_and_map() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("프론트 회귀 검증에는 Node.js가 필요하다")
    result = subprocess.run(
        [node, "tests/frontend.cjs"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
