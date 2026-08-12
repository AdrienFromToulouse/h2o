"""The graph store: named graphs survive a round trip, and writes are conditional."""

import pyoxigraph
import pytest
from fakes import FakeS3, load_vocabulary
from h2o_core import config, graph
from h2o_core.graph import ConcurrentPublishError


def test_the_seed_vocabulary_loads(store: pyoxigraph.Store) -> None:
    assert len(store) == 870


def test_the_dump_is_byte_reproducible() -> None:
    """ADR-005 requires asserting a failed publish leaves the graph
    byte-identical, and pyoxigraph guarantees no dump ordering. Without sorting,
    "byte-identical" is unassertable and a retry gets a spurious diff."""
    sources = load_vocabulary()
    first = graph.dump(graph.store_from_turtle(sources, config.PUBLISHED_GRAPH))
    second = graph.dump(graph.store_from_turtle(sources, config.PUBLISHED_GRAPH))

    assert first == second
    assert graph.digest(first) == graph.digest(second)


def test_named_graphs_survive_the_round_trip(seeded_s3: FakeS3) -> None:
    """N-Quads, not Turtle: the four graphs of ADR-005 are the whole point, and
    the publish transaction's first act is copying a concept between two."""
    snapshot = graph.load(client=seeded_s3)

    graphs = {str(q.graph_name.value) for q in snapshot.store}
    assert graphs == {config.PUBLISHED_GRAPH}
    assert len(snapshot.store) == 870
    assert snapshot.etag is not None


def test_a_missing_object_is_an_empty_store_not_an_error() -> None:
    """Bootstrap. An empty bucket is a starting state, not a failure."""
    snapshot = graph.load(client=FakeS3())

    assert len(snapshot.store) == 0
    assert snapshot.etag is None


def test_a_stale_etag_is_refused(seeded_s3: FakeS3) -> None:
    """The lost update ADR-007 names as this design's one real hazard.

    The write must reach S3 and be refused *there*. A client-side check would
    pass this test and still lose an update against the real service.
    """
    first = graph.load(client=seeded_s3)
    graph.put(b"<a> <b> <c> <d> .\n", first.etag, client=seeded_s3)

    before = seeded_s3.objects[config.GRAPH_KEY]
    attempts = len(seeded_s3.put_calls)

    with pytest.raises(ConcurrentPublishError):
        graph.put(b"clobbered", first.etag, client=seeded_s3)

    assert seeded_s3.objects[config.GRAPH_KEY] == before
    assert len(seeded_s3.put_calls) == attempts + 1, "the refused write never reached S3"


def test_bootstrap_uses_if_none_match() -> None:
    """Two simultaneous first writes must not both succeed."""
    fake = FakeS3()
    graph.put(b"first", None, client=fake)

    with pytest.raises(ConcurrentPublishError):
        graph.put(b"second", None, client=fake)

    assert fake.objects[config.GRAPH_KEY] == b"first"
    assert fake.put_calls[-1]["IfNoneMatch"] == "*"


def test_the_warm_cache_is_bounded_and_droppable(seeded_s3: FakeS3) -> None:
    """A Lambda container serves many requests; reloading per request would pay
    S3 latency for a graph that changes only when a curator publishes."""
    graph.cached(client=seeded_s3)
    graph.cached(client=seeded_s3)
    assert len(seeded_s3.get_calls) == 1

    graph.forget_cached()
    graph.cached(client=seeded_s3)
    assert len(seeded_s3.get_calls) == 2


def test_a_malformed_turtle_file_names_itself() -> None:
    """Seeding reads seven files; "invalid syntax" without a filename is a
    twenty-minute bisect."""
    with pytest.raises(ValueError, match="broken.ttl"):
        graph.store_from_turtle({"broken.ttl": b"@prefix oops"}, config.PUBLISHED_GRAPH)
