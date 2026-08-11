"""Parameterised SPARQL templates, and the only way to bind a value into one.

ADR-001 forbids LLM-authored SPARQL: tools execute templates from this package's
``sparql/`` directory. That prohibition is only worth anything if a template
cannot be turned into an arbitrary query by what gets substituted into it, so
this module enforces one rule:

    **Placeholders bind RDF terms. A different query shape is a different file.**

There is no placeholder in a clause position anywhere in ``sparql/``. That is
why ``facts_for_concept.rq`` and ``facts_for_concept_by_predicate.rq`` are two
files rather than one file with an optional ``FILTER``: the second query is a
different question, and making it a parameter would mean a caller could change
what is being asked rather than what is being asked about.

``render`` therefore refuses a plain ``str``. Passing one is the mistake this
module exists to catch, because a string is exactly what an injected value looks
like, and a ``TypeError`` at the call site is cheaper than an escaping bug.
"""

from __future__ import annotations

import re
from decimal import Decimal
from functools import cache
from importlib import resources
from typing import Protocol

__all__ = [
    "Iri",
    "Lit",
    "Num",
    "Term",
    "TemplateError",
    "render",
    "template_names",
    "template_text",
]

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")

#: Characters RDF forbids in an IRI reference. The angle brackets matter most:
#: a closing one would end the term and let everything after it be read as
#: query syntax.
_ILLEGAL_IN_IRI = set('<>"{}|^`\\')


class TemplateError(RuntimeError):
    """A template could not be found, or was rendered with the wrong terms."""


class Term(Protocol):
    """Anything that knows how to write itself as a SPARQL term."""

    def as_sparql(self) -> str: ...


class Iri:
    """An absolute IRI, rendered as ``<...>``.

    Validated rather than escaped. An IRI containing a character RDF forbids is
    not a value that needs quoting, it is a bug or an attack, and silently
    encoding it would produce a query that runs and means something else.
    """

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str) or not value:
            raise TemplateError("an IRI must be a non-empty string")
        if any(c in _ILLEGAL_IN_IRI for c in value) or any(ord(c) < 0x21 for c in value):
            raise TemplateError(f"illegal character in IRI: {value!r}")
        if ":" not in value:
            raise TemplateError(f"IRI is not absolute: {value!r}")
        self.value = value

    def as_sparql(self) -> str:
        return f"<{self.value}>"


class Lit:
    """A plain or language-tagged literal, escaped per Turtle."""

    __slots__ = ("value", "language")

    def __init__(self, value: str, language: str | None = None) -> None:
        if not isinstance(value, str):
            raise TemplateError(f"a literal must be a string, got {type(value).__name__}")
        if language is not None and not re.fullmatch(
            r"[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*", language
        ):
            raise TemplateError(f"not a language tag: {language!r}")
        self.value = value
        self.language = language

    def as_sparql(self) -> str:
        escaped = (
            self.value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        literal = f'"{escaped}"'
        return f"{literal}@{self.language}" if self.language else literal


class Num:
    """An integer or decimal literal."""

    __slots__ = ("value",)

    def __init__(self, value: int | Decimal) -> None:
        if isinstance(value, bool) or not isinstance(value, int | Decimal):
            raise TemplateError(f"not a number: {value!r}")
        self.value = value

    def as_sparql(self) -> str:
        return str(self.value)


@cache
def template_text(name: str) -> str:
    """Read one template. Cached: a Lambda reads each of these on every call."""
    if not re.fullmatch(r"[a-z0-9_]+\.(rq|ru)", name):
        raise TemplateError(f"not a template name: {name!r}")
    try:
        return (resources.files("h2o_core") / "sparql" / name).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError) as error:
        raise TemplateError(f"no such template: {name}") from error


def template_names() -> list[str]:
    """Every template that ships with the package."""
    directory = resources.files("h2o_core") / "sparql"
    return sorted(p.name for p in directory.iterdir() if p.name.endswith((".rq", ".ru")))


def render(name: str, **terms: Term) -> str:
    """Substitute RDF terms into a template.

    Every placeholder must be supplied and every supplied term must be used, so
    a renamed placeholder fails loudly at its first call rather than producing a
    query with a literal ``{{concept}}`` in it that returns nothing.
    """
    text = template_text(name)
    wanted = set(_PLACEHOLDER.findall(text))
    given = set(terms)

    if missing := wanted - given:
        raise TemplateError(f"{name}: no term supplied for {sorted(missing)}")
    if extra := given - wanted:
        raise TemplateError(f"{name}: has no placeholder for {sorted(extra)}")

    for key, term in terms.items():
        if not hasattr(term, "as_sparql"):
            raise TemplateError(
                f"{name}: {key} must be an Iri, Lit or Num, not {type(term).__name__}. "
                "Templates bind RDF terms only; a different query shape is a different file."
            )

    return _PLACEHOLDER.sub(lambda m: terms[m.group(1)].as_sparql(), text)
