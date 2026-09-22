"""Getting secrets to a build and a container without mangling them."""

from __future__ import annotations

import stat
import subprocess

from forge.domain.models import EnvTarget
from forge.engine.environment import (
    Environment,
    _split,
    platform_variables,
    write_build_secret,
    write_runtime_env_file,
)


def test_a_value_containing_a_quote_cannot_escape_the_shell(tmp_path):
    """The build sources this file. An unescaped value would be executable.

    `hunter2'; rm -rf /; echo '` is the whole attack, and single-quote
    escaping is the whole defence.
    """
    env = _split({"PASSWORD": "hunter2'; rm -rf /; echo '"})
    path = write_build_secret(env, tmp_path / "build.env")
    content = path.read_text()
    assert "rm -rf" in content
    assert content.startswith("PASSWORD='")
    # Proof rather than inspection: ask a shell what it actually reads back.
    result = subprocess.run(
        ["sh", "-c", f'. {path}; printf %s "$PASSWORD"'],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == "hunter2'; rm -rf /; echo '"


def test_the_runtime_file_is_literal_and_unquoted(tmp_path):
    """`docker --env-file` is not a shell. Quoting a value there would put the
    quotes inside the variable."""
    env = _split({"GREETING": "hello world"})
    path = write_runtime_env_file(env, tmp_path / "runtime.env")
    assert path.read_text() == "GREETING=hello world\n"


def test_multiline_values_are_kept_out_of_the_env_file(tmp_path):
    """A PEM key has newlines, and the env-file format cannot express one —
    Docker would read the second line as a separate, malformed variable."""
    pem = "-----BEGIN KEY-----\nabc\n-----END KEY-----"
    env = _split({"TLS_KEY": pem, "PORT_NAME": "web"})

    assert env.inline == {"TLS_KEY": pem}
    assert env.file_safe == {"PORT_NAME": "web"}

    path = write_runtime_env_file(env, tmp_path / "runtime.env")
    assert "BEGIN KEY" not in path.read_text()
    # But the build, which does source a shell file, still sees it.
    assert "BEGIN KEY" in write_build_secret(env, tmp_path / "b.env").read_text()


def test_both_files_are_created_unreadable_by_anyone_else(tmp_path):
    """These hold every plaintext secret for a project while a build runs."""
    env = _split({"SECRET": "x"})
    for name, write in (
        ("build.env", write_build_secret),
        ("runtime.env", write_runtime_env_file),
    ):
        path = write(env, tmp_path / name)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_an_empty_environment_still_writes_a_sourceable_file(tmp_path):
    path = write_build_secret(Environment({}, {}), tmp_path / "build.env")
    assert path.read_text() == ""


def test_the_platform_tells_a_deployment_its_own_url():
    """An app cannot know its preview hostname at commit time, and needs it
    for canonical tags and OAuth redirects."""
    variables = platform_variables(
        short_id="blog-abc123",
        git_sha="f" * 40,
        url="https://blog-abc123.deploys.example.com",
        target=EnvTarget.PREVIEW,
    )
    assert variables["FORGE_URL"] == "https://blog-abc123.deploys.example.com"
    assert variables["FORGE_ENV"] == "preview"
