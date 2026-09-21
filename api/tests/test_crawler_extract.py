"""HTML extraction.

The extractor records what is on the page and forms no opinion. A missing
title is a missing title, not an issue — detection is a separate, versioned
step, so changing a rule never requires re-crawling.
"""

from __future__ import annotations

from api.crawler.extract import extract

URL = "https://example.com/services/"

PAGE = """
<!doctype html>
<html lang="en-GB">
<head>
  <title>  Counselling   services  </title>
  <meta name="description" content="Christian counselling in London.">
  <meta name="viewport" content="width=device-width">
  <meta name="robots" content="index, follow, max-snippet:-1">
  <meta property="og:title" content="Counselling">
  <link rel="canonical" href="/services/">
  <link rel="alternate" hreflang="cy" href="/cy/services/">
  <script type="application/ld+json">
    {"@context":"https://schema.org","@graph":[
      {"@type":"Organization","name":"Example"},
      {"@type":["LocalBusiness","Church"],"name":"Example"}]}
  </script>
</head>
<body>
  <h1>Counselling services</h1>
  <h2>What to expect</h2><h2>Fees</h2>
  <p>We offer one-to-one counselling from our church in London.</p>
  <a href="/about">About us</a>
  <a href="about-2">Relative</a>
  <a href="https://www.example.com/contact">Contact</a>
  <a href="https://other.test/x" rel="nofollow noopener">Elsewhere</a>
  <a href="#top">Skip</a>
  <a>No href</a>
  <img src="/a.jpg" alt="A room">
  <img src="/b.jpg" alt="">
  <img src="/c.jpg">
</body></html>
"""


def test_extracts_the_head_facts():
    facts = extract(PAGE, url=URL)
    assert facts.title == "Counselling services"          # whitespace collapsed
    assert facts.meta_description == "Christian counselling in London."
    assert facts.lang == "en-GB"
    assert facts.viewport_present is True
    assert facts.robots_meta == ["index", "follow", "max-snippet:-1"]
    assert facts.open_graph == {"og:title": "Counselling"}


def test_a_relative_canonical_is_resolved_against_the_page():
    facts = extract(PAGE, url=URL)
    assert facts.canonical_url == "https://example.com/services/"
    assert facts.hreflang == [
        {"hreflang": "cy", "href": "https://example.com/cy/services/"}
    ]


def test_headings_are_counted_and_h1s_kept():
    facts = extract(PAGE, url=URL)
    assert facts.h1 == ["Counselling services"]
    assert facts.heading_counts == {"h1": 1, "h2": 2}


def test_an_empty_alt_is_not_a_missing_alt():
    """Empty alt is a valid way to mark an image decorative. Absent is an
    omission. Conflating them would flag correct markup as a problem."""
    facts = extract(PAGE, url=URL)
    assert facts.images_total == 3
    assert facts.images_missing_alt == 1


def test_links_are_absolutised_and_split_by_site():
    facts = extract(PAGE, url=URL)
    internal = {link.url for link in facts.internal_links}
    assert internal == {
        "https://example.com/about",
        "https://example.com/services/about-2",   # resolved against the path
        "https://www.example.com/contact",        # www counts as the same site
        "https://example.com/services/",          # #top is a link to this page
    }
    external = facts.external_links
    assert [link.url for link in external] == ["https://other.test/x"]
    assert external[0].rel == ["nofollow", "noopener"]


def test_a_fragment_only_link_is_still_recorded_as_the_page_itself():
    facts = extract(PAGE, url=URL)
    assert any(link.url.startswith(URL) for link in facts.links)


def test_nested_json_ld_types_are_found():
    facts = extract(PAGE, url=URL)
    assert set(facts.schema_types) == {"Organization", "LocalBusiness", "Church"}
    assert facts.schema_errors == []


def test_broken_structured_data_is_a_finding_not_a_crash():
    html = (
        '<html><head><script type="application/ld+json">{oops</script>'
        "</head><body/></html>"
    )
    facts = extract(html, url=URL)
    assert facts.schema_types == []
    assert len(facts.schema_errors) == 1
    assert "invalid JSON-LD" in facts.schema_errors[0]


def test_identical_text_hashes_identically_so_change_detection_works():
    a = extract("<html><body><p>Same words here</p></body></html>", url=URL)
    b = extract("<html><body>  <p>Same   words here</p>  </body></html>", url=URL)
    c = extract("<html><body><p>Different</p></body></html>", url=URL)
    assert a.text_hash == b.text_hash
    assert a.text_hash != c.text_hash


def test_a_javascript_shell_is_recognisable():
    """The signal for render escalation: almost no body text, a lot of script,
    and an SPA mount point."""
    shell = (
        '<html><head><title>App</title></head><body><div id="root"></div>'
        '<script>' + ("x" * 60_000) + "</script></body></html>"
    )
    facts = extract(shell, url=URL)
    assert facts.has_spa_root is True
    assert facts.script_bytes > 50_000
    # The 60 KB of script must not count as page content, or every SPA looks
    # like a long article and never gets rendered.
    assert facts.body_text_length < 50

    normal = extract(PAGE, url=URL)
    assert normal.has_spa_root is False
    assert normal.body_text_length > 100
    assert normal.word_count > 15


def test_an_empty_page_does_not_blow_up():
    facts = extract("", url=URL)
    assert facts.title is None
    assert facts.word_count == 0
    assert facts.links == []
