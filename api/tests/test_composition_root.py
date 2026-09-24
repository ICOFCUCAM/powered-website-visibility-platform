"""What `api/main.py` reads off its settings must actually be there.

This exists because it was not. The lifespan called `settings.redis_url` while
`redis_url` lived on `GoogleSettings` — a different class, populated from the
same variable, so every isolated test of the code either side passed and the
API crashed on start in production with an AttributeError.

Nothing catches that shape of bug except looking at the real objects together.
A unit test of the startup check mocks the client; a unit test of the config
builds the settings; neither one asks whether the composition root spells the
attribute the way the dataclass does.
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

from api.config import Settings

MAIN = pathlib.Path(__file__).resolve().parents[1] / "main.py"


def _attributes_read_off(name: str, tree: ast.AST) -> set[str]:
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == name
        and isinstance(node.ctx, ast.Load)
    }


def _available_on(cls: type) -> set[str]:
    """Dataclass fields, plus properties and methods — `is_production` and
    `google()` are read the same way a field is."""
    return {field.name for field in dataclasses.fields(cls)} | {
        attribute for attribute in dir(cls) if not attribute.startswith("_")
    }


def test_main_only_reads_settings_that_exist():
    read = _attributes_read_off("settings", ast.parse(MAIN.read_text()))
    assert read, "no settings access found — this check has stopped checking"

    missing = sorted(read - _available_on(Settings))
    assert missing == [], (
        f"api/main.py reads {missing} off Settings, which has no such "
        "attribute. This fails at startup, in production, after a green build."
    )


@pytest.mark.parametrize("name", ["redis_url", "database_url", "cors_origins"])
def test_the_settings_the_lifespan_depends_on_are_core_settings(name):
    """Named individually because each one is load-bearing at startup: without
    it the API either cannot serve or serves while broken."""
    assert name in {field.name for field in dataclasses.fields(Settings)}


def test_the_hub_still_gets_its_redis_url():
    """Moving `redis_url` onto Settings must not take it away from the Hub,
    which reads it for OAuth state through `GoogleSettings`."""
    from api.config import GoogleSettings

    assert "redis_url" in {field.name for field in dataclasses.fields(GoogleSettings)}
