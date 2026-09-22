"""Webhook authentication and push filtering."""

from __future__ import annotations

import hashlib
import hmac
from types import SimpleNamespace

import pytest

from forge.domain.errors import Unauthorized
from forge.routers.webhooks import _handle_push, _verify_signature

SECRET = "a-project-webhook-secret"


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class TestSignature:
    def test_a_correctly_signed_body_is_accepted(self):
        body = b'{"ref":"refs/heads/main"}'
        _verify_signature(body, sign(body), SECRET)

    def test_a_body_altered_after_signing_is_rejected(self):
        body = b'{"ref":"refs/heads/main"}'
        header = sign(body)
        with pytest.raises(Unauthorized):
            _verify_signature(body + b" ", header, SECRET)

    def test_another_projects_secret_does_not_work(self):
        """Secrets are per project so that rotating or leaking one says
        nothing about any other."""
        body = b"{}"
        with pytest.raises(Unauthorized):
            _verify_signature(body, sign(body, "a-different-secret"), SECRET)

    def test_an_unsigned_request_is_refused_with_advice(self):
        with pytest.raises(Unauthorized) as exc:
            _verify_signature(b"{}", None, SECRET)
        assert "secret" in str(exc.value).lower()

    def test_an_unsupported_algorithm_is_refused(self):
        with pytest.raises(Unauthorized):
            _verify_signature(b"{}", "sha1=abc123", SECRET)


class TestPushFiltering:
    """Not every push event is a deploy, and the ones that are not would
    otherwise queue a build of a commit that does not exist."""

    @pytest.fixture
    def queued(self, monkeypatch):
        """Record what would have been queued, without a database."""
        calls = []

        async def fake_queue(project, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(short_id="blog-abc12345", number=7)

        monkeypatch.setattr("forge.routers.webhooks.service.queue_deploy", fake_queue)
        # The response serialiser needs a real Deployment; what is under test
        # here is which pushes get queued, not how they are rendered.
        monkeypatch.setattr("forge.routers.webhooks._out", lambda *a, **k: None)
        return calls

    @pytest.fixture
    def project(self):
        class P:
            slug = "blog"

        return P()

    async def test_a_tag_push_is_ignored(self, project, queued):
        result = await _handle_push(
            project, {"ref": "refs/tags/v1.0.0", "after": "a" * 40}, None
        )
        assert result.ignored and "not a branch" in result.ignored
        assert queued == []

    async def test_a_deleted_branch_is_ignored(self, project, queued):
        result = await _handle_push(
            project,
            {"ref": "refs/heads/feature", "deleted": True, "after": "0" * 40},
            None,
        )
        assert result.ignored and "deleted" in result.ignored
        assert queued == []

    async def test_the_all_zero_sha_is_ignored_even_without_the_deleted_flag(
        self, project, queued
    ):
        """Not every git host sets `deleted`, but they all send this sha for
        a removed ref — and building it would fail at clone time."""
        result = await _handle_push(
            project, {"ref": "refs/heads/gone", "after": "0" * 40}, None
        )
        assert result.ignored and "no commit" in result.ignored
        assert queued == []

    async def test_an_ordinary_push_queues_the_exact_commit(self, project, queued):
        await _handle_push(
            project,
            {
                "ref": "refs/heads/main",
                "after": "c" * 40,
                "head_commit": {
                    "message": "Fix the thing\n\nlonger body",
                    "author": {"name": "Ada"},
                },
            },
            None,
        )
        assert len(queued) == 1
        assert queued[0]["ref"] == "main"
        assert queued[0]["sha"] == "c" * 40
        # Only the subject line, so a list view stays a list.
        assert queued[0]["message"] == "Fix the thing"
        assert queued[0]["author"] == "Ada"
