"""Domain models.

One file, kai-style, because these are the vocabulary the rest of the package
speaks and splitting them across modules makes that vocabulary harder to read
than the code using it.

Names here follow the model, not the interface. ADR-006 maps skos:Concept to
"Term" and skos:ConceptScheme to "Vocabulary" for the console, and that mapping
lives in the frontend's label table. Renaming these to match the UI would leave
nothing able to say which SKOS construct a field came from.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

__all__ = [
    "ConceptDetail",
    "ConceptRef",
    "LabelKind",
    "LabelRow",
    "MachineSignal",
    "MappingKind",
    "SchemeRef",
    "SchemeTree",
    "TechnicalDetail",
]


class LabelKind(StrEnum):
    """Which SKOS label property a string came from.

    Carried through resolution so an answer can say *how* it matched: "Name",
    "Alternative term" or "Similar wording" in the console (ADR-006), and the
    concept chip's `matched_on` in chat.
    """

    pref = "pref"
    alt = "alt"
    hidden = "hidden"


class MappingKind(StrEnum):
    """How closely a telemetry concept aligns with a business one.

    ADR-003 keeps these distinct rather than flattening to "mapped": whether
    fault code E17 is the same thing as Low Flow is a domain judgement, and
    `close` plus a scope note records that it is a near-equivalence rather than
    pretending it is identity.
    """

    exact = "exact"
    close = "close"
    broad = "broad"


class LabelRow(BaseModel):
    """One label, as the resolver index is built from."""

    concept_id: str
    scheme_id: str
    text: str
    language: str | None = None
    kind: LabelKind


class ConceptRef(BaseModel):
    """A term referred to from somewhere else.

    Carries its definition because the parent and related pickers show it
    inline: choosing a parent is the highest-consequence edit on the review card
    and the easiest to get wrong from a label alone (ADR-006).
    """

    concept_id: str
    pref_label: str
    definition: str | None = None
    child_count: int = 0


class SchemeRef(BaseModel):
    scheme_id: str
    title: str
    description: str | None = None
    concept_count: int = 0


class MachineSignal(BaseModel):
    """A telemetry binding, shown read-only.

    The instrument name is what appears in the interface; the attribute key and
    value do not, because those are firmware's private naming and would leak
    into the box where a curator edits alternative terms (ADR-003, ADR-006).
    """

    signal: str | None = None
    unit: str | None = None
    notation: str | None = None
    match: MappingKind = MappingKind.exact
    scope_note: str | None = None


class TechnicalDetail(BaseModel):
    """Everything behind the "Show technical detail" toggle.

    Always sent, never shown by default. The toggle has to be instant, and a
    second round trip would make the technical view feel like the privileged one
    when the whole design says it is the opt-in (ADR-006).
    """

    iri: str
    scheme_iri: str
    turtle: str = ""
    otel_bindings: list[dict[str, str]] = Field(default_factory=list)


class ConceptDetail(BaseModel):
    """The review card's payload, and get_concept's answer."""

    concept_id: str
    pref_label: dict[str, str] = Field(default_factory=dict)
    definition: dict[str, str] = Field(default_factory=dict)
    alt_labels: list[str] = Field(default_factory=list)
    hidden_labels: list[str] = Field(default_factory=list)
    notation: str | None = None
    scope_note: str | None = None
    scheme: SchemeRef | None = None
    parent: ConceptRef | None = None
    children: list[ConceptRef] = Field(default_factory=list)
    related: list[ConceptRef] = Field(default_factory=list)
    machine_signals: list[MachineSignal] = Field(default_factory=list)
    version: int = 1
    modified: str | None = None
    contributor: str | None = None
    change_note: str | None = None
    # ADR-005: concepts are never hard-deleted. A retired term keeps resolving
    # and names its replacement, so the agent can say what happened to it rather
    # than reporting it missing.
    deprecated: bool = False
    replaced_by: ConceptRef | None = None
    technical: TechnicalDetail


class SchemeTree(BaseModel):
    """The console's landing page: every vocabulary and its top terms."""

    schemes: list[SchemeRef] = Field(default_factory=list)
    top_concepts: dict[str, list[ConceptRef]] = Field(default_factory=dict)
