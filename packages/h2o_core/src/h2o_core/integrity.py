"""The integrity gate: SHACL shapes, plus the one thing SHACL cannot do.

ADR-005 §3. The gate runs before **any** publish, from the console or from a
git bulk load, and no model is involved in it.

**SHACL is the mechanism**, not hand-written queries: the shapes live in
`vocab/shapes/skos-integrity.ttl` and are reviewed in git exactly like the
vocabulary they constrain, which is the point -- the rules become as governable
as the data. Core plus SHACL-SPARQL; Advanced Features stay off, because
``sh:rule`` derives triples and nothing here may assert a fact no human wrote.

**And one check SHACL cannot perform.** SPARQL can fold case and nothing more,
while the resolver normalises further -- NFKD, accent stripping, punctuation
removal -- so ``"CO₂ Cylinder"`` and ``"CO2 Cylinder"`` collide *for the index*
and look distinct to ``LCASE``. Parity is therefore computed in Python, over
the same ``concept_labels`` rows and through the same ``normalise`` function the
published index is built with. Anything deciding whether two labels are "the
same" has to use the function the index actually uses, not an approximation.

Findings carry ``sh:resultMessage`` verbatim. Never a code, never the query:
the console shows this sentence to a domain expert, and the wording lives
beside the rule rather than in a lookup table in the UI.

**What this costs.** pySHACL over the whole 864-triple seed vocabulary measures
~11s, against API Gateway's 29s ceiling, with a load, a serialise and a
conditional PUT still to happen. See `validate` for why it is not narrowed.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import pyoxigraph
import rdflib
from rdflib.namespace import RDF

from h2o_core import config, vocabulary
from h2o_core.models import LabelKind
from h2o_core.normalize import normalise

__all__ = ["Finding", "SHAPES_PATH", "blocking", "validate"]

SHACL = rdflib.Namespace("http://www.w3.org/ns/shacl#")


def _shapes_path() -> Path:
    """The shapes, wherever this is running from.

    ADR-005 keeps the canonical file in `vocab/shapes/` so it is reviewed in git
    like the vocabulary it constrains; the wheel force-includes that same file
    so the Lambda has it. This is one reviewed file found in two places, not two
    files -- if it were ever two, the gate the console runs and the gate `make
    check` runs could differ, which is the one thing this gate cannot afford.
    """
    packaged = Path(__file__).parent / "shapes" / "skos-integrity.ttl"
    if packaged.exists():
        return packaged
    source = Path(__file__).resolve().parents[4] / "vocab" / "shapes" / "skos-integrity.ttl"
    if source.exists():
        return source
    raise FileNotFoundError("skos-integrity.ttl is neither packaged nor in vocab/shapes/")


SHAPES_PATH = _shapes_path()


@dataclass(frozen=True)
class Finding:
    """One reason a publish is being refused, in words a curator can act on."""

    concept_id: str
    message: str
    severity: str = "violation"

    @property
    def blocks(self) -> bool:
        """Violations block a publish; warnings are reported and do not."""
        return self.severity == "violation"


def blocking(findings: list[Finding]) -> list[Finding]:
    return [finding for finding in findings if finding.blocks]


@cache
def _shapes() -> rdflib.Graph:
    """Parsed once. The shape graph is most of pySHACL's fixed cost."""
    return rdflib.Graph().parse(SHAPES_PATH, format="turtle")


def _term(node: Any) -> Any:
    if isinstance(node, pyoxigraph.NamedNode):
        return rdflib.URIRef(node.value)
    if isinstance(node, pyoxigraph.BlankNode):
        return rdflib.BNode(node.value)
    return rdflib.Literal(
        node.value,
        lang=node.language,
        datatype=rdflib.URIRef(node.datatype.value) if node.language is None else None,
    )


def as_rdflib(store: pyoxigraph.Store, graph_name: str | None = None) -> rdflib.Graph:
    """One named graph of the dataset, as the graph pySHACL wants.

    Copied rather than serialised through Turtle: a round trip would be slower
    and would introduce a parser between the data being validated and the data
    being published, which is a place for them to differ.
    """
    target = pyoxigraph.NamedNode(graph_name or config.PUBLISHED_GRAPH)
    data = rdflib.Graph()
    for quad in store.quads_for_pattern(None, None, None, target):
        data.add((_term(quad.subject), _term(quad.predicate), _term(quad.object)))
    return data


