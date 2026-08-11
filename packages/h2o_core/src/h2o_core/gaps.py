"""The vocabulary gap queue: three sources, one entry, evidence attached.

ADR-004's governing distinction, which shapes everything here:

    A gap entry is a **report with a suggested attachment point**, not a
    drafted concept.

So this module counts, merges and ranks. It never writes a prefLabel, never
writes a definition, never mints an IRI, and never proposes a parent outside the
existing tree. A generated concept arrives pre-justified and is *harder to
reject* than a raw gap, which turns review into arguing with a draft rather than
exercising judgement.

Aggregation is entirely deterministic. The shortlist comes from the resolver's
own embedding stage, not from a model.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from h2o_core import config, store
from h2o_core.normalize import normalise
from h2o_core.resolver import Candidate

__all__ = [
    "GapEntry",
    "GapEvidence",
    "GapSource",
    "GapStatus",
    "GapType",
    "dismiss",
    "gap_key",
    "list_gaps",
    "read_gap",
    "record_miss",
    "should_resurface",
]

_PLURAL_KEEP = ("ss", "us", "is")


def gap_key(surface_form: str) -> str:
    """The queue's merge key: the resolver's normalisation plus a plural fold.

    **Deliberately not `normalise` alone.** ADR-004 wants "gas bottle" and "gas
    bottles" to be one queue entry. Teaching `normalise` to stem would change
    what label identity means for the published index and break the
    resolver-parity guarantee the integrity gate depends on (ADR-005), so the
    plural fold lives here and is used for merging queue entries only -- never
    for resolving a mention.
    """
    key = normalise(surface_form)
    if not key:
        return key
    head, _, last = key.rpartition(" ")
    if len(last) > 3 and last.endswith("s") and not last.endswith(_PLURAL_KEEP):
        last = last[:-1]
    return f"{head} {last}".strip()


class GapSource(StrEnum):
    """Which part of the system failed to resolve something.

    Counted separately and shown separately: a term appearing in documents,
    chat *and* telemetry is more clearly real than one appearing once, and
    collapsing the counts would lose exactly that signal.
    """

    ingestion = "ingestion"
    chat = "chat"
    telemetry = "telemetry"


class GapType(StrEnum):
    """A closed set, so the console renders each as one specific question
    rather than a generic form (ADR-004)."""

    add_alt_label = "AddAltLabel"
    new_concept = "NewConcept"
    add_mapping = "AddMapping"
    merge_duplicate = "MergeDuplicate"
    edit_definition = "EditDefinition"


class GapStatus(StrEnum):
    open = "open"
    actioned = "actioned"
    dismissed = "dismissed"


class GapEvidence(BaseModel):
    """One occurrence, verbatim, with its locator.

    Evidence is what makes the judgement cheap: an expert deciding about "gas
    bottle" sees the actual sentences and the actual user turns, not an abstract
    term.
    """

    source: GapSource
    text: str
    locator: str | None = None
    doc_version: str | None = None
    occurred_at: str
    device_ids: list[str] = Field(default_factory=list)
    occurrence_count: int = 1


class GapEntry(BaseModel):
    gap_id: str
    surface_form: str
    normalised_form: str
    gap_type: GapType = GapType.add_alt_label
    counts: dict[str, int] = Field(default_factory=dict)
    total_occurrences: int = 0
    variants: list[str] = Field(default_factory=list)
    evidence: list[GapEvidence] = Field(default_factory=list)
    suggestions: list[dict[str, Any]] = Field(default_factory=list)
    suggested_scheme: str | None = None
    status: GapStatus = GapStatus.open
    first_seen: str = ""
    last_seen: str = ""
    run_id: str | None = None
    closed_by_run_id: str | None = None
    dismissal_reason: str | None = None
    dismissed_at: str | None = None
    #: The total at the moment of dismissal. The 100x rule compares against
    #: this, which is why counts keep accruing while an entry is dismissed.
    dismissed_at_count: int = 0
    resurfaced_at: str | None = None

    @property
    def question(self) -> str:
        """The entry rendered as one specific question (ADR-004 §4)."""
        best = self.suggestions[0] if self.suggestions else None
        counts = " and ".join(
            f"{n} {source} {'mention' if n == 1 else 'mentions'}"
            for source, n in sorted(self.counts.items())
            if n
        )
        match self.gap_type:
            case GapType.add_alt_label if best:
                return f"**{self.surface_form}**: {counts}. Closest term: **{best['pref_label']}**."
            case GapType.new_concept:
                scheme = self.suggested_scheme or "no scheme"
                return f"**{self.surface_form}**: {counts}, matches nothing. Closest scheme: **{scheme}**."
            case GapType.add_mapping if best:
                return f"Signal **{self.surface_form}** looks like **{best['pref_label']}**."
            case _:
                return f"**{self.surface_form}**: {counts}."


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _evidence_id(evidence: GapEvidence) -> str:
    """Dedup key. Two ingest runs over the same document must not double-count."""
    seed = evidence.locator or f"{evidence.source}:{evidence.text[:120]}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]  # noqa: S324 - not a security boundary


def should_resurface(entry: GapEntry) -> bool:
    """A dismissed term whose volume grew by 100x comes back, once.

    Suppression has to exist or the queue never converges: a term the expert
    already judged irrelevant reappears every ingest run and the queue becomes
    noise nobody reads. But it is not permanent amnesty, because volume that
    changes by two orders of magnitude is new information (ADR-004 §5).
    """
    return (
        entry.status is GapStatus.dismissed
        and entry.resurfaced_at is None
        and entry.dismissed_at_count > 0
        and entry.total_occurrences >= entry.dismissed_at_count * config.RESURFACE_MULTIPLIER
    )


def record_miss(
    surface_form: str,
    *,
    source: GapSource,
    evidence: GapEvidence,
    gap_type: GapType = GapType.add_alt_label,
    suggestions: list[Candidate] | None = None,
    suggested_scheme: str | None = None,
    run_id: str | None = None,
    table_resource: Any = None,
) -> str:
    """Merge one miss into the queue and return its gap id.

    An UpdateItem with ADD, never a PutItem. Ingestion and chat can write the
    same entry concurrently, and a read-modify-write would lose one of the two
    counts -- which is precisely the number the console orders the queue by.
    """
    target = table_resource or store.gaps_table()
    key = gap_key(surface_form)
    now = _now()

    ranked = [
        {
            "concept_id": c.concept_id,
            "pref_label": c.pref_label,
            "score": c.score,
            "stage": "embedding",
        }
        for c in (suggestions or [])
    ]

    expression = (
        "ADD #counts.#src :one, total_occurrences :one "
        "SET last_seen = :now, "
        "    surface_form = if_not_exists(surface_form, :surface), "
        "    normalised_form = if_not_exists(normalised_form, :normalised), "
        "    first_seen = if_not_exists(first_seen, :now), "
        "    gap_type = if_not_exists(gap_type, :gap_type), "
        "    #status = if_not_exists(#status, :open), "
        "    run_id = :run_id, "
        "    suggestions = :suggestions, "
        "    suggested_scheme = :scheme, "
        "    evidence.#eid = :evidence, "
        "    variants.#variant = :one_int"
    )

    target.update_item(
        Key={"gap_id": key},
        UpdateExpression=expression,
        ExpressionAttributeNames={
            "#counts": "counts",
            "#src": source.value,
            "#status": "status",
            "#eid": _evidence_id(evidence),
            "#variant": surface_form,
        },
        ExpressionAttributeValues=store.to_dynamo(
            {
                ":one": 1,
                ":one_int": 1,
                ":now": now,
                ":surface": surface_form,
                ":normalised": key,
                ":gap_type": gap_type.value,
                ":open": GapStatus.open.value,
                ":run_id": run_id,
                ":suggestions": ranked,
                ":scheme": suggested_scheme,
                ":evidence": evidence.model_dump(mode="json"),
            }
        ),
    )
    return key


def read_gap(gap_id: str, *, table_resource: Any = None) -> GapEntry | None:
    target = table_resource or store.gaps_table()
    item = target.get_item(Key={"gap_id": gap_id}).get("Item")
    return _to_entry(item) if item else None


def list_gaps(
    status: GapStatus | None = GapStatus.open,
    limit: int = 50,
    *,
    table_resource: Any = None,
) -> list[GapEntry]:
    """Ordered by raw occurrence count, descending.

    ADR-004 rules out a learned ranking: at this scale ordering by occurrence is
    sufficient and, more importantly, explainable. A curator can see why an
    entry is at the top.
    """
    target = table_resource or store.gaps_table()
    items = target.scan().get("Items", [])
    entries = [_to_entry(item) for item in items]

    resolved = [e for e in entries if e is not None]
    for entry in resolved:
        if should_resurface(entry):
            entry.status = GapStatus.open

    if status is not None:
        resolved = [e for e in resolved if e.status is status]
    return sorted(resolved, key=lambda e: e.total_occurrences, reverse=True)[:limit]


def dismiss(
    gap_id: str,
    reason: str,
    *,
    table_resource: Any = None,
) -> GapEntry | None:
    """Suppress a surface form, recording the count it was suppressed at.

    Counts keep accruing afterwards -- dismissal is a presentation state, not an
    ingestion one -- and that is exactly what makes the 100x resurface rule
    computable.
    """
    target = table_resource or store.gaps_table()
    entry = read_gap(gap_id, table_resource=target)
    if entry is None:
        return None

    target.update_item(
        Key={"gap_id": gap_id},
        UpdateExpression=(
            "SET #status = :dismissed, dismissal_reason = :reason, "
            "    dismissed_at = :now, dismissed_at_count = :count, resurfaced_at = :none"
        ),
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues=store.to_dynamo(
            {
                ":dismissed": GapStatus.dismissed.value,
                ":reason": reason,
                ":now": _now(),
                ":count": max(entry.total_occurrences, 1),
                ":none": None,
            }
        ),
    )
    return read_gap(gap_id, table_resource=target)


def _to_entry(item: dict[str, Any]) -> GapEntry | None:
    clean = store.from_dynamo(item)
    evidence_map = clean.pop("evidence", {}) or {}
    variants = clean.pop("variants", {}) or {}

    evidence = sorted(
        (GapEvidence.model_validate(e) for e in evidence_map.values()),
        key=lambda e: e.occurred_at,
    )
    # Capped per source so one noisy document cannot crowd out the chat turns
    # that prove a term is really used by people.
    capped: list[GapEvidence] = []
    seen: dict[str, int] = {}
    for item_ in evidence:
        count = seen.get(item_.source, 0)
        if count < config.MAX_EVIDENCE_PER_SOURCE:
            capped.append(item_)
            seen[item_.source] = count + 1

    return GapEntry.model_validate(
        {
            **clean,
            "evidence": [e.model_dump(mode="json") for e in capped],
            "variants": sorted(variants),
        }
    )
