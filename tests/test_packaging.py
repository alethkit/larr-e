"""The installed `larr-e` command only sees modules listed in pyproject.toml's py-modules.

Tests import from the repo folder, so a missing entry passes every other test and still breaks
`uv run larr-e` from a fresh clone (openai_compat was missing on 17 Oct).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_every_top_level_module_is_packaged():
    listed = set(tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["py-modules"])
    on_disk = {p.stem for p in ROOT.glob("*.py")}
    assert on_disk - listed == set()
