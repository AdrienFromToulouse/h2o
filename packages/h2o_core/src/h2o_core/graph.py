"""The graph store: pyoxigraph in-process over one N-Quads object in S3.

ADR-007's runtime. The useful mental model is SQLite for RDF -- an engine you
import, not a server you connect to -- and the whole dataset, all four named
graphs, is a single S3 object.

Two properties everything else depends on:

**A loaded snapshot is a scratch copy.** Queries and updates run against memory.
Durability is exactly one ``PutObject``, so a failure anywhere before that call
cannot leave a partially-written graph, on any backend.

**The dump is sorted.** pyoxigraph makes no ordering guarantee, and ADR-005
requires a test asserting that a mid-publish failure leaves the published graph
byte-identical. Unsorted output would make "byte-identical" unassertable and
would give a retry a spurious diff.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import pyoxigraph
from botocore.exceptions import ClientError

from h2o_core import config

__all__ = [
    "ConcurrentPublishError",
    "GraphSnapshot",
    "dump",
    "load",
    "put",
    "s3",
    "store_from_turtle",
]


class ConcurrentPublishError(RuntimeError):
    """Another writer changed the dataset while this one was preparing a write."""


_s3: Any = None


def s3() -> Any:
    global _s3
    if _s3 is None:
        import boto3

        _s3 = boto3.client("s3", region_name=config.AWS_REGION)
    return _s3


@dataclass(frozen=True)
class GraphSnapshot:
    """A loaded dataset and the ETag it was loaded at.

    The ETag is the concurrency token. ``None`` means the object did not exist,
    which is the bootstrap case and is written with ``If-None-Match: *`` so two
    simultaneous first writes cannot both succeed.
    """

    store: pyoxigraph.Store
    etag: str | None

    @property
    def quad_count(self) -> int:
        return len(self.store)


def load(*, client: Any = None) -> GraphSnapshot:
    """Read the dataset from S3 into a fresh in-memory store."""
    target = client or s3()
    try:
        response = target.get_object(Bucket=config.GRAPH_BUCKET, Key=config.GRAPH_KEY)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
            return GraphSnapshot(store=pyoxigraph.Store(), etag=None)
        raise

    store = pyoxigraph.Store()
    store.bulk_load(response["Body"].read(), format=pyoxigraph.RdfFormat.N_QUADS)
    return GraphSnapshot(store=store, etag=response.get("ETag"))


def dump(store: pyoxigraph.Store) -> bytes:
    """Serialise to N-Quads, sorted, so the same graph always produces the same bytes.

    N-Quads because the four named graphs of ADR-005 are the point: a
    triples-only format would need them emulated, and the publish transaction
    copies a concept between graphs as its first act.
    """
    serialised = store.dump(format=pyoxigraph.RdfFormat.N_QUADS)
    if serialised is None:  # pragma: no cover - only when dumping to a file object
        raise RuntimeError("dump() wrote to a sink instead of returning bytes")
    lines = sorted(line for line in serialised.decode("utf-8").splitlines() if line.strip())
    return ("\n".join(lines) + "\n").encode("utf-8") if lines else b""


def rows(store: pyoxigraph.Store, query: str) -> list[Any]:
    """Run a SELECT and return its solutions.

    ``Store.query`` is typed as a union across SELECT, ASK and CONSTRUCT, so the
    narrowing happens once here rather than at each of the dozen call sites that
    would otherwise each need the same cast.
    """
    result = store.query(query)
    if isinstance(result, pyoxigraph.QueryBoolean):
        raise TypeError("expected a SELECT query, got an ASK")
    return list(result)


def digest(payload: bytes) -> str:
    """Content hash of a dataset, used as the resolver index's watermark.

    Deriving the watermark from the bytes means a rebuild that changed nothing
    produces the same watermark, so staleness is diagnosed by comparing two
    strings rather than by guessing (ADR-005).
    """
    return hashlib.sha256(payload).hexdigest()


def put(payload: bytes, etag: str | None, *, client: Any = None) -> str:
    """Write the dataset back, but only if nobody else has since we read it.

    The conditional PUT is h2o's entire concurrency control (ADR-007). It makes
    a collision *safe*, not impossible: the loser is told, reloads, and replays
    its change. Losing silently would be a lost update, and at vocabulary scale
    that means a curator's published edit vanishing with a success toast.
    """
    target = client or s3()
    condition = {"IfMatch": etag} if etag else {"IfNoneMatch": "*"}
    try:
        response = target.put_object(
            Bucket=config.GRAPH_BUCKET,
            Key=config.GRAPH_KEY,
            Body=payload,
            ContentType="application/n-quads",
            **condition,
        )
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code")
        status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in {"PreconditionFailed", "ConditionalRequestConflict"} or status == 412:
            raise ConcurrentPublishError(
                "the dataset changed while this write was being prepared"
            ) from error
        raise
    return str(response.get("ETag", ""))


_cached: GraphSnapshot | None = None
_cached_at: float = 0.0


def cached(*, client: Any = None, ttl: float | None = None) -> GraphSnapshot:
    """The dataset, reloaded at most every ``ttl`` seconds.

    A Lambda container serves many requests, and reloading a few thousand quads
    on each one would pay S3 latency for a graph that changes only when a
    curator publishes. The staleness window is bounded and stated rather than
    unbounded and invisible, which is the same bargain the resolver index makes.

    Read paths use this. **The publish transaction must not**: it needs the ETag
    that is current right now, and a cached one would fail its conditional PUT
    on every warm invocation.
    """
    global _cached, _cached_at
    import time

    window = config.INDEX_TTL_SECONDS if ttl is None else ttl
    now = time.monotonic()
    if _cached is None or now - _cached_at > window:
        _cached = load(client=client)
        _cached_at = now
    return _cached


def forget_cached() -> None:
    """Drop the warm cache. Used by tests and immediately after a publish."""
    global _cached, _cached_at
    _cached, _cached_at = None, 0.0


def store_from_turtle(sources: dict[str, bytes], graph_name: str) -> pyoxigraph.Store:
    """Load Turtle files into one named graph.

    Seeding is symmetric across backends by design (ADR-007): the reviewed
    vocab/*.ttl files are the loaded artefact here and the bulk-loader's input on
    Neptune, with no serialisation step in between that could differ.
    """
    store = pyoxigraph.Store()
    target = pyoxigraph.NamedNode(graph_name)
    for name, content in sorted(sources.items()):
        try:
            store.bulk_load(content, format=pyoxigraph.RdfFormat.TURTLE, to_graph=target)
        except Exception as error:  # noqa: BLE001 - name the file that failed
            raise ValueError(f"{name}: {error}") from error
    return store
