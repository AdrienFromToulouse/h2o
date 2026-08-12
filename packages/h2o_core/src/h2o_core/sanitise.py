"""One constrained model call that fixes how a question is *written*.

The resolver's first two stages are a dictionary lookup, so they are exact by
construction: "installtion" is not "installation" and never will be. The third
stage is Titan over label vectors, which cannot help either -- a character-level
typo shreds subword tokenisation, and `_worth_reporting` already records at
length that similarity on this vocabulary points the wrong way. So a person who
mistypes a word, or asks in French, gets an honest refusal about a term the
documents use on every page.

This closes that. It runs **only when something failed to resolve**, and it
returns an alias map -- surface form to the form worth looking up -- never a
rewritten question.

**It is blind to the vocabulary, and that is the whole design.**

The demonstrator's seeded misses are two different kinds of thing. "installtion"
is a misspelling of `Installation`. "gas bottle" is a *synonym* of `CO2
Cylinder` -- semantically correct, and exactly the altLabel ADR-001 requires a
human to add and the model only to report. A model that can see `CO2 Cylinder`
will map "gas bottle" onto it and be right to, and the headline gap entry
disappears along with the platform's one claim. A model that cannot see the
vocabulary cannot produce that mapping: "gas bottle" is correctly-spelled
English and passes through untouched.

So `aliases()` takes a question and a client. There is no parameter through
which an index, a shortlist or a concept could arrive, which makes the rule
unrepresentable-otherwise rather than merely remembered -- the same move
`resolve_instance` makes for exact-match-only instances. `test_sanitise.py`
asserts it a second way, against the request body actually sent.

Stated as the invariant: **it may change how a term is spelled or what language
it is in. It may not change what it refers to.**
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from h2o_core import config
from h2o_core.normalize import normalise

__all__ = ["Correction", "Corrections", "aliases"]

TOOL_NAME = "record_corrections"

#: Every example here is deliberately from outside the subject matter. An
#: illustration naming a real term would put that term in front of a model whose
#: entire safety argument is that it cannot see them, and "it was only an
#: example" is not a distinction the model makes. `test_sanitise` asserts no
#: label from the shipped vocabulary appears anywhere in the request -- it caught
#: this prompt doing exactly that, which is why the examples read as they do.
_SYSTEM_PROMPT = """\
You are given one question a person typed. You correct how its words are
written. You never change what they refer to.

Two kinds of correction, and no others:

1. Spelling. "recieve" is "receive", "adress" is "address", "bicyle" is
   "bicycle". Fix typos, transpositions and obvious mis-keyings.
2. Language. A term written in another language becomes the plain English term.

Correct the things the question is *about*, and leave the words holding it
together alone. Articles, prepositions, pronouns, auxiliaries and question words
are not terms. "quelle", "est", "de", "het" and "cada" need no correction even
when the rest of the question does.

**A whole term is one correction, in English word order.** Never split a term
across several entries, and never swallow the rest of the question into one.
"chaise pliante" is one entry reading "folding chair" -- not "chaise" to "chair"
and "pliante" to "folding", which is three-quarters of a translation and useless
to whatever reads this.

A small word *inside* a term belongs to the term and is not an entry of its own:
"sac de couchage" is one entry reading "sleeping bag", covering all three words
at once. Where a language writes a term as one word and English as two, that is
also one entry: "Fahrradkette" is "bicycle chain".

When you translate, give the plainest, most literal English name for the same
thing. Do not reach for the word a specialist would use.

An empty list is a correct answer and the common one. Most questions are already
correctly-spelled English and need nothing.

**A term already spelled correctly in English is never corrected.** Both kinds of
correction above are defined by a defect -- it is misspelled, or it is not
English. A term with neither is finished, however ordinary or imprecise it looks,
and however sure you are that a better word exists.

Never substitute a different term that means the same thing. "lorry" and "truck"
may well be the same object; deciding that is not your job, and something else
records it. Do not expand an abbreviation. Do not replace an everyday word with
a technical one. Do not make a term more or less specific, and do not change it
between singular and plural.

A useful test: if you would need to know anything about the subject matter to
make the change, it is not a change you may make. You are correcting typing and
language, not terminology.

You are not shown any list of accepted terms and there is no list to consult. A
word being unfamiliar to you is not evidence that it is misspelled. Leave it
alone.

