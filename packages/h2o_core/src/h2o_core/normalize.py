"""Text normalisation: label identity, and the lossless de-markup rule.

Two unrelated jobs share this module because both answer "what does this text
really say", and both have exactly one correct definition that several callers
must agree on.

``normalise`` decides label identity. ``flatten_html`` decides what a document
says for the purpose of citing it.
"""

import html
import re
import unicodedata

__all__ = ["blank_comments", "flatten_html", "normalise", "readable"]

_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_TAG = re.compile(r"<[^>]+>")


def normalise(label: str) -> str:
    """The resolver's normalisation: fold case, strip accents and punctuation.

    This is the function the published resolver index is built with, so it is
    the only correct definition of "two labels collide". Everything that decides
    whether two labels are the same thing must call *this*, not an approximation
    of it: the integrity gate's resolver-parity check, the resolution cascade's
    exact-match stage, and the gap queue's merge key all do.

    SPARQL cannot express it, which is why the SHACL collision shape is a
    complement rather than a replacement (ADR-005 §3): ``LCASE`` alone reads
    "CO₂ Cylinder" and "CO2 Cylinder" as distinct, and the resolver does not.

    It deliberately does no stemming. The gap queue wants "gas bottle" and "gas
    bottles" merged and gets that from its own key (ADR-004 §2); teaching this
    function to stem would change what label identity means and break the
    parity guarantee the gate depends on.
    """
    text = unicodedata.normalize("NFKD", label)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.casefold()
    text = _NON_ALPHANUMERIC.sub(" ", text)
    return text.strip()


def flatten_html(markup: str) -> str:
    """Lossless de-markup: drop tags, decode entities, keep every character.

    A reader of ``<div class="price">&pound;7.99</div>`` sees ``£7.99``, and
    that is what any extractor quotes. Comparing such a quotation against the
    raw markup rejects a *correct* citation as a fabrication and silently drops
    the fact, so HTML documents are extracted from, chunked from, and cited
    against this flattened text (ADR-002 step 2).

    This is safe precisely because the transform is lossless: it introduces no
    character the document did not already contain. That is what separates it
    from OCR repair, which improves embedding quality and must never be cited,
    because citing repaired text claims a datasheet printed a character it never
    printed.
    """
    without_comments = _HTML_COMMENT.sub("", markup)
    without_tags = _HTML_TAG.sub("", without_comments)
    return html.unescape(without_tags)


def blank_comments(markup: str) -> str:
    """Replace HTML comments with their own newlines, keeping every line number.

    A comment spans lines, so it has to be removed from the whole document at
    once -- a per-line pass never sees one. But removing it outright would shift
    every following line number, and `line_range` is half of what makes a
    citation checkable, so each comment leaves its newlines behind.

    This matters more than it sounds: the spec sheet in this corpus carries a
    comment explaining the de-markup rule and quoting the very price the rule is
    about. Leave comments in and a note *about* the document becomes citable
    *as* the document.
    """
    return _HTML_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), markup)


def readable(text: str, *, is_html: bool) -> str:
    """The text a citation is checked against, for either document format."""
    return flatten_html(text) if is_html else text
