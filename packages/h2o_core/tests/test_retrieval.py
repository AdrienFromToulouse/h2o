"""The question sweep: which phrases become chips, and which become queue rows.

This is the mechanism a person actually sees, and it had no test at all. Every
case below is a shape the deployed console produced -- including two it produced
wrongly, kept here as the record of what was fixed.

Nothing here reaches AWS. The graph is the real seed vocabulary, the embedder is
crafted so a threshold is tested rather than a model version, and the sanitiser
is a fake whose call count is itself an assertion: a question that resolves must
not pay for a model call it does not need.
"""

from __future__ import annotations

from typing import Any

import pyoxigraph
import pytest
from fakes import FakeBedrock, FakeTable, FakeVectors
from h2o_core import resolver, retrieval
from h2o_core.retrieval import candidate_terms, content_phrase


@pytest.fixture
def index(store: pyoxigraph.Store) -> resolver.ResolverIndex:
    return resolver.build(store, watermark="test")


@pytest.fixture
def gaps_table() -> FakeTable:
    return FakeTable(hash_key="gap_id")


def _embedder(index: resolver.ResolverIndex, **near: float):  # type: ignore[no-untyped-def]
    """A crafted label space, so nothing here depends on a Titan version."""
    index.vectors = {
        label: [near.get(label.replace(" ", "_"), 0.0), 1.0] for label in sorted(index.by_label)
    }
    return lambda text: [1.0, 0.0]


def _corrections(*pairs: tuple[str, str, str]) -> dict[str, Any]:
    return {"corrections": [{"original": o, "corrected": c, "kind": k} for o, c, k in pairs]}


def _sweep(question: str, index: resolver.ResolverIndex, **kwargs: Any) -> list[Any]:
    return candidate_terms(question, index=index, **kwargs)


def _surfaces(verdicts: list[Any]) -> list[str]:
    return [verdict.surface_form for verdict in verdicts]


# ------------------------------------------------------------------ the sweep


def test_a_multi_word_label_resolves_as_one_term(index: resolver.ResolverIndex) -> None:
    """Longest-first with consumption, which is what stops "carbon filter"
    becoming a chip for "carbon" and another for "filter"."""
    verdicts = _sweep("when is the carbon filter replaced", index)

    resolved = {v.surface_form: v.concept_id for v in verdicts if v.matched}
    assert resolved.get("carbon filter") == "carbon-filter"
    assert "filter" not in resolved


def test_the_longest_window_does_not_win_over_the_real_phrase(
    index: resolver.ResolverIndex,
) -> None:
    """The bug the candidate-shaping rule exists for.

    Judging candidates after the fact was not enough: the sweep takes the
    longest window first, so it offered "I check the gas" before it ever offered
    "gas bottle pressure", consumed the words, and queued an entry about
    nothing. A candidate may not begin or end with a function word.
    """
    verdicts = _sweep("how do I check the gas bottle pressure", index, embed=_embedder(index))

    surfaces = _surfaces(verdicts)
    assert not any(s.startswith(("I ", "how ", "the ")) for s in surfaces), surfaces
    assert any("gas bottle" in s for s in surfaces), surfaces


@pytest.mark.parametrize(
    "question",
    ["what is the process", "tell me the details", "what are the steps"],
)
def test_a_phrase_of_only_generic_words_is_not_a_term(
    index: resolver.ResolverIndex, question: str
) -> None:
    """The console showed "process → not in the vocabulary · closest: Fault,
    Dispenser, Component" -- three unrelated concepts, because the question the
    similarity score answers is not the question the chip was asking. A generic
    noun names nothing a curator could add a label for."""
    verdicts = _sweep(question, index, embed=_embedder(index))

    assert not [v for v in verdicts if not v.matched], _surfaces(verdicts)


