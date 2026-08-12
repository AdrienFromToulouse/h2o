"""The six ADR-002 steps, end to end over the real corpus.

Extraction is stubbed with a deterministic reader rather than a model, because
what this asserts is the *pipeline*: that unresolved mentions hold their claims,
that gaps carry evidence, and that contradictions are found across documents.
Whether Nova 2 Lite finds a given sentence is a separate question, and pinning
it here would make the suite fail when a model version rolls.

The invariants asserted are the ones the README promises and check_corpus.py
already proves against the raw files. If these two ever disagree, the library is
wrong.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pyoxigraph
import pytest
from fakes import FakeTable
from h2o_core import config, extraction, facts, gaps, graph, pipeline, resolver, sparql
from h2o_core.chunking import Chunk, SourceText
from h2o_core.registry import DocumentRecord, load_manifest

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCS = REPO_ROOT / "data" / "docs"
REGISTRY = json.loads((DOCS / "registry.json").read_text())

GAS_BOTTLE = re.compile(r"gas\s+bottles?", re.IGNORECASE)


def gaps_table() -> FakeTable:
    """The real table has no sort key: three sources land on one item (ADR-004)."""
    return FakeTable(hash_key="gap_id")


def _sentence_reader(chunk: Chunk, source: SourceText, **_: Any) -> extraction.Extraction:
    """A deterministic stand-in for the model.

    Emits one fact per sentence mentioning "gas bottle", plus the two seeded
    contradictions where their values appear. Every snippet is a real substring,
    so the verbatim gate is exercised for real rather than bypassed.
    """
    result = extraction.Extraction()

    # Scanned over the whole chunk, not line by line. 04-support-faq.md wraps
    # one mention as "the gas\nbottle yourself", and a line-based reader misses
    # it -- which is exactly the difference between 11 and the 12 the README
    # promises. A real extractor reads a passage, so the stub must too.
    for match in GAS_BOTTLE.finditer(chunk.text):
        window = chunk.text[max(0, match.start() - 60) : match.end() + 60].strip()
        result.facts.append(
            {
                "subject": " ".join(match.group(0).split()),
                "predicate": "mentioned in",
                # A constant, not the filename. Varying it by document would make
                # every pair of documents disagree about this made-up predicate
                # and manufacture a conflict the corpus does not contain -- the
                # stub lying, not the detector finding something.
                "value": "yes",
                "unit": None,
                "snippet": window,
                "confidence": 0.9,
                "source_file": chunk.source_file,
                "line_range": chunk.line_range,
            }
        )

    for line in chunk.text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        for entry in REGISTRY["seeded_contradictions"]:
            for claim in entry["claims"]:
                if claim["source"] == chunk.source_file and claim["value"] in stripped:
                    result.facts.append(
                        {
                            "subject": entry["subject"].replace("-", " "),
                            "predicate": entry["predicate"].replace("-", " "),
                            "value": claim["value"],
                            "unit": None,
                            "snippet": stripped,
                            "confidence": 0.9,
                            "source_file": chunk.source_file,
                            "line_range": chunk.line_range,
                        }
                    )
    return result


@pytest.fixture
def corpus() -> list[tuple[DocumentRecord, str]]:
    records = load_manifest(DOCS / "registry.json")
    return [(record, (DOCS / record.filename).read_text()) for record in records]


@pytest.fixture
def published(store: pyoxigraph.Store) -> pyoxigraph.Store:
    return store


@pytest.fixture
def index(published: pyoxigraph.Store) -> resolver.ResolverIndex:
    """A real index over the real vocabulary, with no vectors.

    No embedder, so the cascade runs exact-match then abstains. That is the
    right shape for this test: it isolates the pipeline from Titan's scores,
    and "gas bottle" abstains for the reason the demo depends on -- there is no
    such label -- rather than because a threshold happened to fall.
    """
    return resolver.build(published, watermark="test")


def test_the_corpus_ingests(
    corpus: list[tuple[DocumentRecord, str]], index: resolver.ResolverIndex
) -> None:
    store = pyoxigraph.Store()
    result = pipeline.ingest_corpus(
        corpus,
        store,
        index=index,
        extract=_sentence_reader,
        gaps_table=gaps_table(),
        write_vectors=False,
        run_id="test-run",
    )

    assert result.documents == 6
    assert result.chunks > 20
    assert result.facts_extracted > 0
    assert not result.rejections, "every stubbed snippet is a real substring"


def test_gas_bottle_holds_twelve_claims_across_three_documents(
    corpus: list[tuple[DocumentRecord, str]], index: resolver.ResolverIndex
) -> None:
    """The number the README promises and the impact preview will quote.

    check_corpus.py already asserts 12 mentions across 3 documents against the
    raw files. This asserts the *pipeline* produces 12 held claims from them, so
    the count the console shows is the count the corpus contains.
    """
    store = pyoxigraph.Store()
    table = gaps_table()

    pipeline.ingest_corpus(
        corpus,
        store,
        index=index,
        extract=_sentence_reader,
        gaps_table=table,
        write_vectors=False,
        run_id="test-run",
    )

    held = graph.records(
        store, sparql.render("facts_held_for_surface.rq", surface=sparql.Lit("gas bottle"))
    )

    assert len(held) == 12
    assert len({row["source_file"] for row in held}) == 3


def test_the_gap_queue_holds_one_entry_for_every_spelling(
    corpus: list[tuple[DocumentRecord, str]], index: resolver.ResolverIndex
) -> None:
    """Three sources and several spellings merge into one entry with counts.

    "Gas bottle", "gas bottles" and "gas  bottle" are one curation decision, and
    splitting them would make the queue look longer than the work actually is.
    """
    store = pyoxigraph.Store()
    table = gaps_table()

    pipeline.ingest_corpus(
        corpus,
        store,
        index=index,
        extract=_sentence_reader,
        gaps_table=table,
        write_vectors=False,
    )

    # Read back through the library rather than off the item, so the assertion
    # is about what a curator sees and survives a change of storage shape -- the
    # per-source counters are flat attributes because DynamoDB's ADD refuses a
    # nested path, and nothing above gaps.py should have to know that.
    entry = gaps.read_gap("gas bottle", table_resource=table)

    assert entry is not None
    assert entry.counts == {"ingestion": 12}
    assert entry.total_occurrences == 12
    assert entry.status is gaps.GapStatus.open
    assert entry.evidence

    # The corpus writes one spelling twelve times, and one of those twelve is
    # wrapped across a line as "gas\nbottle". Both reach the queue as the same
    # variant, which is the merge working: what makes them one entry is the
    # gap key, and every variant on the entry has to fold onto it.
    assert entry.variants
    assert {gaps.gap_key(v) for v in entry.variants} == {entry.gap_id}

    for evidence in entry.evidence:
        assert GAS_BOTTLE.search(evidence.text), "evidence must be the sentence, verbatim"
        assert ":" in evidence.locator, "evidence must carry source_file:line_range"


def test_both_seeded_contradictions_are_flagged(
    corpus: list[tuple[DocumentRecord, str]], index: resolver.ResolverIndex
) -> None:
    """Across documents, which is why step 5 runs once at the end: neither
    document can see the other while it is being read."""
    store = pyoxigraph.Store()

    result = pipeline.ingest_corpus(
        corpus,
        store,
        index=index,
        extract=_sentence_reader,
        gaps_table=gaps_table(),
        write_vectors=False,
    )

    assert result.conflicts_found == len(REGISTRY["seeded_contradictions"])

    rows = facts.read_claims(store)
    flagged = [row for row in rows if row.get("conflicts_with")]
    assert flagged, "a conflict must be readable from the claim, not only from the run"


def test_re_ingesting_the_corpus_changes_nothing(
    corpus: list[tuple[DocumentRecord, str]], index: resolver.ResolverIndex
) -> None:
    """ADR-002: re-ingestion is idempotent on (source_file, doc_version).

    Claim IRIs are content hashes and RDF is a set, so this falls out of the
    data model rather than out of a de-duplication pass -- for an *unchanged*
    reading. See the next test for the half that does not fall out.
    """
    store = pyoxigraph.Store()
    kwargs: dict[str, Any] = {
        "index": index,
        "extract": _sentence_reader,
        "gaps_table": gaps_table(),
        "write_vectors": False,
    }

    pipeline.ingest_corpus(corpus, store, **kwargs)
    after_first = graph.dump(store)

    pipeline.ingest_corpus(corpus, store, **kwargs)
    assert graph.dump(store) == after_first


def test_a_document_read_differently_replaces_its_claims(
    corpus: list[tuple[DocumentRecord, str]], index: resolver.ResolverIndex
) -> None:
    """The half content-addressing does not cover, found the expensive way.

    `snippet` is not one of the six fields the claim IRI hashes, so a document
    whose *text* is read differently comes back as the same claim wearing a
    second, contradictory snippet -- and every consumer does a single-valued
    read, so the console shows whichever row SPARQL returns first. The HTML
    de-markup fix moved exactly this: `Supply pressure1.5 – 6.0 bar` became
    `Supply pressure 1.5 – 6.0 bar`.

    So a document is retracted before it is re-read. Simulated here by changing
    what the reader emits, which is the same thing from the graph's point of
    view.
    """
    store = pyoxigraph.Store()
    kwargs: dict[str, Any] = {
        "index": index,
        "gaps_table": gaps_table(),
        "write_vectors": False,
    }

    pipeline.ingest_corpus(corpus, store, extract=_sentence_reader, **kwargs)

    def _shouting_reader(chunk: Chunk, source: SourceText, **_: Any) -> extraction.Extraction:
        outcome = _sentence_reader(chunk, source)
        for fact in outcome.facts:
            fact["snippet"] = fact["snippet"].upper()
        return outcome

    result = pipeline.ingest_corpus(corpus, store, extract=_shouting_reader, **kwargs)

    assert result.claims_retracted > 0, "the second run did not clear the first"

    # Every surviving snippet must come from the *second* reading. Asserted this
    # way rather than as "one snippet per claim", because a claim legitimately
    # carries several within a single run: the IRI hashes the evidence and not
    # the subject, so four facts quoting one line are one claim with four
    # surface forms. That is the property re-ingestion idempotence rests on, and
    # it is not what this test is about.
    # `.value`, not `str(quad.object)`: the serialised literal escapes newlines
    # as a backslash and a lowercase `n`, so comparing the serialisation reports
    # every multi-line snippet as stale.
    stale = [
        quad.object.value
        for quad in store.quads_for_pattern(
            None,
            pyoxigraph.NamedNode(f"{config.ID_NAMESPACE}snippet"),
            None,
            pyoxigraph.NamedNode(config.FACTS_GRAPH),
        )
        if isinstance(quad.object, pyoxigraph.Literal)
        and quad.object.value != quad.object.value.upper()
    ]
    assert not stale, f"snippets from the first reading survived: {stale[:2]}"


def test_retraction_is_scoped_to_one_document(
    corpus: list[tuple[DocumentRecord, str]], index: resolver.ResolverIndex
) -> None:
    """Re-reading one file must not disturb what another evidenced."""
    store = pyoxigraph.Store()
    pipeline.ingest_corpus(
        corpus,
        store,
        index=index,
        extract=_sentence_reader,
        gaps_table=gaps_table(),
        write_vectors=False,
    )

    others = {
        str(quad.subject)
        for quad in store.quads_for_pattern(
            None,
            pyoxigraph.NamedNode(f"{config.ID_NAMESPACE}sourceFile"),
            None,
            pyoxigraph.NamedNode(config.FACTS_GRAPH),
        )
        if str(quad.object) != '"01-installation-manual-v3.md"'
    }

    facts.retract_document(store, "01-installation-manual-v3.md")

    survivors = {
        str(quad.subject)
        for quad in store.quads_for_pattern(
            None,
            pyoxigraph.NamedNode(f"{config.ID_NAMESPACE}sourceFile"),
            None,
            pyoxigraph.NamedNode(config.FACTS_GRAPH),
        )
    }
    assert survivors == others
    assert others, "the corpus has claims from more than one document"


def test_claims_carry_the_stage_that_resolved_them(
    corpus: list[tuple[DocumentRecord, str]], index: resolver.ResolverIndex
) -> None:
    """ADR-002: every resolution records which stage matched and at what score,
    so a concept link can be explained after the fact."""
    store = pyoxigraph.Store()
    pipeline.ingest_corpus(
        corpus,
        store,
        index=index,
        extract=_sentence_reader,
        gaps_table=gaps_table(),
        write_vectors=False,
    )

    stages = {
        str(q.object.value).rsplit("/", 1)[-1]
        for q in store.quads_for_pattern(
            None,
            pyoxigraph.NamedNode(f"{config.ID_NAMESPACE}resolvedBy"),
            None,
            pyoxigraph.NamedNode(config.FACTS_GRAPH),
        )
    }

    assert "abstain" in stages, "the held gas-bottle claims"
    assert stages <= {"exact", "embedding", "abstain"}
