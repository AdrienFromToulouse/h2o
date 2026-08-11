"""The resolve cascade, and what it is allowed to suggest.

The shortlist is the part of resolution a person actually reads: it becomes the
gap entry's suggested attachment point, and it is what turns a bare miss into
"closest existing terms". So what may appear in it is a constraint, not a
ranking detail.
"""

from __future__ import annotations

import pyoxigraph
import pytest
from h2o_core import config, resolve, resolver
from h2o_core.resolve import Stage


@pytest.fixture
def index(store: pyoxigraph.Store) -> resolver.ResolverIndex:
    return resolver.build(store, watermark="test")


def _embedder(index: resolver.ResolverIndex, *, near: dict[str, float]):  # type: ignore[no-untyped-def]
    """Crafted vectors, so a threshold is tested rather than a model version.

    A test pinning a real Titan score fails when the model rolls, and would be
    asserting the embedding rather than the cascade.
    """
    labels = sorted(index.by_label)
    index.vectors = {
        label: [near.get(label, 0.0), (1.0 - near.get(label, 0.0) ** 2) ** 0.5] for label in labels
    }
    return lambda text: [1.0, 0.0]


def test_an_exact_label_resolves_and_says_so(index: resolver.ResolverIndex) -> None:
    verdict = resolve.resolve("Carbon Filter", index=index)

    assert verdict.concept_id == "carbon-filter"
    assert verdict.stage is Stage.exact
    assert verdict.score == 1.0


def test_gas_bottle_resolves_to_nothing(index: resolver.ResolverIndex) -> None:
    """The seeded gap, and the premise of the whole demonstrator: the vocabulary
    deliberately carries no such label, so the mention holds its claim."""
    verdict = resolve.resolve("gas bottle", index=index)

    assert verdict.concept_id is None
    assert verdict.stage is Stage.abstain


def test_the_shortlist_never_offers_a_machine_term(index: resolver.ResolverIndex) -> None:
    """ADR-003 §3.1 and ADR-006 §2.

    Found by running the real embedder against the real vocabulary: Titan put
    `instrument.bottles_avoided` second for "gas bottle", ahead of CO₂ Cylinder,
    because the label shares a word. Offering a curator an instrument name as a
    place to attach a business term is the leakage those ADRs forbid -- and a
    document mention resolving to a firmware name would be worse still.
    """
    embed = _embedder(index, near={"instrument bottles avoided": 0.99, "co2 cylinder": 0.4})

    verdict = resolve.resolve("gas bottle", index=index, embed=embed)

    machine = {
        candidate.concept_id
        for candidate in verdict.shortlist
        if index.schemes.get(candidate.concept_id) == config.MACHINE_SCHEME
    }
    assert not machine, f"machine terms reached a curator: {machine}"
    assert verdict.stage is Stage.abstain, "and it certainly did not resolve to one"


def test_an_instrument_name_still_resolves_exactly(index: resolver.ResolverIndex) -> None:
    """Excluding the machine scheme from the *embedding* stage costs nothing the
    OTEL mapper needs: ADR-003 maps a declared instrument name by exact match,
    which is a different stage."""
    machine = [c for c, s in index.schemes.items() if s == config.MACHINE_SCHEME]
    assert machine, "the seed vocabulary carries a machine scheme"

    label = index.pref_labels[machine[0]]
    assert resolve.resolve(label, index=index).concept_id == machine[0]


def test_a_confident_match_that_is_not_clear_abstains(index: resolver.ResolverIndex) -> None:
    """Two concepts scoring alike is a MergeDuplicate signal, not a resolution.
    Picking one is the silent failure the margin rule exists to prevent."""
    embed = _embedder(index, near={"carbon filter": 0.95, "sediment filter": 0.94})

    verdict = resolve.resolve("filter thing", index=index, embed=embed)

    assert verdict.stage is Stage.abstain
    assert len(verdict.shortlist) >= 2


def test_the_shortlist_is_populated_even_when_nothing_matches(
    index: resolver.ResolverIndex,
) -> None:
    """Which is what makes a miss useful: "closest existing terms" is the gap
    entry's suggested attachment point, and a bare miss would give a curator
    nothing to act on."""
    embed = _embedder(index, near={"co2 cylinder": 0.5})

    verdict = resolve.resolve("gas bottle", index=index, embed=embed)

    assert verdict.stage is Stage.abstain
    assert verdict.shortlist
