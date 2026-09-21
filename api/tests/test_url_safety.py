"""The guard that decides whether we are willing to send a request.

Every case here is an attack somebody has actually used. A server that will
fetch any URL on request will read its own cloud metadata endpoint and hand
the credentials to whoever asked, so this file is mostly a list of the ways
that has been done.
"""

from __future__ import annotations

import pytest

from api.crawler.safety import UnsafeUrl, check_peer, check_url


# -- the thing it is actually for -------------------------------------------
def test_the_cloud_metadata_endpoint_is_refused():
    """169.254.169.254 is link-local. On every major cloud it serves the
    instance's own credentials to anything that asks."""
    with pytest.raises(UnsafeUrl):
        check_url("http://169.254.169.254/latest/meta-data/iam/")


def test_loopback_is_refused_however_it_is_spelled():
    for url in (
        "http://127.0.0.1/",
        "http://127.1/",                    # the short form is still loopback
        "http://[::1]/",
        "http://localhost/",
        "http://LOCALHOST/",
        "http://localhost./",               # trailing dot is the same host
        "http://[::ffff:127.0.0.1]/",       # loopback wearing an IPv6 hat
    ):
        with pytest.raises(UnsafeUrl):
            check_url(url)


def test_private_ranges_are_refused():
    for url in (
        "http://10.0.0.1/", "http://192.168.1.1/", "http://172.16.0.1/",
        "http://[fc00::1]/",                # IPv6 unique-local
        "http://[fe80::1]/",                # IPv6 link-local
        "http://100.64.0.1/",               # carrier-grade NAT
        "http://0.0.0.0/",
    ):
        with pytest.raises(UnsafeUrl):
            check_url(url)


# -- the ways people get around a naive check --------------------------------
def test_only_the_web_schemes_are_allowed():
    """A redirect may name any scheme it likes, and file:// reads the disk."""
    for url in (
        "file:///etc/passwd",
        "gopher://127.0.0.1:11211/_stats",   # the memcached classic
        "ftp://internal/",
        "dict://127.0.0.1:11211/",
    ):
        with pytest.raises(UnsafeUrl):
            check_url(url)


def test_credentials_in_the_url_are_refused():
    """`https://example.com@10.0.0.1/` is example.com to a careless reader and
    10.0.0.1 to the socket. Refusing the form removes the disagreement."""
    with pytest.raises(UnsafeUrl):
        check_url("https://example.com@10.0.0.1/")
    with pytest.raises(UnsafeUrl):
        check_url("https://user:pass@example.com/")


def test_odd_ports_are_refused():
    """A site worth scanning is on a web port. Anything else is somebody
    pointing us at a database or an admin panel."""
    for url in ("http://example.com:6379/", "http://example.com:22/",
                "http://example.com:11211/"):
        with pytest.raises(UnsafeUrl):
            check_url(url)


def test_internal_names_are_refused_without_resolving_them():
    for url in ("http://router.local/", "http://db.internal/",
                "http://wiki.corp/", "http://thing.home.arpa/"):
        with pytest.raises(UnsafeUrl):
            check_url(url)


def test_a_bare_hostname_is_refused():
    """A name with no dot is resolved through the machine's own search
    domains, which is how `http://admin/` reaches something real."""
    with pytest.raises(UnsafeUrl):
        check_url("http://admin/")


def test_a_host_that_resolves_to_a_private_address_is_refused(monkeypatch):
    """The DNS answer is the attack: the name is public, the address is not."""
    monkeypatch.setattr(
        "api.crawler.safety._resolve", lambda host, port: ["10.1.2.3"]
    )
    with pytest.raises(UnsafeUrl, match="private"):
        check_url("https://totally-fine.example.com/")


def test_one_private_answer_among_public_ones_is_still_refused(monkeypatch):
    """Round-robin DNS with a private answer in the set is a coin flip, and
    the coin only has to land once."""
    monkeypatch.setattr(
        "api.crawler.safety._resolve",
        lambda host, port: ["93.184.216.34", "127.0.0.1"],
    )
    with pytest.raises(UnsafeUrl):
        check_url("https://mixed.example.com/")


def test_a_host_that_does_not_resolve_is_refused(monkeypatch):
    import socket as socket_module

    def boom(host, port, **kwargs):
        raise socket_module.gaierror("nope")

    monkeypatch.setattr("api.crawler.safety.socket.getaddrinfo", boom)
    with pytest.raises(UnsafeUrl, match="resolve"):
        check_url("https://nowhere.example.com/")


# -- what must still work ----------------------------------------------------
def test_an_ordinary_website_passes(monkeypatch):
    monkeypatch.setattr(
        "api.crawler.safety._resolve", lambda host, port: ["93.184.216.34"]
    )
    assert check_url("https://example.com/") == "https://example.com/"
    assert check_url("http://example.com:80/a?b=c") == "http://example.com:80/a?b=c"


# -- the second lie ----------------------------------------------------------
def test_the_connected_address_is_checked_too():
    """DNS can answer differently for the check and for the connection. This
    is the rebinding window, and it closes by looking at the socket."""
    check_peer("93.184.216.34")               # fine
    with pytest.raises(UnsafeUrl):
        check_peer("10.0.0.7")
    with pytest.raises(UnsafeUrl):
        check_peer("169.254.169.254")


def test_an_unknown_peer_fails_closed():
    """This runs for an unauthenticated stranger. Not knowing where the
    connection went is a reason to refuse, not a reason to continue."""
    with pytest.raises(UnsafeUrl):
        check_peer(None)
