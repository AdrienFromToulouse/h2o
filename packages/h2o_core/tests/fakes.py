"""Stand-ins for the AWS services h2o talks to. No test touches a real one.

A fake that is more permissive than the service it replaces is worse than no
fake at all: it makes a suite pass on code that fails in production. So each of
these reproduces the specific refusal the real service makes, and the tests that
matter are the ones asserting the refusal happens.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

REPO_ROOT = Path(__file__).resolve().parents[3]
VOCAB_DIR = REPO_ROOT / "vocab"
DOCS_DIR = REPO_ROOT / "data" / "docs"


def load_vocabulary() -> dict[str, bytes]:
    """The real seed vocabulary. Tests assert against what ships, not a fixture."""
    return {p.name: p.read_bytes() for p in sorted(VOCAB_DIR.glob("*.ttl"))}


def _client_error(code: str, status: int, operation: str) -> ClientError:
    """A real botocore ClientError, so production's `except ClientError` runs.

    Raising a bare Exception here would let code that never catches ClientError
    pass the test and then fail against S3.
    """
    return ClientError(
        {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        operation,
    )


class FakeS3:
    """S3 with the conditional-write semantics h2o's concurrency control needs.

    `If-Match` and `If-None-Match` are honoured and refused with a real 412,
    because ADR-007 makes the conditional PUT the only thing standing between two
    curators and a lost update. A fake that accepted every write would make the
    lost-update test pass while proving nothing.
    """

    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects: dict[str, bytes] = dict(objects or {})
        self.etags: dict[str, str] = {k: self._etag(v) for k, v in self.objects.items()}
        #: Every PutObject attempt, including refused ones. A test asserting a
        #: write reached S3 and was rejected *there* needs to see the attempt.
        self.put_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []

    @staticmethod
    def _etag(payload: bytes) -> str:
        return f'"{hashlib.md5(payload).hexdigest()}"'  # noqa: S324 - S3's own scheme

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        self.get_calls.append(Key)
        if Key not in self.objects:
            raise _client_error("NoSuchKey", 404, "GetObject")
        payload = self.objects[Key]
        return {"Body": _Body(payload), "ETag": self.etags[Key], "ContentLength": len(payload)}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **kwargs: Any) -> dict[str, Any]:  # noqa: N803
        self.put_calls.append({"Key": Key, "Body": Body, **kwargs})
        current = self.etags.get(Key)

        if (expected := kwargs.get("IfMatch")) is not None and current != expected:
            raise _client_error("PreconditionFailed", 412, "PutObject")
        if kwargs.get("IfNoneMatch") == "*" and Key in self.objects:
            raise _client_error("PreconditionFailed", 412, "PutObject")

        self.objects[Key] = Body
        self.etags[Key] = self._etag(Body)
        return {"ETag": self.etags[Key]}


class _Body:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload
