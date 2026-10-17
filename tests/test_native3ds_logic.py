"""Compile and run the native 3DS app's host-side C tests (config and /api/screen parsing)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "native3ds"


@pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler")
def test_native3ds_logic(tmp_path: Path) -> None:
    exe = tmp_path / "test_logic"
    sources = [ROOT / "source" / "larre_cfg.c", ROOT / "source" / "screen.c", ROOT / "test" / "test_logic.c"]
    subprocess.run(["cc", "-std=c99", "-Wall", "-Werror", "-I", str(ROOT / "source"), "-o", str(exe),
                    *map(str, sources)], check=True)
    result = subprocess.run([str(exe)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
