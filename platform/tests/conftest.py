"""Fixtures shared by the suite.

Nothing here needs Postgres or a Docker daemon. The parts of the platform
worth testing hardest — what a repository will be built as, what ends up in
the image, what goes in a router file, how a secret is quoted — are pure
functions over a directory, and keeping them that way is what makes this
suite fast enough to run on every save.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def repo(tmp_path: Path):
    """Build a fake repository on disk from a {path: contents} mapping."""

    def make(files: dict[str, str | dict]) -> Path:
        root = tmp_path / "repo"
        root.mkdir(exist_ok=True)
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, dict):
                path.write_text(json.dumps(content))
            else:
                path.write_text(content)
        return root

    return make


@pytest.fixture
def package_json():
    def make(**kwargs) -> dict:
        base = {"name": "app", "version": "1.0.0", "scripts": {"build": "build"}}
        base.update(kwargs)
        return base

    return make
