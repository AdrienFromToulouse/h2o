"""Contradiction detection: deterministic, no model, resolves nothing.

ADR-002 step 5. When two sources disagree, **both claims persist**, each with its
own source, version, line range and verbatim snippet, and a conflict flag
attaches to the group. The pipeline's job is to make the disagreement impossible
to miss, not to decide who is right.

Why not source precedence: ranking sources is a few lines of code and lets the
platform state one confident answer. It also permanently hides a real
disagreement, and the disagreement is frequently the most valuable thing in the
corpus -- it means two documents in circulation tell technicians different
things.

Why not recency: "newest wins" is right for a revised manual and wrong for two
peer sources published the same quarter, and distinguishing them needs validity
windows we do not have for every document.

**The alias map kai needed is absent here, and that absence is the project's
thesis.** kai carried 25 hand-written mappings folding `b12`, `vitamin_b_12` and
`methylcobalamin` onto one attribute name, precisely because it had no
vocabulary. h2o resolves subjects against a governed thesaurus before they reach
this module, so the resolver *is* the alias map. Only predicates still need one,
because predicates are not yet concepts.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from h2o_core import config
from h2o_core.normalize import normalise
from h2o_core.units import Quantity, parse_quantity

__all__ = ["Conflict", "ClaimLike", "detect", "predicate_key"]

#: The small residue of kai's ATTRIBUTE_ALIASES. Predicates are not concepts
#: yet, so "replacement interval" and "replacement-interval" still need folding
#: by hand. When predicates become part of the vocabulary this disappears too.
_PREDICATE_ALIASES = {
    "replacement interval": "replacement-interval",
    "replacement period": "replacement-interval",
    "service interval": "replacement-interval",
    "change interval": "replacement-interval",
    "dispense rate": "dispense-rate",
    "flow rate": "dispense-rate",
    "delivery rate": "dispense-rate",
}


def predicate_key(predicate: str) -> str:
    normalised = normalise(predicate)
    return _PREDICATE_ALIASES.get(normalised, normalised.replace(" ", "-"))


@dataclass
class ClaimLike:
    """The parts of a claim contradiction detection cares about."""

    claim_id: str
    subject: str
    predicate: str
    value: str
    unit: str | None = None
    source_file: str = ""
    doc_version: str = ""
    line_range: str = ""
    snippet: str = ""
    #: True when the mention did not resolve. Held claims still participate:
    #: two held claims can contradict each other without either being attached
    #: to a concept, so detection does not wait for the vocabulary to catch up.
    held: bool = False


@dataclass
class Conflict:
    subject: str
    predicate: str
    dimension: str | None
    claim_ids: list[str] = field(default_factory=list)
    values: list[str] = field(default_factory=list)

    @property
    def sides(self) -> int:
        return len(self.claim_ids)


def _differs(a: Quantity, b: Quantity) -> bool:
    """Whether two comparable quantities really disagree.

    A relative tolerance, because 2.40 and 2.4 are the same measurement reported
    to different precision, not a contradiction to put in front of a curator.
    """
    if a.canonical is None or b.canonical is None:
        return a.value != b.value
    if a.canonical == b.canonical:
        return False
    largest = max(abs(a.canonical), abs(b.canonical))
    if largest == 0:
        return False
    return abs(a.canonical - b.canonical) / largest > config.RELATIVE_TOLERANCE


def detect(claims: list[ClaimLike]) -> list[Conflict]:
    """Group by (subject, predicate) and flag every group that disagrees.

    An approximate value never creates a conflict on its own. "Firmware raises
    low flow below roughly 1.2 L/min" is a threshold, not a competing claim
    about the dispense rate, and treating it as one would put a false
    contradiction in the queue that a curator cannot resolve because there is
    nothing wrong.
    """
    groups: dict[tuple[str, str], list[ClaimLike]] = defaultdict(list)
    for claim in claims:
        groups[(claim.subject, predicate_key(claim.predicate))].append(claim)

    conflicts: list[Conflict] = []
    for (subject, predicate), group in sorted(groups.items()):
        if len(group) < 2:
            continue

        parsed: list[tuple[ClaimLike, Quantity | None]] = [
            (claim, parse_quantity(claim.value, claim.unit)) for claim in group
        ]
        measured = [(c, q) for c, q in parsed if q is not None and not q.approximate]
        if len(measured) < 2:
            continue

        dimensions = {q.dimension for _, q in measured}
        if len(dimensions) > 1:
            # Comparing across dimensions is an error, not a conversion
            # (ADR-002). Two claims measuring different kinds of thing are not
            # in disagreement; the extraction that produced them is wrong.
            continue

        disagreeing: list[tuple[ClaimLike, Quantity]] = []
        for index, (claim, quantity) in enumerate(measured):
            if any(
                _differs(quantity, other) for j, (_, other) in enumerate(measured) if j != index
            ):
                disagreeing.append((claim, quantity))

        if not disagreeing:
            continue

        # Every side of the group is recorded, including the ones that agree
        # with each other. Two documents in circulation both saying six months
        # is part of what the curator needs to see, and dropping the duplicate
        # would hide it.
        conflicts.append(
            Conflict(
                subject=subject,
                predicate=predicate,
                dimension=next(iter(dimensions)),
                claim_ids=[c.claim_id for c, _ in measured],
                values=[c.value for c, _ in measured],
            )
        )

    return conflicts


def relative_difference(a: str, b: str) -> Decimal | None:
    """How far apart two values are, for reporting. None if incomparable."""
    left, right = parse_quantity(a), parse_quantity(b)
    if left is None or right is None or not left.comparable_with(right):
        return None
    if left.canonical is None or right.canonical is None:
        return None
    largest = max(abs(left.canonical), abs(right.canonical))
    return abs(left.canonical - right.canonical) / largest if largest else Decimal(0)
