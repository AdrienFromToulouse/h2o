"""The publish transaction: it lands whole, or it does not land.

ADR-005 §2 makes four claims about this code. Each is a test here, because each
is the kind of property that is true right up until someone reorders two lines
for readability:

  * steps before the write touch memory only, so a failure leaves nothing;
  * the gate runs on the post-change graph, where a collision exists;
  * the serialisation is deterministic, or "byte-identical" is unassertable;
  * a retry replays the draft rather than a diff.
"""

from __future__ import annotations

from typing import Any

import pyoxigraph
import pytest
from botocore.exceptions import ClientError
from fakes import FakeS3, FakeTable
from h2o_core import config, facts, gaps, graph, integrity, publish, sparql
from h2o_core.facts import Claim
from h2o_core.impact import ConceptDraft


@pytest.fixture(autouse=True)
def _gate_is_tested_elsewhere(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Stub the gate for the tests that are about the transaction.

    Whether SHACL and resolver parity catch a bad graph is test_integrity's
    question, and answering it costs ~11s of pySHACL per call. Paying that
    eighteen times to assert things about ETags and history graphs made `make
    check` take ten minutes, which is a real cost paid for no extra coverage.

    Tests that genuinely need the gate ask for it with `@pytest.mark.real_gate`.
    """
    if request.node.get_closest_marker("real_gate"):
        return
    monkeypatch.setattr(integrity, "validate", lambda *_, **__: [])


@pytest.fixture
def audit() -> FakeTable:
    return FakeTable(hash_key="concept_id", range_key="published_at")


@pytest.fixture
def queue() -> FakeTable:
    return FakeTable(hash_key="gap_id")


def draft(**overrides: Any) -> ConceptDraft:
    """The demo's edit: CO₂ Cylinder gains "gas bottle" as an alternative term."""
    base = {
        "concept_id": "co2-cylinder",
        "pref_label": "CO₂ Cylinder",
        "alt_labels": ["Carbonation Cylinder", "Sparkling Cylinder", "gas bottle"],
        "definition": "A pressurised cylinder supplying carbon dioxide to the carbonator.",
        "scheme_id": "equipment",
        "broader": "component",
        "change_note": "Technicians call it a gas bottle in the field.",
    }
    return ConceptDraft(**{**base, **overrides})


def run(s3: FakeS3, audit: FakeTable, queue: FakeTable, **kwargs: Any) -> publish.PublishResult:
    return publish.publish(
        kwargs.pop("draft", None) or draft(),
        client=s3,
        audit_table=audit,
        gaps_table=queue,
        **kwargs,
    )


# ------------------------------------------------------------------ atomicity


def test_publishing_is_exactly_one_write(
    seeded_s3: FakeS3, audit: FakeTable, queue: FakeTable
) -> None:
    """Atomicity expressed as a count. Two writes would mean a window where the
    dataset held half a publish, and no amount of ordering care would close it."""
    run(seeded_s3, audit, queue)

    writes = [call for call in seeded_s3.put_calls if call["Key"] == config.GRAPH_KEY]
    assert len(writes) == 1
    assert "IfMatch" in writes[0]


def test_the_write_is_conditional_on_what_was_read(
    seeded_s3: FakeS3, audit: FakeTable, queue: FakeTable
) -> None:
    before = seeded_s3.etags[config.GRAPH_KEY]

    run(seeded_s3, audit, queue)

    assert [c for c in seeded_s3.put_calls if c["Key"] == config.GRAPH_KEY][0]["IfMatch"] == before


@pytest.mark.real_gate
def test_a_refused_publish_leaves_the_dataset_byte_identical(
    seeded_s3: FakeS3, audit: FakeTable, queue: FakeTable
) -> None:
    """Steps 3 to 5 mutate a scratch copy that came from S3 and goes back once.
    A gate failure discards it, and nothing was written to discard."""
    before = seeded_s3.objects[config.GRAPH_KEY]

    with pytest.raises(publish.IntegrityError):
        run(seeded_s3, audit, queue, draft=draft(alt_labels=["Filter Cartridge"]))

    assert seeded_s3.objects[config.GRAPH_KEY] == before
    assert not [c for c in seeded_s3.put_calls if c["Key"] == config.GRAPH_KEY]
    assert audit.writes == [], "and nothing claims a publish happened"


@pytest.mark.parametrize("failing_step", ["validate", "dump", "put"])
def test_a_failure_at_any_step_before_the_write_changes_nothing(
    seeded_s3: FakeS3,
    audit: FakeTable,
    queue: FakeTable,
    monkeypatch: pytest.MonkeyPatch,
    failing_step: str,
) -> None:
    """Parametrised over the three injection points, because "nothing was
    written" has to hold wherever the failure lands, not just where it was
    convenient to test."""
    before = seeded_s3.objects[config.GRAPH_KEY]

    def explode(*_: Any, **__: Any) -> Any:
        raise RuntimeError("the machine caught fire")

    target = {"validate": integrity, "dump": graph, "put": graph}[failing_step]
    name = {"validate": "validate", "dump": "dump", "put": "put"}[failing_step]
    monkeypatch.setattr(target, name, explode)

    with pytest.raises(RuntimeError):
        run(seeded_s3, audit, queue)

    assert seeded_s3.objects[config.GRAPH_KEY] == before


# ----------------------------------------------------------------- the gate


@pytest.mark.real_gate
def test_the_gate_sees_a_collision_the_draft_introduces(
    seeded_s3: FakeS3, audit: FakeTable, queue: FakeTable
) -> None:
    """Run before the change, this passes: nothing collides yet. That is the
    whole reason it runs after."""
    with pytest.raises(publish.IntegrityError) as raised:
        run(seeded_s3, audit, queue, draft=draft(alt_labels=["Filter Cartridge"]))

    assert raised.value.findings
    assert "Filter Cartridge" in raised.value.findings[0].message


@pytest.mark.real_gate
def test_the_refusal_carries_sentences_and_not_codes(
    seeded_s3: FakeS3, audit: FakeTable, queue: FakeTable
) -> None:
    """ADR-006 renders these verbatim to a domain expert."""
    with pytest.raises(publish.IntegrityError) as raised:
        run(seeded_s3, audit, queue, draft=draft(alt_labels=["Filter Cartridge"]))

    for finding in raised.value.findings:
        for jargon in ("skos:", "sh:", "SELECT", "http://"):
            assert jargon not in finding.message


# ------------------------------------------------------------------ versions


def test_the_prior_version_freezes_into_history(
    seeded_s3: FakeS3, audit: FakeTable, queue: FakeTable
) -> None:
    """ADR-005: concepts are never hard-deleted, and every publish leaves the
    version it replaced readable."""
    result = run(seeded_s3, audit, queue)

    assert result.version == 2
    assert result.history_graph == "h2o:graph/history/co2-cylinder/1"

    republished = graph.load(client=seeded_s3).store
    frozen = list(
        republished.quads_for_pattern(None, None, None, pyoxigraph.NamedNode(result.history_graph))
    )
    assert frozen, "the outgoing version is readable after the publish"


def test_the_new_version_is_the_one_in_the_published_graph(
    seeded_s3: FakeS3, audit: FakeTable, queue: FakeTable
) -> None:
    run(seeded_s3, audit, queue)

    reloaded = graph.load(client=seeded_s3).store
    labels = {
        str(quad.object.value)
        for quad in reloaded.quads_for_pattern(
            pyoxigraph.NamedNode("https://vocab.h2o.example/id/co2-cylinder"),
            pyoxigraph.NamedNode("http://www.w3.org/2004/02/skos/core#altLabel"),
            None,
            pyoxigraph.NamedNode(config.PUBLISHED_GRAPH),
        )
    }
    assert "gas bottle" in labels
    assert publish.current_version(reloaded, "co2-cylinder") == 2


def test_publishing_twice_makes_a_second_version_not_a_merge(
    seeded_s3: FakeS3, audit: FakeTable, queue: FakeTable
) -> None:
    first = run(seeded_s3, audit, queue)
    second = run(seeded_s3, audit, queue, draft=draft(change_note="And again."))

    assert (first.version, second.version) == (2, 3)
    assert second.history_graph == "h2o:graph/history/co2-cylinder/2"


# ------------------------------------------------------------ the lost update


def test_a_lost_update_is_refused_at_s3_and_then_replayed(
    store: pyoxigraph.Store, audit: FakeTable, queue: FakeTable, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of this test is *where* the refusal happens.

    A publish that noticed the clash client-side and backed off would pass a
    weaker version of this test while proving nothing about the conditional
    PUT. So the write is allowed to reach S3, which refuses it with a real 412,
    and what is asserted is that the attempt was made, refused there, and then
    replayed against the graph that won.
    """
    ours = FakeS3({config.GRAPH_KEY: graph.dump(store)})

    # Another curator's publish: the same vocabulary carrying one more note, so
    # the bytes differ, the ETag moves, and the graph is still valid.
    store.add(
        pyoxigraph.Quad(
            pyoxigraph.NamedNode("https://vocab.h2o.example/id/dispenser"),
            pyoxigraph.NamedNode("http://www.w3.org/2004/02/skos/core#scopeNote"),
            pyoxigraph.Literal("Edited by somebody else.", language="en"),
            pyoxigraph.NamedNode(config.PUBLISHED_GRAPH),
        )
    )
    theirs = graph.dump(store)

    raced: list[bool] = []
    real_put = ours.put_object

    def lands_first(**kwargs: Any) -> Any:
        if kwargs["Key"] == config.GRAPH_KEY and not raced:
            raced.append(True)
            real_put(Bucket=kwargs["Bucket"], Key=config.GRAPH_KEY, Body=theirs)
        return real_put(**kwargs)

    monkeypatch.setattr(ours, "put_object", lands_first)

    result = publish.publish(draft(), client=ours, audit_table=audit, gaps_table=queue)

    assert result.attempts == 2, "the first attempt lost and the draft was replayed"

    conditional = [c for c in ours.put_calls if c["Key"] == config.GRAPH_KEY and "IfMatch" in c]
    assert len(conditional) == 2, "both attempts reached S3 rather than being pre-empted"

    # The winner's edit survived, and ours landed on top of it.
    reloaded = graph.load(client=ours).store
    notes = list(
        reloaded.quads_for_pattern(
            pyoxigraph.NamedNode("https://vocab.h2o.example/id/dispenser"),
            pyoxigraph.NamedNode("http://www.w3.org/2004/02/skos/core#scopeNote"),
            None,
            pyoxigraph.NamedNode(config.PUBLISHED_GRAPH),
        )
    )
    assert notes, "the publish we lost to was not clobbered by the replay"
    assert publish.current_version(reloaded, "co2-cylinder") == 2


def test_the_fake_refuses_a_stale_etag_the_way_s3_does(seeded_s3: FakeS3) -> None:
    """Guarding the guard: if the fake accepted a stale If-Match, every test
    above would pass while the real lost update went through."""
    with pytest.raises(ClientError) as refused:
        seeded_s3.put_object(Bucket="b", Key=config.GRAPH_KEY, Body=b"x", IfMatch='"stale"')

    assert refused.value.response["Error"]["Code"] == "PreconditionFailed"


def test_a_publish_that_keeps_losing_gives_up_loudly(
    seeded_s3: FakeS3, audit: FakeTable, queue: FakeTable, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retrying forever would turn a contended concept into a hung request."""

    def always_refused(*_: Any, **__: Any) -> str:
        raise graph.ConcurrentPublishError("somebody else got there first")

    monkeypatch.setattr(graph, "put", always_refused)

    with pytest.raises(graph.ConcurrentPublishError, match="lost"):
        run(seeded_s3, audit, queue)


# ----------------------------------------------------- determinism and record


def test_the_same_publish_twice_produces_the_same_bytes(
    store: pyoxigraph.Store, audit: FakeTable, queue: FakeTable, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pyoxigraph does not guarantee dump ordering, so graph.dump sorts.

    Without that, "a mid-publish failure leaves the dataset byte-identical"
    would not be assertable at all -- two dumps of one graph could differ and
    every comparison in this file would be measuring the serialiser's mood.
    Time is pinned because a timestamp is the one thing in a publish that is
    genuinely meant to differ.
    """
    monkeypatch.setattr(publish, "_now", lambda: "2026-08-11T09:00:00+00:00")
    seeded = graph.dump(store)

    outcomes = []
    for _ in range(2):
        s3 = FakeS3({config.GRAPH_KEY: seeded})
        result = publish.publish(draft(), client=s3, audit_table=audit, gaps_table=queue)
        outcomes.append((s3.objects[config.GRAPH_KEY], result.watermark))

    assert outcomes[0][0] == outcomes[1][0], "the same publish produced different bytes"
    assert outcomes[0][1] == outcomes[1][1], "and therefore a different watermark"
    assert outcomes[0][1] == graph.digest(outcomes[0][0])


def test_the_audit_row_is_written_after_the_durable_write(
    seeded_s3: FakeS3, audit: FakeTable, queue: FakeTable
) -> None:
    """ADR-005 §1: who, when, which concept, and the expert's own reason.

    Written last, so a lost race cannot leave a row describing a change that
    never landed.
    """
    result = run(seeded_s3, audit, queue)

    rows = audit.all_items()
    assert len(rows) == 1
    assert rows[0]["concept_id"] == "co2-cylinder"
    assert rows[0]["version"] == 2
    assert rows[0]["change_note"] == "Technicians call it a gas bottle in the field."
    assert rows[0]["run_id"] == result.publish_id


def test_the_result_names_the_gaps_this_publish_answers(
    seeded_s3: FakeS3, audit: FakeTable, queue: FakeTable
) -> None:
    """Read before the fan-out closes them, so the run record can say what was
    closed even if a later step fails."""
    gaps.record_miss(
        "gas bottle",
        source=gaps.GapSource.ingestion,
        evidence=gaps.GapEvidence(
            source=gaps.GapSource.ingestion,
            text="Store the gas bottle upright.",
            locator="02-service-bulletin.md:4-4",
            occurred_at="2026-08-11T09:00:00",
        ),
        table_resource=queue,
    )

    result = run(seeded_s3, audit, queue)

    assert "gas bottle" in result.surfaces
    assert result.closes == ["gas bottle"]
    # Named, not closed. Fan-out step 2 moves the status, because the queue
    # closes when a publish's consequences run and not when it is recorded.
    assert gaps.read_gap("gas bottle", table_resource=queue).status is gaps.GapStatus.open


def test_held_claims_are_untouched_by_the_publish_itself(
    store: pyoxigraph.Store, audit: FakeTable, queue: FakeTable
) -> None:
    """Publishing changes the vocabulary. Re-resolving the claims it unblocks is
    fan-out step 2, and keeping them separate is what makes a failed re-resolve
    retryable without republishing."""
    facts.insert(
        store,
        [
            Claim(
                subject_concept=None,
                subject_surface="gas bottle",
                predicate="stored",
                value="upright",
                source_file="02-service-bulletin.md",
                doc_version="v1",
                line_range="4-4",
                snippet="Store the gas bottle upright.",
            )
        ],
    )
    s3 = FakeS3({config.GRAPH_KEY: graph.dump(store)})

    publish.publish(draft(), client=s3, audit_table=audit, gaps_table=queue)

    reloaded = graph.load(client=s3).store
    still_held = graph.records(
        reloaded, sparql.render("facts_held_for_surface.rq", surface=sparql.Lit("gas bottle"))
    )
    assert len(still_held) == 1