def _shacl(data: rdflib.Graph) -> list[Finding]:
    from pyshacl import validate as shacl_validate

    _, report, text = shacl_validate(
        data,
        shacl_graph=_shapes(),
        # sh:rule would derive triples, and ADR-001 makes the vocabulary
        # human-authored. Off explicitly, so enabling it is a visible decision.
        advanced=False,
        inference="none",
        # ADR-005 check 6: an orphan concept is a warning and must not block.
        allow_warnings=True,
        abort_on_first=False,
    )

    findings = []
    for result in report.subjects(RDF.type, SHACL.ValidationResult):
        node = report.value(result, SHACL.focusNode)
        message = report.value(result, SHACL.resultMessage)
        severity = report.value(result, SHACL.resultSeverity)
        findings.append(
            Finding(
                concept_id=vocabulary.slug(str(node)) if node else "",
                # Verbatim. ADR-006 renders this to a domain expert, so it is
                # the shape author's sentence and never a code or a query.
                message=str(message).strip(),
                severity="warning" if severity == SHACL.Warning else "violation",
            )
        )

    if not findings and text and "Conforms: False" in text:
        # Non-conformance the report did not describe. Surfaced rather than
        # swallowed: a gate that fails silently is worse than no gate.
        findings.append(Finding(concept_id="", message=text.strip()))
    return findings


def _parity(store: pyoxigraph.Store) -> list[Finding]:
    """The resolver's own notion of label identity, applied to the whole graph.

    A dictionary build in the milliseconds, so there is nothing to save by
    narrowing it, and a collision is a collision wherever it is. It is also the
    check that matters most: SHACL passing while the index it protects collides
    is the exact failure ADR-005 §3 exists to prevent.
    """
    owners: dict[tuple[str, str], set[str]] = defaultdict(set)
    pref: dict[str, str] = {}

    for row in vocabulary.concept_labels(store):
        key = normalise(row.text)
        if key:
            owners[(row.scheme_id, key)].add(row.concept_id)
        if row.kind is LabelKind.pref and (row.language or "en") == "en":
            pref[row.concept_id] = row.text

    findings = []
    for (_, key), concept_ids in sorted(owners.items()):
        if len(concept_ids) < 2:
            continue
        names = sorted(pref.get(concept_id, concept_id) for concept_id in concept_ids)
        findings.append(
            Finding(
                concept_id=sorted(concept_ids)[0],
                message=(
                    f"“{key}” is already a term for {names[0]}. "
                    f"Two terms in one vocabulary cannot share a name, because a search "
                    f"for it could not tell {' and '.join(names)} apart."
                ),
            )
        )
    return findings


def validate(store: pyoxigraph.Store) -> list[Finding]:
    """Every reason this graph should not be published, worst first.

    Run on the **post-change** graph, always. A label collision only exists
    after the insert, so gating beforehand would be a gate that passes while the
    index it protects collides.

    **The whole graph, not a neighbourhood.** ADR-005 anticipated focusing on
    the changed concept to stay inside API Gateway's 29s, and the measurements
    say that is not yet the trade to make: the full seed vocabulary validates in
    ~11s and one scheme in ~4.5s, so roughly 3s is fixed shape-graph cost and
    the saving is real but modest. Against that, every narrowing tried here
    manufactured violations rather than finding them -- a `broader` target that
    is merely absent reads as dangling, and stubbing the referenced concepts
    just moves the dangling one hop out. A gate that invents a reason to refuse
    a good publish is worse than a slow one, so this validates everything until
    a vocabulary large enough to need otherwise makes the publish path
    asynchronous. That is the threshold to watch, and it is a size, not a
    timeout.
    """
    findings = [*_parity(store), *_shacl(as_rdflib(store))]
    return sorted(findings, key=lambda f: (not f.blocks, f.concept_id, f.message))
