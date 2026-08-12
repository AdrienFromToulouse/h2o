"""The one model call that runs before resolution, and what it may not see.

`sanitise` exists because the resolver's first two stages are a dictionary and a
dictionary has no opinion about "installtion". It is also the only place in the
read path where a model runs before deterministic code rather than after it, so
what bounds it is worth asserting rather than describing.

The bound is that it cannot see the vocabulary. "gas bottle" and "CO2 Cylinder"
are the same object, and a model shown both will say so and be right -- which
would delete the gap entry ADR-001's central claim depends on. A model shown
only the question cannot: "gas bottle" is correctly-spelled English.
"""

from __future__ import annotations

from typing import Any

import pyoxigraph
import pytest
from fakes import FakeBedrock
from h2o_core import resolver, sanitise


def _corrections(*pairs: tuple[str, str, str]) -> dict[str, Any]:
    return {
        "corrections": [
            {"original": original, "corrected": corrected, "kind": kind}
            for original, corrected, kind in pairs
        ]
    }


# ------------------------------------------------------- the containment rule


def test_no_label_from_the_real_vocabulary_reaches_the_prompt(
    store: pyoxigraph.Store,
) -> None:
    """The rule the whole design rests on, asserted against the bytes sent.

    Total rather than a hand-written denylist, and checked against the shipped
    vocabulary: a future change that "helpfully" passes the shortlist in, to
    improve the corrections, is the change that quietly resolves "gas bottle" to
    CO₂ Cylinder and empties the queue.

    This is not hypothetical. It first failed on the prompt's own worked example,
    which named a real concept to say the model must not substitute it -- an
    illustration the model reads as vocabulary either way. The examples are now
    from outside the subject matter entirely.
    """
    index = resolver.build(store, watermark="test")
    client = FakeBedrock(_corrections(("installtion", "installation", "spelling")))

    sanitise.aliases("what is the installtion process?", client=client)
    sent = client.sent_text().lower()

    # Single words are excluded: "filter", "unit" and "machine" are ordinary
    # English as well as labels, and a prompt is allowed to contain English.
    # A multi-word label is not something a prompt says by accident.
    leaked = sorted(label for label in index.by_label if " " in label and label in sent)
    assert not leaked, f"vocabulary labels reached the sanitiser prompt: {leaked}"

    for forbidden in ("skos:", "preflabel", "altlabel", "hiddenlabel", "h2o:", "concept"):
        assert forbidden not in sent, f"the sanitiser was shown {forbidden!r}"


def test_there_is_no_parameter_an_index_could_arrive_through() -> None:
    """Unrepresentable-otherwise, the way `resolve_instance` takes no embedder.

    A rule enforced only by a prompt is a rule one refactor away from being
    untrue, so the signature is the first line of defence and this is what
    notices when it changes.
    """
    import inspect

    parameters = set(inspect.signature(sanitise.aliases).parameters)
    assert parameters == {"question", "client"}


# ------------------------------------------------------------- what it returns


def test_a_typo_becomes_an_alias() -> None:
    client = FakeBedrock(_corrections(("installtion", "Installation", "spelling")))

    assert sanitise.aliases("the installtion steps", client=client) == {
        "installtion": "installation"
    }


def test_another_language_becomes_an_alias() -> None:
    """The case edit distance could never reach: "bouteille de gaz" is not a
    misspelling of anything."""
    client = FakeBedrock(_corrections(("bouteille de gaz", "gas bottle", "translation")))

    assert sanitise.aliases("la pression de la bouteille de gaz", client=client) == {
        "bouteille de gaz": "gas bottle"
    }


def test_an_empty_list_is_a_normal_answer() -> None:
    client = FakeBedrock({"corrections": []})

    assert sanitise.aliases("how do I check the gas bottle pressure", client=client) == {}


def test_a_blank_question_never_reaches_the_model() -> None:
    client = FakeBedrock({"corrections": []})

    assert sanitise.aliases("   ", client=client) == {}
    assert client.calls == 0


# ------------------------------------------------------------ what it discards


@pytest.mark.parametrize(
    ("original", "corrected"),
    [
        ("CO2", "CO₂"),  # normalise folds the subscript: the model said nothing
        ("Filter", "filter"),  # and the case
        ("carbon-filter", "carbon filter"),  # and the punctuation
    ],
)
def test_a_correction_that_normalises_to_itself_is_dropped(original: str, corrected: str) -> None:
    """Otherwise the map looks like it did work and changes no lookup, and a
    caller reading `if alias_map:` re-sweeps the question for nothing."""
    client = FakeBedrock(_corrections((original, corrected, "spelling")))

    assert sanitise.aliases(f"about the {original}", client=client) == {}


