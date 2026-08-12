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

#: Tags a browser renders as a box, so their boundary is whitespace on the page.
#: Everything else -- `b`, `span`, `a`, `em`, `code` -- is inline, and a
#: separator there would break a word the reader sees whole: `<b>£7</b>.99` is
#: "£7.99", not "£7 .99". That distinction is the entire rule.
_BLOCK_TAGS = frozenset(
    """
    address article aside blockquote br caption col colgroup dd div dl dt
    fieldset figcaption figure footer form h1 h2 h3 h4 h5 h6 header hr li
    main nav ol option p pre section table tbody td tfoot th thead tr ul
    """.split()
)
_TAG_NAME = re.compile(r"</?\s*([a-zA-Z][a-zA-Z0-9]*)")
#: Runs of the separators this module introduces. Not `\s+`: a newline or a tab
#: the *document* wrote is content, and collapsing it would be a second, silent
#: departure from the rule. Only spaces are collapsed, and only spaces are added.
_SPACE_RUN = re.compile(r" {2,}")


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
    """De-markup for citation: drop tags, decode entities, invent no content.

    A reader of ``<div class="price">&pound;7.99</div>`` sees ``£7.99``, and
    that is what any extractor quotes. Comparing such a quotation against the
    raw markup rejects a *correct* citation as a fabrication and silently drops
    the fact, so HTML documents are extracted from, chunked from, and cited
    against this flattened text (ADR-002 step 2).

    **A block tag flattens to a space, and that is a correction.** It used to
    flatten to nothing, on the reading that the transform must introduce no
    character the document did not contain. The deployed console showed what
    that costs: ``<th>Supply pressure</th><td>1.5 &ndash; 6.0 bar</td>`` came
    out as ``Supply pressure1.5 – 6.0 bar``, and a reader of that table has
    never seen ``pressure1.5``. The rule was failing the standard it set for
    itself in its own first sentence.

    It also cost more than display. Chunks are built from this text and handed
    to the extractor, so the model was reading the glue too and deciding
    ``subject`` and ``value`` from it; and ``check_corpus``'s coverage check
    matches ``" carbon filter "``, which ``carbon filter6 months`` does not
    contain, so the corpus coverage figure was understated.

    The rule it is actually held to is **invent no content**, and this still
    obeys it. That rule exists as the line against OCR repair: rewriting
    ``ug``→``µg`` claims a datasheet printed a character it never printed, and
    citing that is a fabrication the verbatim gate would certify. A space at a
    box boundary claims nothing -- it renders a boundary the document really
    had, in the place the document really had it. Byte-preservation was never
    the standard in any case: ``chunking.read_source`` already strips each
    line's indentation and drops blank lines outright, which deletes characters
    the document did contain.

    Two properties this must keep, both load-bearing:

    * **Inline tags still flatten to nothing.** ``<b>£7</b>.99`` is ``£7.99``.
      A separator there would break a word the reader sees whole.
    * **It never emits two spaces in a row.** ``</th><td>`` is two adjacent
      block tags. ``chunking.locate``'s fallback searches a whitespace-collapsed
      copy and then uses that offset against an *uncollapsed* line map, so any
      run would silently shift every HTML citation's line number.
    """
    without_comments = _HTML_COMMENT.sub("", markup)
    without_tags = _HTML_TAG.sub(_tag_replacement, without_comments)
    return _SPACE_RUN.sub(" ", html.unescape(without_tags))


def _tag_replacement(match: re.Match[str]) -> str:
    """A space for a box, nothing for a span of text."""
    name = _TAG_NAME.match(match.group(0))
    return " " if name and name.group(1).lower() in _BLOCK_TAGS else ""


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
