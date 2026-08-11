"""Restating claims: the operation that makes the demonstrator repeatable.

`make demo-reset` exists because the loop is otherwise one-shot -- once "gas
bottle" is published the gap is closed and the twelve mentions are live, and the
one claim the demo makes ("no code change, no redeploy") cannot be shown twice.

The reset recomputes rather than undoes, and this file asserts the property that
makes that safe: after restating, every claim's status agrees with what the
vocabulary actually says, whichever direction it moved.
"""

from __future__ import annotations

import pyoxigraph
import pytest
from h2o_core import config, facts, fanout, graph, resolver, sparql
from h2o_core.facts import Claim

PUBLISHED = "h2o:graph/published"
SKOS = "http://www.w3.org/2004/02/skos/core#"


def claim(surface: str, line: int, source_file: str = "02-service-bulletin.md") -> Claim:
    """One claim, from one line.

    The line matters: a claim's IRI is a content hash of its evidence -- source
    file, version, line range, predicate, value -- and *not* of its subject. Two
    claims quoting the same line are the same claim, which is what makes
    re-ingestion idempotent, and is why a test needs distinct lines to have
    distinct claims.
    """
    return Claim(
        subject_concept=None,
        subject_surface=surface,
        predicate="stored",
        value="upright",
        source_file=source_file,
        doc_version="v1",
        line_range=f"{line}-{line}",
        snippet=f"Store the {surface} upright.",
    )


def add_alt_label(store: pyoxigraph.Store, concept_id: str, text: str) -> None:
    store.add(
        pyoxigraph.Quad(
            pyoxigraph.NamedNode(f"https://vocab.h2o.example/id/{concept_id}"),
            pyoxigraph.NamedNode(f"{SKOS}altLabel"),
            pyoxigraph.Literal(text, language="en"),
            pyoxigraph.NamedNode(PUBLISHED),
        )
    )


def held_count(store: pyoxigraph.Store, surface: str) -> int:
    return len(
        graph.records(
            store, sparql.render("facts_held_for_surface.rq", surface=sparql.Lit(surface))
        )
    )


@pytest.fixture
def with_held_claims(store: pyoxigraph.Store) -> pyoxigraph.Store:
    facts.insert(store, [claim("gas bottle", n) for n in (4, 9, 21)] + [claim("Carbon Filter", 30)])
    return store


def test_a_term_the_vocabulary_gained_goes_live(with_held_claims: pyoxigraph.Store) -> None:
    add_alt_label(with_held_claims, "co2-cylinder", "gas bottle")
    index = resolver.build(with_held_claims, watermark="test")

    counts = fanout.restate_claims(with_held_claims, index)

    assert held_count(with_held_claims, "gas bottle") == 0
    assert counts["held"] == 0


def test_a_term_the_vocabulary_lost_goes_back_to_held(
    with_held_claims: pyoxigraph.Store,
) -> None:
    """The direction the fan-out never goes.

    A publish only adds resolution, so step 2 promotes and never demotes -- and
    that is right, because publishing a label cannot invalidate a link. The
    reset takes labels away, so it needs the reverse, or the graph would keep
    asserting a link nothing supports.
    """
    add_alt_label(with_held_claims, "co2-cylinder", "gas bottle")
    fanout.restate_claims(with_held_claims, resolver.build(with_held_claims, watermark="a"))
    assert held_count(with_held_claims, "gas bottle") == 0

    # The reset: the vocabulary goes back to what git holds, and the facts come
    # across untouched -- which is the whole shape of scripts/demo_reset.py.
    reverted = pyoxigraph.Store()
    for quad in with_held_claims:
        if str(getattr(quad.graph_name, "value", "")) != PUBLISHED:
            reverted.add(quad)
    for quad in with_held_claims.quads_for_pattern(
        None, None, None, pyoxigraph.NamedNode(PUBLISHED)
    ):
        if not (
            str(quad.predicate.value).endswith("altLabel")
            and getattr(quad.object, "value", "") == "gas bottle"
        ):
            reverted.add(quad)

    fanout.restate_claims(reverted, resolver.build(reverted, watermark="b"))

    assert held_count(reverted, "gas bottle") == 3, "the claims came back to the queue"


def test_restating_twice_changes_nothing(with_held_claims: pyoxigraph.Store) -> None:
    """The reset has to be runnable after a demo that half-happened, so it is
    idempotent rather than a diff against an assumed starting state."""
    index = resolver.build(with_held_claims, watermark="test")

    fanout.restate_claims(with_held_claims, index)
    once = graph.dump(with_held_claims)
    fanout.restate_claims(with_held_claims, index)

    assert graph.dump(with_held_claims) == once


def test_a_claim_never_loses_its_evidence(with_held_claims: pyoxigraph.Store) -> None:
    """Restating changes what a claim is *about*, never what it says. The
    snippet, the file and the line range are the document's, and no vocabulary
    decision can alter them."""
    before = {
        str(quad.subject.value): str(quad.object.value)
        for quad in with_held_claims.quads_for_pattern(
            None,
            pyoxigraph.NamedNode("https://vocab.h2o.example/id/snippet"),
            None,
            pyoxigraph.NamedNode(config.FACTS_GRAPH),
        )
    }
    add_alt_label(with_held_claims, "co2-cylinder", "gas bottle")

    fanout.restate_claims(with_held_claims, resolver.build(with_held_claims, watermark="test"))

    after = {
        str(quad.subject.value): str(quad.object.value)
        for quad in with_held_claims.quads_for_pattern(
            None,
            pyoxigraph.NamedNode("https://vocab.h2o.example/id/snippet"),
            None,
            pyoxigraph.NamedNode(config.FACTS_GRAPH),
        )
    }
    assert before == after and before


def test_an_exact_label_stays_resolved(with_held_claims: pyoxigraph.Store) -> None:
    """ "Carbon Filter" is a real prefLabel, so that claim was never held and
    must not become held by being restated."""
    counts = fanout.restate_claims(
        with_held_claims, resolver.build(with_held_claims, watermark="test")
    )

    assert counts["active"] == 1
    assert held_count(with_held_claims, "gas bottle") == 3
