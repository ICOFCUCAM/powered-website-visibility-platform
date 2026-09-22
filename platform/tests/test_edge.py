"""The router files that promotion writes."""

from __future__ import annotations

import json

from forge.adapters.edge import ProductionRoute, clear_route, config_path, write_route


def route(**kwargs) -> ProductionRoute:
    base = {
        "project_slug": "blog",
        "service": "blog-abc12345",
        "primary_host": "example.com",
    }
    base.update(kwargs)
    return ProductionRoute(**base)


def read(directory, slug="blog") -> dict:
    return json.loads(config_path(directory, slug).read_text())


def test_a_route_points_at_the_deployments_docker_service(tmp_path):
    """The file provider names a service the Docker provider created, which
    is what lets promotion happen without touching the container."""
    write_route(route(), directory=tmp_path, cert_resolver="le")
    routers = read(tmp_path)["http"]["routers"]
    assert routers["blog-primary"]["service"] == "blog-abc12345@docker"
    assert routers["blog-primary"]["rule"] == "Host(`example.com`)"


def test_production_outranks_the_deployments_own_wildcard_route(tmp_path):
    """Both are plain Host() matches of similar length, and Traefik's
    tie-break is not something production routing should depend on."""
    write_route(route(), directory=tmp_path, cert_resolver="le")
    assert read(tmp_path)["http"]["routers"]["blog-primary"]["priority"] == 100


def test_aliases_redirect_to_the_primary_rather_than_serving_it_twice(tmp_path):
    write_route(
        route(aliases=("www.example.com", "example.net")),
        directory=tmp_path,
        cert_resolver="le",
    )
    document = read(tmp_path)["http"]
    aliases = document["routers"]["blog-aliases"]
    assert "Host(`example.net`)" in aliases["rule"]
    assert "Host(`www.example.com`)" in aliases["rule"]

    middleware = document["middlewares"]["blog-canonical"]["redirectRegex"]
    assert middleware["replacement"] == "https://example.com${1}"
    assert middleware["permanent"] is True


def test_the_redirect_preserves_the_path(tmp_path):
    write_route(
        route(aliases=("www.example.com",)), directory=tmp_path, cert_resolver="le"
    )
    rule = read(tmp_path)["http"]["middlewares"]["blog-canonical"]["redirectRegex"]
    import re

    match = re.match(rule["regex"], "https://www.example.com/pricing?x=1")
    assert match is not None
    assert match.group(1) == "/pricing?x=1"


def test_tls_is_omitted_entirely_when_no_resolver_is_configured(tmp_path):
    """Local development runs on plain HTTP; asking for a certificate there
    would fail every reload."""
    write_route(route(), directory=tmp_path, cert_resolver="")
    router = read(tmp_path)["http"]["routers"]["blog-primary"]
    assert "tls" not in router
    assert router["entryPoints"] == ["web"]


def test_writing_leaves_no_temporary_file_behind(tmp_path):
    """Traefik watches this directory. A stray .tmp would be parsed as config."""
    write_route(route(), directory=tmp_path, cert_resolver="le")
    assert [p.name for p in tmp_path.iterdir()] == ["project-blog.json"]


def test_clearing_a_route_is_safe_when_there_is_nothing_to_clear(tmp_path):
    clear_route("never-existed", directory=tmp_path)
    write_route(route(), directory=tmp_path, cert_resolver="le")
    clear_route("blog", directory=tmp_path)
    assert list(tmp_path.iterdir()) == []