def test_a_generic_noun_that_is_a_real_label_still_resolves(
    index: resolver.ResolverIndex,
) -> None:
    """`_GENERIC_NOUNS` is safe to list a word the vocabulary uses, because exact
    match runs first and consumes it."""
    verdicts = _sweep("which part is it", index)

    assert {v.concept_id for v in verdicts if v.matched} == {"component"}


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("how do I check the gas bottle", "gas bottle"),
        ("the carbon filter", "carbon filter"),
        ("the rate of flow", "rate of flow"),
        ("quelle est la pression", "pression"),
        ("how do I", ""),
        # The limit, asserted rather than left to be rediscovered: `use` is a
        # listed generic verb, so a term ending in one cannot be reported through
        # here. It costs nothing today because such a term resolves at the exact
        # stage, which runs first.
        ("point of use", "point"),
    ],
)
def test_content_phrase_trims_the_edges_and_not_the_middle(phrase: str, expected: str) -> None:
    """Trimming rather than filtering: a function word *inside* a term is part of
    it, so "rate of flow" survives whole where filtering would leave "rate flow"."""
    assert content_phrase(phrase) == expected


# ------------------------------------------------- the sanitiser, on the miss path


def test_a_question_that_resolves_never_calls_the_sanitiser(
    index: resolver.ResolverIndex, store: pyoxigraph.Store, gaps_table: FakeTable
) -> None:
    """The extra model call is bought by a miss. A clean question pays nothing,
    which is the whole reason it is a second attempt and not a first pass."""
    client = FakeBedrock({"corrections": []})

    retrieval.retrieve(
        "carbon filter",
        store,
        index=index,
        gaps_table=gaps_table,
        sanitise_client=client,
    )

    assert client.calls == 0


def test_a_typo_resolves_and_leaves_no_queue_row(
    index: resolver.ResolverIndex, store: pyoxigraph.Store, gaps_table: FakeTable
) -> None:
    """The case that started this: "installtion" is one keystroke from a label
    the index holds, and the console answered that it had nothing on it."""
    client = FakeBedrock(_corrections(("installtion", "installation", "spelling")))

    answer = retrieval.retrieve(
        "what is the installtion process?",
        store,
        index=index,
        gaps_table=gaps_table,
        sanitise_client=client,
    )

    assert [(t.surface_form, t.concept_id) for t in answer.resolved] == [
        ("installtion", "installation")
    ]
    assert not answer.unresolved
    assert not gaps_table.items, "a corrected term is not a vocabulary gap"


def test_the_chip_keeps_the_words_that_were_typed(
    index: resolver.ResolverIndex, store: pyoxigraph.Store, gaps_table: FakeTable
) -> None:
    """The correction is silent, which is only coherent if the left-hand side of
    the chip is what the person wrote. "installation → Installation" tells a
    reader nothing."""
    client = FakeBedrock(_corrections(("installtion", "installation", "spelling")))

    answer = retrieval.retrieve(
        "the installtion steps",
        store,
        index=index,
        gaps_table=gaps_table,
        sanitise_client=client,
    )

    assert answer.resolved[0].surface_form == "installtion"
    assert answer.resolved[0].pref_label == "Installation"


def test_the_seeded_gap_survives_the_sanitiser(
    index: resolver.ResolverIndex, store: pyoxigraph.Store, gaps_table: FakeTable
) -> None:
    """**The regression that matters most.**

    "gas bottle" is correctly-spelled English and a *synonym* of CO₂ Cylinder,
    not a misspelling of it. A model shown the vocabulary would map it and be
    right to, and the entry ADR-001's central claim depends on would vanish. The
    sanitiser cannot see the vocabulary, so it returns nothing and the miss
    stands.
    """
    client = FakeBedrock({"corrections": []})

    answer = retrieval.retrieve(
        "how do I check the gas bottle pressure",
        store,
        index=index,
        embed_one=_embedder(index),
        gaps_table=gaps_table,
        sanitise_client=client,
    )

    assert not answer.resolved
    assert any("gas bottle" in term.surface_form for term in answer.unresolved)
    assert any("gas bottle" in str(key) for key in gaps_table.items), gaps_table.items


