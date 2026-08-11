"""Templates bind RDF terms, and nothing else.

ADR-001 forbids LLM-authored SPARQL. That prohibition is only worth something if
a template cannot be reshaped by what is substituted into it, so most of these
tests are about what `render` refuses.
"""

from decimal import Decimal

import pyoxigraph
import pytest
from h2o_core import sparql
from h2o_core.sparql import Iri, Lit, Num, TemplateError


def test_every_shipped_template_parses(store: pyoxigraph.Store) -> None:
    """A template that does not parse fails on a user's turn, not at import.

    Rendered with placeholder-shaped terms rather than executed for results:
    this asserts the SPARQL is valid, which is the part that cannot be caught
    by the tests that read data.
    """
    placeholders = {
        "concept": Iri("https://vocab.h2o.example/id/carbon-filter"),
        "scheme": Iri("https://vocab.h2o.example/scheme/equipment"),
        "predicate": Iri("https://vocab.h2o.example/id/replacement-interval"),
        "surface": Lit("gas bottle"),
        "old_parent": Iri("https://vocab.h2o.example/id/filter"),
        "new_parent": Iri("https://vocab.h2o.example/id/component"),
        "notation": Lit("carbon_filter"),
    }

    names = sparql.template_names()
    assert names, "no templates shipped with the package"

    for name in names:
        wanted = {
            k: v for k, v in placeholders.items() if "{{" + k + "}}" in sparql.template_text(name)
        }
        query = sparql.render(name, **wanted)
        assert "{{" not in query, f"{name}: unsubstituted placeholder"
        if name.endswith(".rq"):
            store.query(query)


def test_a_plain_string_is_refused() -> None:
    """The mistake this module exists to catch.

    A str is exactly what an injected value looks like, so a TypeError at the
    call site is cheaper than an escaping bug found later.
    """
    with pytest.raises(TemplateError, match="must be an Iri, Lit or Num"):
        sparql.render("concept_get.rq", concept="https://vocab.h2o.example/id/carbon-filter")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "hostile",
    [
        "https://vocab.h2o.example/id/x> } DELETE { ?s ?p ?o } WHERE { ?s ?p ?o } #",
        "https://vocab.h2o.example/id/x<y",
        'https://vocab.h2o.example/id/"quoted"',
        "https://vocab.h2o.example/id/x\ny",
        "not-absolute",
    ],
)
def test_an_iri_that_could_end_the_term_is_rejected(hostile: str) -> None:
    """Validated, not escaped.

    An IRI carrying a character RDF forbids is not a value needing quotes; it is
    a bug or an attack, and encoding it would produce a query that runs and
    means something else.
    """
    with pytest.raises(TemplateError):
        Iri(hostile)


def test_literals_are_escaped_not_rejected() -> None:
    """User wording is data and may contain anything."""
    assert Lit('he said "six months"').as_sparql() == '"he said \\"six months\\""'
    assert Lit("line\nbreak").as_sparql() == '"line\\nbreak"'
    assert Lit("Koolstoffilter", "nl").as_sparql() == '"Koolstoffilter"@nl'


def test_numbers_are_not_booleans() -> None:
    assert Num(12).as_sparql() == "12"
    assert Num(Decimal("2.4")).as_sparql() == "2.4"
    with pytest.raises(TemplateError):
        Num(True)  # type: ignore[arg-type]


def test_a_missing_or_unused_term_fails_loudly() -> None:
    """A renamed placeholder must fail at its first call.

    Silently leaving `{{concept}}` in the query would produce a valid-looking
    result set that is always empty, which reads as "no such term".
    """
    with pytest.raises(TemplateError, match="no term supplied"):
        sparql.render("concept_get.rq")
    with pytest.raises(TemplateError, match="has no placeholder"):
        sparql.render("scheme_list.rq", concept=Iri("https://vocab.h2o.example/id/x"))


def test_no_template_has_a_placeholder_in_a_clause_position() -> None:
    """The rule that makes the prohibition mechanical.

    Placeholders bind terms; a different query shape is a different file. A
    placeholder next to a keyword would mean a caller could change what is being
    asked rather than what is being asked about.
    """
    keywords = ("FILTER", "WHERE", "GRAPH", "UNION", "OPTIONAL", "SELECT", "ORDER", "GROUP")
    for name in sparql.template_names():
        for line in sparql.template_text(name).splitlines():
            if "{{" not in line or line.strip().startswith("#"):
                continue
            bare = line.upper().replace("{{", "").replace("}}", "")
            for keyword in keywords:
                assert f"{keyword} {{" not in bare, f"{name}: placeholder in a clause position"


def test_an_unknown_template_names_itself() -> None:
    with pytest.raises(TemplateError, match="no such template"):
        sparql.template_text("nope.rq")
    with pytest.raises(TemplateError, match="not a template name"):
        sparql.template_text("../../../etc/passwd")
