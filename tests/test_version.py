from __future__ import annotations

import tomllib
from pathlib import Path

from c5bot import __version__


def test_version_matches_pyproject():
    data = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"] == __version__