def test_a_typo_of_a_function_word_does_not_reach_the_queue(
    index: resolver.ResolverIndex, store: pyoxigraph.Store, gaps_table: FakeTable
) -> None:
    """Found against the deployed API, not in a fixture.

    "how often do I replce the carbon filtre" queued an entry for `replce`. The
    sanitiser had already said it meant "replace", which is a listed function
    word and precisely what `_worth_reporting` refuses -- but the check was
    reading the typed form, where the typo hid the function word from the list.
    A curator's queue is not the place to learn that somebody mistyped a verb.
    """
    client = FakeBedrock(
        _corrections(("replce", "replace", "spelling"), ("filtre", "filter", "spelling"))
    )

    answer = retrieval.retrieve(
        "how often do I replce the carbon filtre",
        store,
        index=index,
        embed_one=_embedder(index),
        gaps_table=gaps_table,
        vectors_client=FakeVectors(),
        sanitise_client=client,
    )

    assert [t.surface_form for t in answer.unresolved] == []
    assert not gaps_table.items, f"a mistyped verb reached the queue: {list(gaps_table.items)}"


def test_a_multi_word_alias_may_not_be_what_makes_a_term_resolve(
    index: resolver.ResolverIndex, store: pyoxigraph.Store, gaps_table: FakeTable
) -> None:
    """The `gas cylinder` observation, generalised into a rule.

    Asked in French about the gas bottle, the live model returned `bouteille de
    gaz -> gas cylinder`: the right phrase, and a *substituted* English term
    rather than a translated one. That one was harmless because "gas cylinder"
    is not a label. The same move landing on a term that *is* one would resolve,
    and the entry the demonstrator turns on would be gone.

    Single-word aliases stay exempt, and that asymmetry is the whole point:
    "installtion" and "koolstoffilter" are the cases that must resolve, and one
    word leaves no room for the phrase-level reinterpretation observed here.
    """
    client = FakeBedrock(_corrections(("bouteille de gaz", "carbon filter", "translation")))

    answer = retrieval.retrieve(
        "ou est la bouteille de gaz",
        store,
        index=index,
        embed_one=_embedder(index),
        gaps_table=gaps_table,
        sanitise_client=client,
    )

    assert not answer.resolved, "a multi-word alias resolved a term it invented"
    assert answer.unresolved


def test_an_alias_that_is_already_a_label_is_ignored(
    index: resolver.ResolverIndex, store: pyoxigraph.Store, gaps_table: FakeTable
) -> None:
    """A word the index knows is not a misspelling, whatever a model thinks.
    Without the prune, a stray correction could take away a term that was
    resolving perfectly well."""
    client = FakeBedrock(_corrections(("filter", "filters", "spelling")))

    answer = retrieval.retrieve(
        "the filter and the limescale",
        store,
        index=index,
        gaps_table=gaps_table,
        sanitise_client=client,
    )

    assert [t.concept_id for t in answer.resolved] == ["filter"]


# --------------------------------------------------------------- multilingual


def test_a_translated_miss_merges_with_the_english_entry(
    index: resolver.ResolverIndex, store: pyoxigraph.Store, gaps_table: FakeTable
) -> None:
    """Two people asking the same thing in two languages are one gap.

    Edit distance could never have done this -- "bouteille de gaz" is not a
    misspelling of anything -- and without the merge the queue would carry one
    entry per language, splitting the count the console orders by.
    """
    english = FakeBedrock({"corrections": []})
    retrieval.retrieve(
        "where is the gas bottle",
        store,
        index=index,
        embed_one=_embedder(index),
        gaps_table=gaps_table,
        sanitise_client=english,
    )
    keys_after_english = set(gaps_table.items)
    assert keys_after_english == {("gas bottle",)}

    french = FakeBedrock(_corrections(("bouteille de gaz", "gas bottle", "translation")))
    retrieval.retrieve(
        "ou est la bouteille de gaz",
        store,
        index=index,
        embed_one=_embedder(index),
        gaps_table=gaps_table,
        sanitise_client=french,
    )

    assert set(gaps_table.items) == keys_after_english, (
        f"the French question opened its own entry: {set(gaps_table.items)}"
    )
    assert gaps_table.all_items()[0]["total_occurrences"] == 2, "one entry, two occurrences"


