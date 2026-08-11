"""The resolver index: a build artefact, not a live query.

ADR-005 is explicit about why. Resolution runs on every user turn and every
ingested mention, so it must be an in-process dictionary lookup rather than a
graph round trip. The index is compiled from the published graph by the publish
fan-out, written to S3, and versioned with a watermark so staleness is bounded
and observable rather than possible and invisible.

The watermark is derived from the dataset's own bytes, so a rebuild that changed
nothing produces the same watermark. That turns "is the index stale?" into a
string comparison instead of a guess.

Embeddings are stored as base64 float32 rather than JSON numbers: 320 labels at
1024 dimensions is 1.3 MB packed and roughly 6 MB as text, and this object is
read on Lambda cold start.
"""

from __future__ import annotations

import base64
import json
import struct
import time
from dataclasses import dataclass, field
from typing import Any

import pyoxigraph
from botocore.exceptions import ClientError

from h2o_core import config, graph, vocabulary
from h2o_core.models import LabelKind, LabelRow
from h2o_core.normalize import normalise

__all__ = ["Candidate", "ResolverIndex", "build", "current", "load", "publish"]


@dataclass(frozen=True)
class Candidate:
    """A concept the cascade considered, with the score that put it there."""

    concept_id: str
    pref_label: str
    score: float
    kind: LabelKind = LabelKind.pref


@dataclass
class ResolverIndex:
    """Normalised label -> concept, plus label vectors for the embedding stage."""

    watermark: str
    #: Normalised text -> the concepts claiming it. A list, not a single value:
    #: the integrity gate should make collisions impossible, and if one reaches
    #: here the cascade must abstain loudly rather than pick arbitrarily.
    by_label: dict[str, list[str]] = field(default_factory=dict)
    pref_labels: dict[str, str] = field(default_factory=dict)
    schemes: dict[str, str] = field(default_factory=dict)
    kinds: dict[tuple[str, str], LabelKind] = field(default_factory=dict)
    #: Normalised label -> unit vector, for the cascade's third stage.
    vectors: dict[str, list[float]] = field(default_factory=dict)

    @property
    def concept_count(self) -> int:
        return len(self.pref_labels)

    def exact(self, normalised: str) -> list[str]:
        return self.by_label.get(normalised, [])

    def nearest(
        self, vector: list[float], k: int = 5, *, include_machine: bool = False
    ) -> list[Candidate]:
        """Cosine similarity over label vectors.

        A plain dot product because Titan V2 returns unit vectors, which is also
        why this needs no numpy: 320 labels at 1024 dimensions is a few
        milliseconds in pure Python, and a compiled dependency in a Lambda that
        already carries a Rust wheel is not worth that.

        **The machine scheme is excluded by default**, and the default is the
        safe one because every current caller needs it. Firmware's names for
        things are not candidates for a document mention -- a bulletin saying
        "gas bottle" must not resolve to `instrument.bottles_avoided` -- and
        they are certainly not attachment points to offer a curator, which is
        the leakage ADR-003 §3.1 forbids and ADR-006 §2 keeps behind a toggle.
        Instrument names still resolve: they match exactly, which is the only
        way ADR-003 maps them anyway.
        """
        scored: dict[str, tuple[float, str]] = {}
        for label, candidate in self.vectors.items():
            score = sum(a * b for a, b in zip(vector, candidate, strict=False))
            for concept_id in self.by_label.get(label, []):
                if not include_machine and self.schemes.get(concept_id) == config.MACHINE_SCHEME:
                    continue
                best = scored.get(concept_id)
                if best is None or score > best[0]:
                    scored[concept_id] = (score, label)

        ranked = sorted(scored.items(), key=lambda kv: kv[1][0], reverse=True)[:k]
        return [
            Candidate(
                concept_id=concept_id,
                pref_label=self.pref_labels.get(concept_id, concept_id),
                score=round(score, 4),
                kind=self.kinds.get((concept_id, label), LabelKind.pref),
            )
            for concept_id, (score, label) in ranked
        ]


def _pack(vector: list[float]) -> str:
    return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode("ascii")


def _unpack(packed: str) -> list[float]:
    raw = base64.b64decode(packed)
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


