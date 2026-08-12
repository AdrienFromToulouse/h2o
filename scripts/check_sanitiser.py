"""Ask the real model whether the containment holds. Read-only, no deploy.

`make check` is offline, so it can only assert that the sanitiser behaves when a
fake says what the fake was told to say. The claim this module actually rests on
is about a real model: that one which cannot see the vocabulary will correct
spelling and language and will not substitute a synonym.

CLAUDE.md: the deployed system is a third source and it has repeatedly disagreed
with the other two. This is how that disagreement gets found on purpose rather
than in a demo. It is not part of `make check` because it costs Bedrock calls and
needs credentials.

    AWS_PROFILE=personal H2O_ENV=prod uv run python scripts/check_sanitiser.py

**The MUST-BE-EMPTY block is the gate on the whole design.** If "gas bottle" or
"limescale" starts being corrected, the sanitiser has crossed from spelling into
meaning, and the queue entries the demonstrator turns on are gone. Record what
happened; do not tune the prompt until the number is nice.
"""

from __future__ import annotations

import sys

from h2o_core import sanitise

#: (question, the alias the map must contain, what it is checking). An empty
#: `expected` means the map itself must be empty.
#:
#: Naming the exact alias rather than asking for "any correction" is not
#: pedantry: an earlier version of this file only checked that the map was
#: non-empty, and passed happily while the model returned `bouteille -> bottle`
#: and `gaz -> gas` as separate entries. Neither composes into a phrase the
#: resolver looks up -- the map was full and worth nothing.
CASES: list[tuple[str, dict[str, str], str]] = [
    ("what is the installtion process?", {"installtion": "installation"}, "a one-keystroke typo"),
    ("how often do I replce the carbon filtre", {"filtre": "filter"}, "two typos in one question"),
    (
        "wanneer moet ik het koolstoffilter vervangen",
        {"koolstoffilter": "carbon filter"},
        "Dutch: one foreign word becomes an English term",
    ),
    # The gate. Every one of these is correctly-spelled English.
    ("how do I check the gas bottle pressure", {}, "THE SEEDED GAP: a synonym, not a typo"),
    ("what causes limescale in the machine", {}, "THE SEEDED GAP: genuinely absent"),
    ("when is the carbon filter replaced", {}, "an ordinary question, already resolvable"),
    ("what is the flow rate of the FS-500-SPK", {}, "a serial number is not a misspelling"),
    ("is the water still or sparkling", {}, "short common words invite a helpful edit"),
]

#: Reported, never gated. **This is a record, not a to-do list.**
#:
#: Nova 2 Lite does not reliably keep a multi-word foreign term together, and
#: five prompt formulations did not change that. Measured, in one sitting:
#:
#:   * word by word -- `bouteille -> bottle`, `gaz -> gas`, which never compose
#:     into the phrase the resolver looks up;
#:   * the whole question as one term -- `wanneer moet ik het koolstoffilter
#:     vervangen -> when should i replace the carbon filter`;
#:   * the right phrase with a *substituted* English term -- `bouteille de gaz ->
#:     gas cylinder`, which is the synonym move the design exists to refuse;
#:   * nothing at all.
#:
#: One formulation did produce `bouteille de gaz -> gas bottle` reliably. It did
#: it by naming that exact pair in the prompt, which is the model echoing an
#: example rather than following an instruction -- and it put a term from the
#: subject matter into a prompt whose whole safety argument is that it holds
#: none. That is not a fix, and reading a pass off it would have been worse than
#: this failure.
#:
#: What this costs: a multi-word foreign term files an imprecise queue entry, or
#: several. It cannot resolve wrongly -- `retrieval._prune_aliases` refuses a
#: multi-word alias that would make a term resolve, for exactly the `gas
#: cylinder` reason -- so the corpus and the seeded gaps are unaffected. The
#: honest scope of the feature is: typos, and single-word foreign terms.
WEAK: list[tuple[str, dict[str, str], str]] = [
    (
        "quelle est la pression de la bouteille de gaz",
        {"bouteille de gaz": "gas bottle"},
        "French: a three-word term",
    ),
    (
        "cada cuanto se cambia el filtro de carbon",
        {"filtro de carbon": "carbon filter"},
        "Spanish: a three-word term",
    ),
]

FORBIDDEN = ("co2", "co₂", "cylinder", "scale")


def main() -> int:
    failures: list[str] = []
    print("h2o sanitiser check (live Bedrock)")
    print("-" * 60)

    for question, expected, why in CASES:
        result = sanitise.aliases(question)
        missing = {k: v for k, v in expected.items() if result.get(k) != v}
        ok = not result if not expected else not missing

        # Separately from the alias itself: the sanitiser must never produce a
        # vocabulary term. It cannot see the vocabulary, so if one appears it
        # came from the model's own knowledge, which is the substitution the
        # whole design exists to prevent.
        leaked = [f"{k} -> {v}" for k, v in result.items() if any(t in v for t in FORBIDDEN)]

        status = "OK  " if ok and not leaked else "FAIL"
        print(f"  {status} {why}")
        print(f"       {question}")
        print(f"       -> {result or '{}'}")
        if not ok:
            failures.append(f"{why}: expected {expected or 'no corrections'}, missing {missing}")
        if leaked:
            failures.append(f"{why}: produced a vocabulary term: {leaked}")

    print("-" * 60)
    print("  measured weakness, reported and not gated -- see WEAK above")
    for question, expected, why in WEAK:
        result = sanitise.aliases(question)
        held = all(result.get(k) == v for k, v in expected.items())
        print(f"  {'as hoped' if held else 'as recorded'}  {why}")
        print(f"       {question}")
        print(f"       -> {result or '{}'}")

    print("-" * 60)
    if failures:
        for failure in failures:
            print(f"  FAIL {failure}")
        print("FAILED: the sanitiser is correcting meaning, not spelling")
        return 1
    print("PASSED: corrections where expected, and the seeded gaps untouched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