def test_an_aliased_span_beats_a_longer_guess(
    index: resolver.ResolverIndex, store: pyoxigraph.Store, gaps_table: FakeTable
) -> None:
    """The straddling bug, in its third form.

    "quelle est la pression de la bouteille de gaz" offered "pression de la
    bouteille" first: four words with content at both edges, so neither the
    function-word rule nor the generic-noun rule rejects it. It consumed the
    middle of the question and left "gaz" alone, producing two queue rows that
    name nothing. An alias is evidence of where a term ends; length is only a
    guess, so an aliased span is tried first.
    """
    client = FakeBedrock(
        _corrections(
            ("bouteille de gaz", "gas bottle", "translation"),
            ("pression", "pressure", "translation"),
        )
    )

    retrieval.retrieve(
        "quelle est la pression de la bouteille de gaz",
        store,
        index=index,
        embed_one=_embedder(index),
        gaps_table=gaps_table,
        sanitise_client=client,
    )

    assert set(gaps_table.items) == {("gas bottle",), ("pressure",)}


def test_the_typed_words_survive_as_the_entry_variant(
    index: resolver.ResolverIndex, store: pyoxigraph.Store, gaps_table: FakeTable
) -> None:
    """Merging on the English form must not lose what somebody actually typed --
    a curator judging whether a term is really used needs to see it."""
    client = FakeBedrock(
        _corrections(
            ("bouteille de gaz", "gas bottle", "translation"),
            ("pression", "pressure", "translation"),
        )
    )

    retrieval.retrieve(
        "quelle est la pression de la bouteille de gaz",
        store,
        index=index,
        embed_one=_embedder(index),
        gaps_table=gaps_table,
        sanitise_client=client,
    )

    entry = next(item for item in gaps_table.all_items() if item["gap_id"] == "gas bottle")
    assert any("bouteille" in variant for variant in entry["variants"]), entry["variants"]
    assert "bouteille" in next(iter(entry["evidence"].values()))["text"]
    assert entry["surface_form"] == "gas bottle", "the entry a curator reads is in English"


# ------------------------------------------------------------------ the refusal


def test_nothing_resolved_means_nothing_retrieved(
    index: resolver.ResolverIndex, store: pyoxigraph.Store, gaps_table: FakeTable
) -> None:
    """The fallback everyone reaches for -- search the vectors unfiltered -- is
    the plain vector search this design exists to replace."""
    client = FakeBedrock({"corrections": []})

    answer = retrieval.retrieve(
        "how do I check the gas bottle pressure",
        store,
        index=index,
        embed_one=_embedder(index),
        gaps_table=gaps_table,
        sanitise_client=client,
    )

    assert not answer.understood
    assert not answer.passages
    assert not answer.searched_concepts


def test_limescale_is_still_missing(
    index: resolver.ResolverIndex, store: pyoxigraph.Store, gaps_table: FakeTable
) -> None:
    """ADR-003's second loop is built on the vocabulary genuinely lacking this.
    A sanitiser that "helped" here would be correcting terminology."""
    client = FakeBedrock({"corrections": []})

    answer = retrieval.retrieve(
        "what causes limescale",
        store,
        index=index,
        embed_one=_embedder(index),
        gaps_table=gaps_table,
        sanitise_client=client,
    )

    assert not answer.resolved
    assert [t.surface_form for t in answer.unresolved] == ["causes limescale"]
    assert ("causes limescale",) in gaps_table.items


def test_the_recorded_phrase_is_the_window_and_not_the_term(
    index: resolver.ResolverIndex, store: pyoxigraph.Store, gaps_table: FakeTable
) -> None:
    """A known open question, asserted so it is a decision and not a surprise.

    "what causes limescale" queues "causes limescale", because `causes` is
    neither a function word nor a generic noun and the window is what the sweep
    had. It is honest -- somebody really did ask that -- but it does not merge
    with the "limescale" the telemetry mapper will file against the same hole,
    and it splits a count the console orders by.

    Fixing it means either a much larger stopword list, which is a maintenance
    burden that fails silently, or recording `content_phrase` instead of the
    window, which would compute the shortlist against one string and file the
    entry under another. Neither has been chosen, so this records what happens.
    """
    client = FakeBedrock({"corrections": []})

    retrieval.retrieve(
        "what causes limescale",
        store,
        index=index,
        embed_one=_embedder(index),
        gaps_table=gaps_table,
        sanitise_client=client,
    )

    assert ("limescale",) not in gaps_table.items