def build(
    store: pyoxigraph.Store,
    watermark: str,
    *,
    embed: Any = None,
) -> ResolverIndex:
    """Compile the index from the published graph.

    A label collision raises rather than resolving arbitrarily. The integrity
    gate should have caught it at publish time (ADR-005 check 2), so reaching
    here means the gate was bypassed, and building an index that silently makes
    one of two concepts unreachable is worse than failing the rebuild.
    """
    rows: list[LabelRow] = vocabulary.concept_labels(store)

    index = ResolverIndex(watermark=watermark)
    owners: dict[str, set[str]] = {}

    for row in rows:
        key = normalise(row.text)
        if not key:
            continue
        owners.setdefault(key, set()).add(row.concept_id)
        index.kinds[(row.concept_id, key)] = row.kind
        if row.kind is LabelKind.pref and (row.language or "en") == "en":
            index.pref_labels[row.concept_id] = row.text
        index.schemes[row.concept_id] = row.scheme_id

    collisions = {
        key: sorted(concept_ids)
        for key, concept_ids in owners.items()
        if len(concept_ids) > 1 and len({index.schemes[c] for c in concept_ids}) == 1
    }
    if collisions:
        raise ValueError(
            f"cannot build an index over colliding labels: {collisions}. "
            "The integrity gate exists to make this unreachable (ADR-005 check 2)."
        )

    index.by_label = {key: sorted(concept_ids) for key, concept_ids in owners.items()}

    if embed is not None:
        labels = sorted(index.by_label)
        for label, vector in zip(labels, embed(labels), strict=True):
            index.vectors[label] = vector

    return index


def _document(index: ResolverIndex) -> dict[str, Any]:
    return {
        "watermark": index.watermark,
        "by_label": index.by_label,
        "pref_labels": index.pref_labels,
        "schemes": index.schemes,
        "kinds": [[c, label, kind.value] for (c, label), kind in sorted(index.kinds.items())],
        "vectors": {label: _pack(vector) for label, vector in sorted(index.vectors.items())},
    }


def _from_document(payload: dict[str, Any]) -> ResolverIndex:
    return ResolverIndex(
        watermark=str(payload["watermark"]),
        by_label={k: list(v) for k, v in payload["by_label"].items()},
        pref_labels=dict(payload["pref_labels"]),
        schemes=dict(payload["schemes"]),
        kinds={(c, label): LabelKind(kind) for c, label, kind in payload.get("kinds", [])},
        vectors={label: _unpack(packed) for label, packed in payload.get("vectors", {}).items()},
    )


def publish(index: ResolverIndex, *, client: Any = None) -> str:
    """Write the index, then the pointer.

    The pointer is written **last** and is the only mutable object, so a torn or
    failed write leaves every reader on the previous good index rather than on
    half of a new one.
    """
    target = client or graph.s3()
    key = f"{config.INDEX_PREFIX}{index.watermark}/index.json"
    target.put_object(
        Bucket=config.GRAPH_BUCKET,
        Key=key,
        Body=json.dumps(_document(index), separators=(",", ":")).encode("utf-8"),
        ContentType="application/json",
    )
    target.put_object(
        Bucket=config.GRAPH_BUCKET,
        Key=config.INDEX_POINTER_KEY,
        Body=json.dumps({"watermark": index.watermark, "key": key}).encode("utf-8"),
        ContentType="application/json",
    )
    return index.watermark


def load(*, client: Any = None) -> ResolverIndex | None:
    """Read the published index, or None if none has been built yet."""
    target = client or graph.s3()
    try:
        pointer = json.loads(
            target.get_object(Bucket=config.GRAPH_BUCKET, Key=config.INDEX_POINTER_KEY)[
                "Body"
            ].read()
        )
        body = target.get_object(Bucket=config.GRAPH_BUCKET, Key=pointer["key"])["Body"].read()
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
            return None
        raise
    return _from_document(json.loads(body))


_cached: ResolverIndex | None = None
_cached_at: float = 0.0


def current(*, client: Any = None) -> ResolverIndex | None:
    """The index, re-read at most every INDEX_TTL_SECONDS.

    This is the staleness window ADR-005 chooses to expose rather than hide: a
    published change reaches the agent within seconds, and how many is a stated
    number rather than an emergent one.
    """
    global _cached, _cached_at
    now = time.monotonic()
    if _cached is None or now - _cached_at > config.INDEX_TTL_SECONDS:
        _cached = load(client=client)
        _cached_at = now
    return _cached


def forget_cached() -> None:
    global _cached, _cached_at
    _cached, _cached_at = None, 0.0
