"""What a proposed edit would actually do, computed before Save.

ADR-005 §4. This is the single feature that makes the console more than a form:
it turns an abstract edit into a visible consequence, and it is what a domain
expert needs in order to judge whether a change is worth making at all.

Everything here is deterministic and reads only what ingestion already wrote.
Held claims carry `h2o:heldSurface` -- the gap merge key -- precisely so this
question can be answered without re-reading a single document, which is also
what lets the same query drive the fan-out's re-resolution afterwards. The
preview and the thing it predicts are the same lookup.

The draft arrives in the request body rather than from the graph, so the
sentence updates as the expert types and before anything has been saved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pyoxigraph
from pydantic import BaseModel, Field

from h2o_core import gaps, graph, integrity, sparql, vocabulary
from h2o_core.models import LabelKind
from h2o_core.normalize import normalise

__all__ = ["ConceptDraft", "Impact", "HeldMention", "preview"]


class ConceptDraft(BaseModel):
    """A proposed edit to one concept, as the review card holds it.

    Only the fields a curator can change. `concept_id` names an existing term,
    or a new slug for a concept being created -- the impact of creating one is
    a real question ("None of these" in ADR-004's flow), and it is the same
    query.
    """

    concept_id: str
    pref_label: str | None = None
    alt_labels: list[str] = Field(default_factory=list)
    hidden_labels: list[str] = Field(default_factory=list)
    definition: str | None = None
    scope_note: str | None = None
    broader: str | None = None
    related: list[str] = Field(default_factory=list)
    scheme_id: str | None = None
    change_note: str | None = None

    def surfaces(self) -> list[str]:
        """Every label this draft would add, as the queue's merge keys.

        `gap_key`, not `normalise`: a held claim records the key the gap queue
        merged it under, so "gas bottles" and "gas bottle" are one lookup here
        for the same reason they are one queue entry.
        """
        proposed = [self.pref_label, *self.alt_labels, *self.hidden_labels]
        return sorted({gaps.gap_key(text) for text in proposed if text and gaps.gap_key(text)})


@dataclass(frozen=True)
class HeldMention:
    """One claim that would go live, with the evidence it already carries."""

    claim_id: str
    surface_form: str
    predicate: str
    value: str
    source_file: str
    doc_version: str
    line_range: str
    snippet: str


@dataclass
class Impact:
    """The consequences of publishing this draft, in numbers and in a sentence."""

    concept_id: str
    mentions: list[HeldMention] = field(default_factory=list)
    documents: list[str] = field(default_factory=list)
    children_gained: list[str] = field(default_factory=list)
    children_lost: list[str] = field(default_factory=list)
    #: The gate, run early and non-fatally, so a collision is visible before the
    #: change note is typed rather than after Save (ADR-005 §4).
    findings: list[integrity.Finding] = field(default_factory=list)
    surfaces: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(integrity.blocking(self.findings))

    @property
    def sentence(self) -> str:
        """One sentence, in the words ADR-006 puts in the review card.

        Rendered here and nowhere else. The API, the console and any test all
        read the same string, so "the impact sentence" is one thing that can be
        wrong in one place rather than three that can disagree.

        Says nothing rather than something vague when there is nothing to say:
        "will resolve 0 mentions" reads as a failure, when the truth is that
        this edit is simply not the kind that resolves anything.
        """
        if not self.mentions:
            return ""
        mentions = _plural(len(self.mentions), "mention")
        documents = _plural(len(self.documents), "document")
        return f"Adding this alternative term will resolve {mentions} across {documents}."

    def as_dict(self) -> dict[str, Any]:
        return {
            "concept_id": self.concept_id,
            "sentence": self.sentence,
            "mention_count": len(self.mentions),
            "document_count": len(self.documents),
            "documents": self.documents,
            "surfaces": self.surfaces,
            "children_gained": self.children_gained,
            "children_lost": self.children_lost,
            "blocked": self.blocked,
            "findings": [
                {"concept_id": f.concept_id, "message": f.message, "severity": f.severity}
                for f in self.findings
            ],
            "mentions": [
                {
                    "surface_form": m.surface_form,
                    "predicate": m.predicate,
                    "value": m.value,
                    "source_file": m.source_file,
                    "doc_version": m.doc_version,
                    "line_range": m.line_range,
                    "snippet": m.snippet,
                }
                for m in self.mentions
            ],
        }


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def held_for(store: pyoxigraph.Store, surface: str) -> list[HeldMention]:
    """Every held claim a label with this merge key would resolve."""
    rows = graph.records(
        store, sparql.render("facts_held_for_surface.rq", surface=sparql.Lit(surface))
    )
    return [
        HeldMention(
            claim_id=str(row["claim"]),
            surface_form=str(row["subject_surface"]),
            predicate=str(row["predicate"]),
            value=str(row["value"]),
            source_file=str(row["source_file"]),
            doc_version=str(row["doc_version"]),
            line_range=str(row["line_range"]),
            snippet=str(row["snippet"]),
        )
        for row in rows
    ]


def preview(
    store: pyoxigraph.Store,
    draft: ConceptDraft,
    *,
    validate: bool = True,
) -> Impact:
    """What publishing this draft would do to the claims already held.

    Reads the *current* graph and the draft, and changes neither. The expert is
    still typing.
    """
    impact = Impact(concept_id=draft.concept_id, surfaces=draft.surfaces())

    seen: set[str] = set()
    for surface in impact.surfaces:
        for mention in held_for(store, surface):
            if mention.claim_id not in seen:
                seen.add(mention.claim_id)
                impact.mentions.append(mention)

    impact.mentions.sort(key=lambda m: (m.source_file, m.line_range))
    impact.documents = sorted({mention.source_file for mention in impact.mentions})

    current = vocabulary.concept(store, draft.concept_id)
    if current is not None and draft.broader is not None:
        existing = current.parent.concept_id if current.parent else None
        if existing != draft.broader:
            # Reparenting moves a subtree. Which concept gains a child and which
            # loses one is the part a reviewer cannot see from the form.
            impact.children_gained = [draft.broader]
            impact.children_lost = [existing] if existing else []

    if validate:
        # Non-fatally, and on the pre-change graph: this is a warning to the
        # expert, not the gate. The gate itself runs on the post-change graph
        # inside the publish transaction, where a collision actually exists.
        impact.findings = _would_collide(store, draft)

    return impact


def _would_collide(store: pyoxigraph.Store, draft: ConceptDraft) -> list[integrity.Finding]:
    """Whether any proposed label is already taken, in the resolver's terms.

    Deliberately not a full SHACL run. The gate costs ~11s and this is called on
    every keystroke the review card debounces; what an expert needs *while
    typing* is the one answer they can act on -- "that name is already used" --
    and the structural checks can wait for Save, where they block.
    """
    proposed = [draft.pref_label, *draft.alt_labels, *draft.hidden_labels]
    wanted = {normalise(text) for text in proposed if text and normalise(text)}
    if not wanted:
        return []

    rows = vocabulary.concept_labels(store)
    pref = {
        row.concept_id: row.text
        for row in rows
        if row.kind is LabelKind.pref and (row.language or "en") == "en"
    }

    # A collision is a statement about one vocabulary, so this compares within a
    # scheme -- the draft's, or the one the concept already belongs to. Two
    # schemes may both hold a "Cylinder" without either being ambiguous.
    scheme = draft.scheme_id or next(
        (row.scheme_id for row in rows if row.concept_id == draft.concept_id), None
    )

    findings = []
    for row in rows:
        if row.concept_id == draft.concept_id or normalise(row.text) not in wanted:
            continue
        if scheme is not None and row.scheme_id != scheme:
            continue
        owner = pref.get(row.concept_id, row.concept_id)
        findings.append(
            integrity.Finding(
                concept_id=row.concept_id,
                message=f"“{row.text}” is already a term for {owner}.",
            )
        )
    return findings
