"""The gap queue: one entry per surface form, evidence attached, counts explainable."""

import pytest
from h2o_core import config
from h2o_core.gaps import GapEntry, GapStatus, GapType, gap_key, should_resurface
from h2o_core.normalize import normalise


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("gas bottle", "gas bottle"),
        ("Gas Bottles", "gas bottle"),
        ("gas  bottle", "gas bottle"),
        ("GAS-BOTTLE", "gas bottle"),
        ("CO₂ cylinders", "co2 cylinder"),
    ],
)
def test_variants_merge_into_one_entry(written: str, expected: str) -> None:
    """Three sources and five spellings are one entry with three counts, not
    fifteen entries (ADR-004)."""
    assert gap_key(written) == expected


def test_the_merge_key_is_looser_than_the_resolver_and_deliberately_so() -> None:
    """Decision A.

    The queue wants "gas bottle" and "gas bottles" merged. Teaching normalise()
    to stem would change what label identity means for the published index and
    break the resolver-parity guarantee the integrity gate depends on
    (ADR-005), so the plural fold lives only here.
    """
    assert normalise("gas bottles") != normalise("gas bottle")
    assert gap_key("gas bottles") == gap_key("gas bottle")


@pytest.mark.parametrize("word", ["glass", "analysis", "status", "gas"])
def test_the_plural_fold_does_not_maul_real_words(word: str) -> None:
    """Stemming is a heuristic and this is the cheap end of it: never strip an
    s from -ss, -is, -us, or from a word short enough to be one."""
    assert gap_key(word) == normalise(word)


def test_a_dismissed_term_stays_dismissed_until_the_volume_changes() -> None:
    """Suppression has to exist or the queue never converges: a term already
    judged irrelevant reappears every ingest run and becomes noise."""
    entry = GapEntry(
        gap_id="scale buildup",
        surface_form="scale_buildup",
        normalised_form="scale buildup",
        status=GapStatus.dismissed,
        dismissed_at_count=5,
        total_occurrences=200,
    )

    assert not should_resurface(entry)


def test_a_hundredfold_jump_resurfaces_it_once() -> None:
    """Not permanent amnesty: volume that changes by two orders of magnitude is
    new information (ADR-004 §5)."""
    entry = GapEntry(
        gap_id="scale buildup",
        surface_form="scale_buildup",
        normalised_form="scale buildup",
        status=GapStatus.dismissed,
        dismissed_at_count=5,
        total_occurrences=5 * config.RESURFACE_MULTIPLIER,
    )

    assert should_resurface(entry)

    entry.resurfaced_at = "2026-08-11T00:00:00+00:00"
    assert not should_resurface(entry), "it comes back once, not on every run"


def test_counts_keep_accruing_while_dismissed() -> None:
    """Which is exactly what makes the rule computable: dismissal is a
    presentation state, not an ingestion one."""
    entry = GapEntry(
        gap_id="gas bottle",
        surface_form="gas bottle",
        normalised_form="gas bottle",
        status=GapStatus.dismissed,
        dismissed_at_count=1,
        total_occurrences=0,
    )
    assert not should_resurface(entry)

    entry.total_occurrences = config.RESURFACE_MULTIPLIER
    assert should_resurface(entry)


def test_the_entry_renders_as_one_specific_question() -> None:
    """ADR-004 §4: a closed set of types so the console asks a question rather
    than showing a generic form."""
    entry = GapEntry(
        gap_id="gas bottle",
        surface_form="gas bottle",
        normalised_form="gas bottle",
        gap_type=GapType.add_alt_label,
        counts={"ingestion": 12, "chat": 3},
        total_occurrences=15,
        suggestions=[{"concept_id": "co2-cylinder", "pref_label": "CO₂ Cylinder", "score": 0.81}],
    )

    question = entry.question
    assert "gas bottle" in question
    assert "CO₂ Cylinder" in question
    assert "12 ingestion mentions" in question
    assert "3 chat mentions" in question


def test_an_unmapped_signal_asks_for_a_new_concept_not_a_label() -> None:
    """The telemetry loop's question. scale_buildup matches nothing at all, so
    the suggestion is a scheme rather than a term to hang an alias on."""
    entry = GapEntry(
        gap_id="scale buildup",
        surface_form="scale_buildup",
        normalised_form="scale buildup",
        gap_type=GapType.new_concept,
        counts={"telemetry": 214},
        total_occurrences=214,
        suggested_scheme="Fault",
    )

    question = entry.question
    assert "scale_buildup" in question
    assert "214 telemetry mentions" in question
    assert "matches nothing" in question
    assert "Fault" in question
