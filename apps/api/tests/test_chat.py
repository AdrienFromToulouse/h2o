"""The chat wire contract: what a chip is allowed to say.

ADR-006 §133 requires the TypeScript and Python event definitions to change
together, and there is no shared mirror file to make that automatic. So the
shape is asserted here, against `apps/frontend/components/Chat.tsx`.
"""

from __future__ import annotations

from h2o_api import chat
from h2o_core.retrieval import Answer, ResolvedTerm, UnresolvedTerm


def _answer() -> Answer:
    return Answer(
        question="how do I check the gas bottle pressure",
        resolved=[
            ResolvedTerm(
                surface_form="installtion",
                concept_id="installation",
                pref_label="Installation",
                stage="exact",
                score=1.0,
            )
        ],
        unresolved=[
            UnresolvedTerm(
                surface_form="gas bottle",
                suggestions=[
                    {
                        "concept_id": "bottles-avoided",
                        "pref_label": "Single-Use Bottles Avoided",
                        "score": 0.348,
                    },
                    {"concept_id": "co2-cylinder", "pref_label": "CO₂ Cylinder", "score": 0.28},
                ],
                gap_id="gas bottle",
            )
        ],
    )


def test_a_miss_chip_does_not_suggest_a_replacement() -> None:
    """The shortlist is a curator's artefact and stays on the queue entry.

    Rendered beside an answer it reads as "did you mean", which the similarity
    score cannot support on this vocabulary: measured against the deployed
    index, "limescale" scores 0.170 while the verb "replace" scores 0.393. The
    console showed the consequence -- "process → not in the vocabulary · closest:
    Fault, Dispenser, Component", three unrelated concepts.
    """
    miss = next(
        event["item"]
        for event in chat.events(_answer(), "…")
        if event["type"] == "concept" and event["item"]["origin"] == "miss"
    )

    assert "near_terms" not in miss
    assert not any("Cylinder" in str(value) for value in miss.values())
    assert miss["surface_form"] == "gas bottle"
    assert miss["gap_id"] == "gas bottle", "and it still reached the queue"


def test_the_suggestions_still_reach_the_gap_entry() -> None:
    """Dropping the chip must not drop the data. ADR-004's whole argument is that
    a shortlist turns an unbounded authoring task into a yes/no judgement -- for
    the person doing the authoring."""
    answer = _answer()

    assert answer.unresolved[0].suggestions[0]["pref_label"]


def test_a_corrected_term_is_indistinguishable_from_an_exact_match() -> None:
    """A spelling correction is silent by design, so there is no third chip state
    to render. The left-hand side is always what was typed, which is the only
    thing that makes "installtion → Installation" worth reading."""
    resolution = next(
        event["item"]
        for event in chat.events(_answer(), "…")
        if event["type"] == "concept" and event["item"]["origin"] == "resolution"
    )

    assert resolution["surface_form"] == "installtion"
    assert resolution["pref_label"] == "Installation"
    assert resolution["matched_on"] == "exact"


def test_the_wire_fields_match_the_frontend_type() -> None:
    """The two definitions have no shared file, so this is what notices when one
    of them is edited alone."""
    from pathlib import Path

    tsx = (Path(__file__).resolve().parents[3] / "apps/frontend/components/Chat.tsx").read_text()

    assert "near_terms" not in tsx, "the frontend still declares a field the API stopped sending"
    for field in ("origin", "surface_form", "concept_id", "pref_label", "gap_id"):
        assert field in tsx, f"the frontend dropped {field}"
