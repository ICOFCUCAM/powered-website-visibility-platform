"""What a repository gets built as.

These are the rules a deploy platform is judged on. A wrong answer here is not
a crash — it is an image that builds, starts, and serves the wrong thing.
"""

from __future__ import annotations

import pytest

from forge.domain.detect import Overrides, detect
from forge.domain.errors import DetectionFailed


def test_next_standalone_is_detected_from_the_config_not_the_dependency(
    repo, package_json
):
    app = repo(
        {
            "package.json": package_json(dependencies={"next": "15.0.0"}),
            "package-lock.json": "{}",
            "next.config.mjs": "export default { output: 'standalone' }",
        }
    )
    plan = detect(app)
    assert plan.framework == "next-standalone"
    # The runtime stage must carry the two things Next's tracing leaves out.
    assert "/app/.next/static" in plan.dockerfile
    assert "/app/public" in plan.dockerfile


def test_next_export_is_served_as_static_files_with_no_node_runtime(repo, package_json):
    app = repo(
        {
            "package.json": package_json(dependencies={"next": "15.0.0"}),
            "next.config.js": "module.exports = { output: 'export' }",
        }
    )
    plan = detect(app)
    assert plan.framework == "next-export"
    runtime = plan.dockerfile.split("AS run")[-1]
    assert "node" not in runtime.lower()
    assert "caddy" in plan.dockerfile


def test_a_commented_out_output_setting_is_not_read_as_standalone(repo, package_json):
    """The regex must not be fooled by a config that only mentions the word.

    Getting this wrong produces the worst failure mode available: a build that
    succeeds and an image whose CMD references a server.js that was never
    generated.
    """
    app = repo(
        {
            "package.json": package_json(dependencies={"next": "15.0.0"}),
            "next.config.js": "module.exports = { /* output: 'standalone' */ }",
        }
    )
    assert detect(app).framework == "next"


@pytest.mark.parametrize(
    ("lockfile", "expected"),
    [
        ("pnpm-lock.yaml", "pnpm install --frozen-lockfile"),
        ("yarn.lock", "yarn install --frozen-lockfile"),
        ("package-lock.json", "npm ci"),
        ("bun.lockb", "bun install --frozen-lockfile"),
    ],
)
def test_lockfile_chooses_the_package_manager(repo, package_json, lockfile, expected):
    """Each case needs its own tree: lockfiles are checked in precedence
    order, so a leftover pnpm-lock.yaml would answer for every later case."""
    app = repo(
        {
            "package.json": package_json(devDependencies={"vite": "5"}),
            "vite.config.ts": "export default {}",
            lockfile: "",
        }
    )
    assert expected in detect(app).dockerfile


def test_a_missing_lockfile_is_a_note_not_a_failure(repo, package_json):
    app = repo(
        {
            "package.json": package_json(devDependencies={"vite": "5"}),
            "vite.config.ts": "export default {}",
        }
    )
    plan = detect(app)
    assert plan.framework == "vite"
    assert any("lockfile" in note for note in plan.notes)


def test_spa_and_exported_sites_get_different_fallback_rules(repo, package_json):
    """A single-page app needs /settings to serve index.html. A statically
    exported site must 404 instead, or every wrong URL becomes a duplicate of
    the home page in a search index."""
    spa = detect(
        repo(
            {
                "package.json": package_json(devDependencies={"vite": "5"}),
                "vite.config.ts": "export default {}",
            }
        )
    )
    exported = detect(
        repo(
            {
                "package.json": package_json(dependencies={"astro": "4"}),
                "astro.config.mjs": "export default {}",
            }
        )
    )
    spa_rule = _try_files(dict(spa.context_files)["Caddyfile"])
    exported_rule = _try_files(dict(exported.context_files)["Caddyfile"])

    # The distinction is the final fallback. `{path}/index.html` is a
    # directory index and correct for both; a bare `/index.html` at the end is
    # the SPA catch-all that makes every unknown URL return the app shell.
    assert spa_rule.split()[-1] == "/index.html"
    assert exported_rule.split()[-1] != "/index.html"


def _try_files(caddyfile: str) -> str:
    line = next(ln for ln in caddyfile.splitlines() if "try_files" in ln)
    return line.split("try_files", 1)[1].strip()


def test_astro_with_the_node_adapter_becomes_a_server(repo, package_json):
    app = repo(
        {
            "package.json": package_json(
                dependencies={"astro": "4", "@astrojs/node": "8"}
            ),
            "astro.config.mjs": "export default {}",
        }
    )
    plan = detect(app)
    assert plan.framework == "astro-node"
    assert "dist/server/entry.mjs" in plan.dockerfile


def test_sveltekit_adapter_auto_is_flagged_before_it_fails_in_the_build(
    repo, package_json
):
    app = repo(
        {
            "package.json": package_json(
                dependencies={"@sveltejs/kit": "2", "@sveltejs/adapter-auto": "3"}
            ),
            "svelte.config.js": "export default {}",
        }
    )
    plan = detect(app)
    assert any("adapter-node" in note for note in plan.notes)


def test_fastapi_entrypoint_is_read_from_the_source_not_guessed(repo):
    app = repo(
        {
            "requirements.txt": "fastapi\n",
            "app/main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
        }
    )
    plan = detect(app)
    assert plan.framework == "python-asgi"
    assert "uvicorn app.main:app" in plan.dockerfile


def test_a_python_project_with_no_app_object_fails_with_advice(repo):
    app = repo({"requirements.txt": "requests\n", "main.py": "print('hi')\n"})
    with pytest.raises(DetectionFailed) as exc:
        detect(app)
    assert "start command" in str(exc.value)


def test_django_is_found_through_its_wsgi_module(repo):
    app = repo(
        {
            "requirements.txt": "Django==5.0\n",
            "manage.py": "",
            "mysite/wsgi.py": "application = None\n",
        }
    )
    plan = detect(app)
    assert plan.framework == "django"
    assert "mysite.wsgi:application" in plan.dockerfile


def test_a_repository_dockerfile_wins_and_its_port_is_honoured(repo):
    app = repo(
        {
            "Dockerfile": "FROM nginx\nEXPOSE 8123\n",
            "package.json": '{"dependencies":{"next":"15"}}',
        }
    )
    plan = detect(app)
    assert plan.from_repo is True
    assert plan.framework == "dockerfile"
    assert plan.port == 8123


def test_the_last_expose_wins(repo):
    app = repo({"Dockerfile": "FROM x\nEXPOSE 3000\nFROM y\nEXPOSE 9000\n"})
    assert detect(app).port == 9000


def test_project_settings_override_a_detected_framework(repo, package_json):
    app = repo(
        {
            "package.json": package_json(dependencies={"next": "15"}),
            "next.config.js": "module.exports = {}",
        }
    )
    plan = detect(app, Overrides(start_command="node custom-server.js", port=4321))
    assert "custom-server.js" in plan.dockerfile
    assert plan.port == 4321


def test_forge_json_is_read_when_project_settings_are_silent(repo, package_json):
    app = repo(
        {
            "package.json": package_json(),
            "forge.json": '{"startCommand": "node serve.js", "port": 7000}',
        }
    )
    plan = detect(app)
    assert "serve.js" in plan.dockerfile
    assert plan.port == 7000


def test_an_unrecognisable_directory_says_what_it_looked_for(repo):
    app = repo({"README.md": "# nothing to build"})
    with pytest.raises(DetectionFailed) as exc:
        detect(app)
    message = str(exc.value)
    assert "package.json" in message and "Dockerfile" in message
