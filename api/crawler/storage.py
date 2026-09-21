"""Raw artifact storage.

Fetched HTML never enters Postgres. A 500-page site crawled weekly is millions
of rows of text nobody queries; object storage is the system of record for
immutable raw artifacts and Postgres stores only the key.

The key layout is deliberate: {website_id}/{crawl_id}/{url_hash}.html.gz makes
a crawl's artifacts deletable as a prefix, which is what a retention policy and
a deletion request both need.
"""

from __future__ import annotations

import gzip
from pathlib import Path
from typing import Protocol
from uuid import UUID


def artifact_key(website_id: UUID, crawl_id: UUID, url_hash: bytes) -> str:
    return f"{website_id}/{crawl_id}/{url_hash.hex()}.html.gz"


class ArtifactStore(Protocol):
    async def put(self, key: str, body: bytes) -> str: ...

    async def get(self, key: str) -> bytes | None: ...


class LocalArtifactStore:
    """Filesystem-backed, for development and tests.

    The S3 implementation replaces this class and nothing else: the crawler
    only knows `put` and `get`.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self._root / key).resolve()
        # Keys are built from ids and hashes, but a store that can be talked
        # out of its own directory is a store that can be talked into writing
        # anywhere.
        if not str(path).startswith(str(self._root.resolve())):
            raise ValueError("artifact key escapes the store root")
        return path

    async def put(self, key: str, body: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(body))
        return key

    async def get(self, key: str) -> bytes | None:
        path = self._path(key)
        if not path.exists():
            return None
        return gzip.decompress(path.read_bytes())


class NullArtifactStore:
    """Records keys without storing bytes. For tests that do not care."""

    def __init__(self) -> None:
        self.keys: list[str] = []

    async def put(self, key: str, body: bytes) -> str:
        self.keys.append(key)
        return key

    async def get(self, key: str) -> bytes | None:
        return None