`original` must be copied character for character from the question.
"""

_RETRY_NUDGE = (
    f"Record the corrections by calling the {TOOL_NAME} tool. "
    "If the question needs none, call it with an empty list."
)


class Correction(BaseModel):
    """One term, as typed and as it should be looked up."""

    original: str = Field(description="The term as it appears in the question, copied exactly.")
    corrected: str = Field(description="The same term, correctly spelled and in English.")
    kind: Literal["spelling", "translation"] = Field(
        description="Which of the two permitted corrections this is."
    )


class Corrections(BaseModel):
    corrections: list[Correction] = Field(default_factory=list)


def _tool_config() -> dict[str, Any]:
    return {
        "tools": [
            {
                "toolSpec": {
                    "name": TOOL_NAME,
                    "description": "Record how the question's terms should be spelled in English.",
                    "inputSchema": {"json": Corrections.model_json_schema()},
                }
            }
        ],
        # Forced, as extraction is: there is no free-text path for a correction
        # to arrive on, and a model narrating its reasoning into the answer is a
        # model whose output nothing downstream can bind.
        "toolChoice": {"tool": {"name": TOOL_NAME}},
    }


_runtime: Any = None


def runtime() -> Any:
    global _runtime
    if _runtime is None:
        import boto3

        _runtime = boto3.client("bedrock-runtime", region_name=config.AWS_REGION)
    return _runtime


def _converse(client: Any, messages: list[dict[str, Any]]) -> dict[str, Any]:
    return dict(
        client.converse(
            modelId=config.MODEL_ID,
            system=[{"text": _SYSTEM_PROMPT}],
            messages=messages,
            toolConfig=_tool_config(),
            # The same question must sanitise the same way on every run, or an
            # answer stops being reproducible for a reason the user cannot see.
            inferenceConfig=config.DETERMINISTIC,
        )
    )


def _tool_payload(response: dict[str, Any]) -> dict[str, Any] | None:
    for block in response.get("output", {}).get("message", {}).get("content", []):
        if "toolUse" in block and block["toolUse"].get("name") == TOOL_NAME:
            return dict(block["toolUse"].get("input") or {})
    return None


def _text_of(response: dict[str, Any]) -> str:
    return " ".join(
        block["text"]
        for block in response.get("output", {}).get("message", {}).get("content", [])
        if "text" in block
    )


def aliases(question: str, *, client: Any = None) -> dict[str, str]:
    """Normalised surface form -> normalised form to look up.

    Both sides are normalised because the only consumer is the resolver's
    dictionary, whose keys are normalised. Returning the model's raw strings
    would make every caller re-derive that and eventually one of them would do it
    differently.

    Never raises. A sanitiser that fails should cost the question its correction,
    not its answer: without this the read path would start failing for questions
    that resolve perfectly well, which is a worse outcome than the typo.
    """
    if not question.strip():
        return {}

    target = client or runtime()
    messages: list[dict[str, Any]] = [{"role": "user", "content": [{"text": question}]}]

    try:
        response = _converse(target, messages)
        payload = _tool_payload(response)

        if payload is None:
            # Move the conversation forward rather than resending: at
            # temperature 0 an identical request produces an identical answer,
            # so a plain retry fails in exactly the same way.
            messages = [
                *messages,
                {"role": "assistant", "content": [{"text": _text_of(response) or "(no answer)"}]},
                {"role": "user", "content": [{"text": _RETRY_NUDGE}]},
            ]
            payload = _tool_payload(_converse(target, messages))

        if payload is None:
            return {}

        parsed = Corrections.model_validate(payload)
    except Exception:  # noqa: BLE001 - see the docstring: this must not fail the question
        return {}

    return _as_map(parsed)


def _as_map(parsed: Corrections) -> dict[str, str]:
    """The corrections worth acting on, normalised.

    Three are dropped, and each drop is a real thing Nova does:

    A pair that normalises to the same string on both sides is the model saying
    nothing -- "CO2" to "CO₂", "filters" to "Filters". `normalise` already folds
    those, so letting it through would put an alias in the map that changes no
    lookup while making the map look like it did work.

    A side that normalises to empty names nothing and cannot be a dictionary key.

    A duplicate `original` is the model offering two readings of one term. There
    is no basis here for choosing between them, and choosing arbitrarily is the
    silent failure the cascade's collision rule exists to avoid, so neither is
    used.

    A rewrite that keeps a word and replaces another (`_is_rewrite`) is the one
    that matters and it is not hypothetical -- see that function.
    """
    seen: dict[str, str] = {}
    conflicted: set[str] = set()

    for correction in parsed.corrections:
        original = normalise(correction.original)
        corrected = normalise(correction.corrected)
        if not original or not corrected or original == corrected:
            continue
        if _is_rewrite(original, corrected):
            continue
        if original in seen and seen[original] != corrected:
            conflicted.add(original)
            continue
        seen[original] = corrected

    for key in conflicted:
        seen.pop(key, None)
    return seen


#: Edit budget by word length, for the check below only. Generous, because this
#: is a contract test on a model's output and not a matcher: it asks "is this
#: plausibly the same word, respelled", not "which label is nearest".
def _budget(word: str) -> int:
    if len(word) <= 4:
        return 1
    if len(word) <= 8:
        return 2
    return 3


def _is_rewrite(original: str, corrected: str) -> bool:
    """Whether a correction swaps a word out rather than repairing one.

    **The measured failure, and the reason this is code rather than a sentence in
    the prompt.** Asked about "how do I check the gas bottle pressure", Nova 2
    Lite returned `gas bottle -> gas cylinder`. It is a reasonable thing to
    believe and the prompt forbids it twice, and it arrived anyway. Nothing
    downstream would have caught it: `gas cylinder` is not a label either, so the
    question still misses honestly -- but the queue records the wrong term, and
    the entry the whole demonstrator turns on is split in two.

    The signal is that the phrases share a word. A shared word means this is one
    phrase being edited rather than one language becoming another, so every
    differing word must be spelling-sized. "carbon filtre" to "carbon filter"
    passes; "gas bottle" to "gas cylinder" does not.

    A genuine translation shares nothing -- "bouteille de gaz" and "gas bottle"
    have no word in common -- so it is unaffected, which is the point. Nothing
    here needs the vocabulary, and it must not: `sanitise` being unable to see
    the vocabulary is what makes this whole module safe.
    """
    left, right = original.split(), corrected.split()
    if not set(left) & set(right):
        return False
    if len(left) != len(right):
        return True
    return any(
        word_a != word_b and _distance(word_a, word_b) > _budget(word_a)
        for word_a, word_b in zip(left, right, strict=True)
    )


def _distance(a: str, b: str, /) -> int:
    """Levenshtein, iterative and unbounded. The inputs are single words."""
    if a == b:
        return 0
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        for j, char_b in enumerate(b, start=1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b))
            )
        previous = current
    return previous[-1]