def test_a_synonym_swap_is_dropped_however_it_is_labelled() -> None:
    """**The failure this guard was written for, as it actually happened.**

    Asked "how do I check the gas bottle pressure", the real Nova 2 Lite
    returned `gas bottle -> gas cylinder`. The prompt forbids substituting a
    synonym twice over and it arrived anyway, which is why this is code.

    Nothing downstream would have noticed: `gas cylinder` is not a label either,
    so the question still misses honestly -- and the queue records the wrong
    term, splitting the entry the whole demonstrator turns on. The two phrases
    share a word, so this is one phrase being edited rather than one language
    becoming another, and the edits must be spelling-sized.
    """
    client = FakeBedrock(_corrections(("gas bottle", "gas cylinder", "spelling")))

    assert sanitise.aliases("how do I check the gas bottle pressure", client=client) == {}


@pytest.mark.parametrize("kind", ["spelling", "translation"])
def test_the_guard_does_not_care_what_the_model_called_it(kind: str) -> None:
    """A rewrite relabelled as a translation is the same rewrite. The check is
    structural, so the model cannot route around it by choosing a different
    `kind`."""
    client = FakeBedrock(_corrections(("gas bottle", "gas cylinder", kind)))

    assert sanitise.aliases("the gas bottle", client=client) == {}


@pytest.mark.parametrize(
    ("original", "corrected"),
    [
        ("carbon filtre", "carbon filter"),  # one word repaired, one kept
        ("bouteille de gaz", "gas bottle"),  # a translation shares nothing
        ("koolstoffilter", "carbon filter"),  # one word becomes two
        ("installtion", "installation"),  # a single word has nothing to share
    ],
)
def test_the_guard_leaves_a_real_correction_alone(original: str, corrected: str) -> None:
    """Each of these came back from the live model. The guard has to be narrow
    enough to let the feature work, or the multilingual case dies with the
    synonym case."""
    client = FakeBedrock(_corrections((original, corrected, "spelling")))

    assert sanitise.aliases(f"about the {original}", client=client) == {original: corrected}


def test_a_phrase_that_loses_a_word_is_a_rewrite() -> None:
    """ "gas bottle" to "bottle" keeps a word and drops another. Whatever that is,
    it is not a spelling correction."""
    client = FakeBedrock(_corrections(("gas bottle", "bottle", "spelling")))

    assert sanitise.aliases("the gas bottle", client=client) == {}


def test_a_side_that_normalises_to_nothing_is_dropped() -> None:
    client = FakeBedrock(_corrections(("—", "filter", "spelling"), ("filtre", "•", "spelling")))

    assert sanitise.aliases("a question", client=client) == {}


def test_two_readings_of_one_term_use_neither() -> None:
    """Choosing arbitrarily between them is the silent failure the cascade's
    collision rule exists to prevent, and there is no basis here for choosing."""
    client = FakeBedrock(
        _corrections(
            ("filtre", "filter", "translation"),
            ("filtre", "carbon filter", "translation"),
        )
    )

    assert sanitise.aliases("le filtre", client=client) == {}


# ------------------------------------------------------------- how it fails


def test_prose_is_retried_by_moving_the_conversation_forward() -> None:
    """At temperature 0 a resent request fails identically, so the retry adds a
    turn instead. Asserted on the request, because "it retried" and "it retried
    usefully" are different claims."""
    client = FakeBedrock(
        "Sure! The question looks fine to me.",
        _corrections(("presure", "pressure", "spelling")),
    )

    assert sanitise.aliases("the presure", client=client) == {"presure": "pressure"}
    assert client.calls == 2
    assert len(client.requests[1]["messages"]) == 3, "the retry resent the same one turn"


def test_prose_twice_costs_the_correction_and_not_the_answer() -> None:
    client = FakeBedrock("I am not going to call that tool.")

    assert sanitise.aliases("the presure", client=client) == {}
    assert client.calls == 2


def test_a_bedrock_failure_costs_the_correction_and_not_the_answer() -> None:
    """The read path is what matters. A question that resolves perfectly well
    must not start failing because a sanitiser could not reach Bedrock."""
    client = FakeBedrock(RuntimeError("Bedrock is having a day"))

    assert sanitise.aliases("what is the carbon filter interval", client=client) == {}


def test_a_payload_that_does_not_match_the_schema_is_not_guessed_at() -> None:
    client = FakeBedrock({"corrections": [{"original": "presure"}]})

    assert sanitise.aliases("the presure", client=client) == {}
