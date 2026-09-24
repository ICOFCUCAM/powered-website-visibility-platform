"""Every third-party import must be declared in pyproject.toml.

The failure this prevents is specific and was expensive: `selectolax` was
imported by `api/crawler/extract.py` and declared nowhere. It was present in
the development environment — installed at some point by hand — so every test
passed and every local run worked. The Docker image installs only what
pyproject declares, so the API died on import with `ModuleNotFoundError`
before serving a single request, and did so identically on five consecutive
deployments.

A static check rather than an import-time one, deliberately: importing the
modules would pass in exactly the environment where the mistake is invisible.
This reads the source and the manifest, so it gives the same answer on a
developer's machine as it does in the image.
"""

from __future__ import annotations

import ast
import pathlib
import sys
import tomllib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Distribution name -> the module it actually provides, where they differ.
#: Anything not listed is assumed to import under its own name with hyphens
#: turned into underscores.
PROVIDES = {
    "pyjwt": {"jwt"},
    # The `pool` extra brings psycopg_pool; the `binary` extra brings the
    # compiled driver, which is imported by psycopg itself rather than by us.
    "psycopg": {"psycopg", "psycopg_pool"},
    # FastAPI is a thin layer over Starlette and re-exports much of it, so
    # importing starlette directly is using a declared dependency.
    "fastapi": {"fastapi", "starlette"},
    "uvicorn": {"uvicorn"},
    "celery": {"celery", "kombu", "billiard"},
    "pydantic": {"pydantic", "pydantic_core"},
}


def declared_modules() -> set[str]:
    manifest = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    modules: set[str] = set()
    for spec in manifest["project"]["dependencies"]:
        name = spec.split("[")[0].split(">")[0].split("<")[0].split("=")[0]
        name = name.strip().lower()
        modules |= PROVIDES.get(name, {name.replace("-", "_")})
    return modules


def imported_modules() -> dict[str, set[str]]:
    """Top-level third-party module -> the files importing it.

    Tests are excluded: a test-only dependency belongs in the dev extra, and
    the image does not install it or copy them.
    """
    found: dict[str, set[str]] = {}
    for path in sorted((REPO_ROOT / "api").rglob("*.py")):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # `level > 0` is a relative import, which is always our own.
                names = (
                    [node.module.split(".")[0]]
                    if node.module and node.level == 0
                    else []
                )
            else:
                continue
            for name in names:
                if name == "api" or name in sys.stdlib_module_names:
                    continue
                found.setdefault(name, set()).add(
                    str(path.relative_to(REPO_ROOT))
                )
    return found


def test_no_import_is_undeclared():
    declared = declared_modules()
    undeclared = {
        module: files
        for module, files in imported_modules().items()
        if module not in declared
    }

    report = "\n".join(
        f"  {module}  (imported by {', '.join(sorted(files))})"
        for module, files in sorted(undeclared.items())
    )
    assert not undeclared, (
        "these modules are imported but not declared in pyproject.toml, so "
        "the Docker image will not contain them and the process will die on "
        f"import:\n{report}"
    )


def test_the_check_itself_still_sees_the_imports():
    """Guard against the check silently passing because it found nothing.

    An ast walk that stopped matching — a moved directory, a renamed package —
    would make the test above vacuously true, which is the failure mode of
    every check that asserts an empty set.
    """
    found = imported_modules()
    assert "fastapi" in found, "the import scan found no fastapi; it is broken"
    assert len(found) >= 5, f"suspiciously few third-party imports: {sorted(found)}"
