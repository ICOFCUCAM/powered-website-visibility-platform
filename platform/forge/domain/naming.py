"""Names that end up in DNS.

Deployment hostnames are permanent and public, so they are generated once,
here, under rules strict enough that the result is always a valid DNS label:
lowercase, alphanumeric-or-hyphen, no leading or trailing hyphen, 63 bytes or
fewer.
"""

from __future__ import annotations

import re
import secrets
import unicodedata

_NOT_LABEL = re.compile(r"[^a-z0-9]+")

#: A DNS label may not exceed 63 octets. The suffix a deployment adds is
#: `-` plus eight hex characters, so the project's share of the label is
#: capped well below that and the total can never overflow.
MAX_LABEL = 63
SUFFIX_LENGTH = 8
MAX_SLUG = 39


def slugify(value: str, *, limit: int = MAX_SLUG) -> str:
    """Reduce arbitrary text to a DNS-safe label.

    Accents are folded rather than dropped, so `Café` becomes `cafe` and not
    `caf` — a small thing that stops two differently-named projects colliding
    on an empty slug.
    """
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    slug = _NOT_LABEL.sub("-", folded.lower()).strip("-")
    slug = slug[:limit].strip("-")
    if not slug:
        # Never raise here: an unnameable project is still a project, and the
        # caller can rename it. A random label is preferable to a failed
        # creation.
        return f"app-{secrets.token_hex(3)}"
    return slug


def deployment_short_id(project_slug: str) -> str:
    """`blog` -> `blog-3f9a2c71`.

    Random rather than sequential on purpose. A deployment URL is effectively
    unlisted — it is not secret, but nothing should be able to enumerate every
    preview of every project by counting.
    """
    suffix = secrets.token_hex(SUFFIX_LENGTH // 2)
    stem = project_slug[: MAX_LABEL - SUFFIX_LENGTH - 1].strip("-")
    return f"{stem}-{suffix}"


def container_name(short_id: str) -> str:
    return f"forge-{short_id}"


def image_tag(project_slug: str, git_sha: str) -> str:
    """Tagged by content, not by deployment.

    Two deployments of the same commit produce the same tag and the second
    build is almost entirely cache hits. A redeploy of an unchanged commit is
    therefore nearly free, which is what makes "redeploy to pick up a changed
    environment variable" a reasonable thing to offer.
    """
    return f"forge/{project_slug}:{git_sha[:12]}"


def router_id(short_id: str, *, production: bool = False) -> str:
    """Traefik router and service names.

    Production routes get their own router on the same container so that
    promotion is an attach, not a rewrite: the deployment's permanent URL and
    the project's production domains resolve through two independent routers.
    """
    return f"{short_id}-prod" if production else short_id
