"""What a build is, once detection has decided.

Separated from detection so the generators below can be tested against a plan
without a repository on disk, and so the engine depends on this small type
rather than on the detection rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Base images, pinned by major version in one place. A framework bump is a
#: change here and nowhere else.
NODE_IMAGE = "node:22-alpine"
PYTHON_IMAGE = "python:3.12-slim"
GO_IMAGE = "golang:1.23-alpine"
STATIC_IMAGE = "caddy:2-alpine"

#: The port every generated image listens on. Fixed rather than detected,
#: because nothing else on the host can collide with it: each deployment has
#: its own network namespace and the router reaches it by container name.
#: Apps that read $PORT get this value; apps that hardcode one get it too,
#: because the generator writes it into the start command.
DEFAULT_PORT = 8080


@dataclass(frozen=True, slots=True)
class BuildPlan:
    """A complete, self-contained recipe for turning a directory into a running
    container.

    `dockerfile` is the whole file as text. It is written into the build
    context rather than committed to the user's repository, so a framework fix
    reaches every project on its next deploy without anyone editing anything.
    """

    #: Identifier recorded on the deployment, e.g. `next-standalone`.
    framework: str

    #: Human sentence written to the deployment log, so the owner can see what
    #: was decided and why before the build output starts scrolling.
    reason: str

    dockerfile: str
    port: int = DEFAULT_PORT

    #: True when the repository supplied its own Dockerfile and detection
    #: stepped aside. Recorded because "the platform built this" and "you
    #: built this" are different support conversations.
    from_repo: bool = False

    #: Advice that does not block the build: a faster configuration, a missing
    #: lockfile. Written to the log after the decision.
    notes: tuple[str, ...] = field(default_factory=tuple)

    #: Extra files the generator needs written into the build context beside
    #: the Dockerfile, as (relative path, contents). A static site's Caddyfile
    #: arrives this way rather than being squeezed through `printf` inside a
    #: RUN line, which is how it stays readable and reviewable.
    context_files: tuple[tuple[str, str], ...] = field(default_factory=tuple)
