"""Names that become DNS labels."""

from __future__ import annotations

import re

from forge.domain.naming import MAX_LABEL, deployment_short_id, image_tag, slugify

LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def test_accents_fold_rather_than_vanish():
    assert slugify("Café Deluxe") == "cafe-deluxe"


def test_punctuation_and_spacing_collapse_to_single_hyphens():
    assert slugify("  My  Great   Site!! ") == "my-great-site"


def test_a_name_with_nothing_usable_still_produces_a_valid_label():
    """An unnameable project is still a project. A random label beats
    refusing to create it."""
    slug = slugify("日本語")
    assert LABEL.match(slug)


def test_a_slug_never_ends_in_a_hyphen_after_truncation():
    slug = slugify("a" * 38 + "-" + "b" * 20)
    assert LABEL.match(slug)
    assert not slug.endswith("-")


def test_deployment_ids_fit_in_a_dns_label_even_from_a_maximal_slug():
    short = deployment_short_id("x" * 39)
    assert len(short) <= MAX_LABEL
    assert LABEL.match(short)


def test_deployment_ids_are_unpredictable():
    """Sequential ids would let anyone enumerate every preview of every
    project by counting."""
    ids = {deployment_short_id("blog") for _ in range(200)}
    assert len(ids) == 200


def test_the_image_tag_is_keyed_on_the_commit_not_the_deployment():
    """Two deploys of one commit reuse the image, which is what makes
    'redeploy to pick up a changed variable' cheap."""
    assert image_tag("blog", "a" * 40) == image_tag("blog", "a" * 40)
    assert image_tag("blog", "a" * 40) != image_tag("blog", "b" * 40)
