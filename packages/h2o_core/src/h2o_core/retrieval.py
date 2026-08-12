"""The read path: resolve the question, expand, then search what is left.

**Graph first, vectors second.** The GraphRAG literature's usual shape is to
embed the question, find the nearest chunks, and traverse from whatever they
mention. That order needs entities induced from the corpus, and ADR-001 rejects
inducing them. h2o has the opposite asset: a human-authored SKOS vocabulary that
every chunk was already resolved against, carried on the vector as a filterable
``concept`` key (ADR-002 step 2, deliberately written *after* step 4 so the key
is true). So the vocabulary narrows the search before the search runs, rather
than the search discovering a vocabulary afterwards.

Concretely this is metadata filtering, and the reason it is safe here is that
the filter is not guessed. The usual failure of the pattern is a model deciding
what to filter on and being subtly wrong; here the filter comes from the same
deterministic cascade that ingestion used, which abstains loudly rather than
picking. A question and a document that mention the same thing are filtered to
the same concept because the *same function* said so.

**Abstention is not a dead end.** A phrase that resolves to nothing is a
vocabulary gap with a question as its evidence (ADR-004, ``GapSource.chat``).
The system cannot answer, and says so, and the fact that somebody asked becomes
a curation item. That is the loop the README describes, entered from the read
side.

**Nothing here adjudicates.** Claims that contradict each other come back as a
disagreement carrying both cited values. ADR-002: the system records what is
evidenced, not what is true.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pyoxigraph
from pydantic import BaseModel, Field

from h2o_core import config, facts, gaps, resolve, sanitise, vectors, vocabulary
from h2o_core.gaps import GapEvidence, GapSource, GapType
from h2o_core.normalize import normalise
from h2o_core.resolve import Stage
from h2o_core.resolver import Candidate, ResolverIndex

__all__ = [
    "Answer",
    "Disagreement",
    "Passage",
    "ResolvedTerm",
    "UnresolvedTerm",
    "candidate_terms",
    "expand",
    "retrieve",
]


class ResolvedTerm(BaseModel):
    """A phrase from the question that the vocabulary recognised."""

    surface_form: str
    concept_id: str
    pref_label: str
    #: Which cascade stage matched, so an answer can say *how* it understood the
    #: question rather than only what it concluded.
    stage: str
    score: float


class UnresolvedTerm(BaseModel):
    """A phrase the vocabulary did not recognise, and what it nearly was."""

    surface_form: str
    suggestions: list[dict[str, Any]] = Field(default_factory=list)
    #: The queue entry this question contributed to, or None when the phrase was
    #: too far from anything to be worth a curator's time.
    gap_id: str | None = None


class Passage(BaseModel):
    """One retrieved chunk, with the locator that makes it checkable."""

    source_file: str
    line_range: str
    #: source_file:line_range. Present on every passage, because a passage
    #: without one is a quotation nobody can verify.
    locator: str
    snippet: str
    doc_type: str | None = None
    doc_version: str | None = None
    concepts: list[str] = Field(default_factory=list)
    distance: float | None = None


class Disagreement(BaseModel):
    """Two or more claims about the same thing that do not agree.

    Presented, never resolved. The claims are the whole group, each with its own
    citation, so a reader sees that the corpus disagrees rather than seeing
    whichever value happened to be stored first.
    """

    subject_concept: str
    predicate: str
    claims: list[dict[str, Any]]


class Answer(BaseModel):
    """Everything retrieval found, and everything it could not understand.

    Not prose. Composing an answer from this is the agent's job (ADR-001); what
    this guarantees is that every passage and every claim in it is quotable and
    located, because the verbatim gate already refused anything that was not.
    """

    question: str
    resolved: list[ResolvedTerm] = Field(default_factory=list)
    unresolved: list[UnresolvedTerm] = Field(default_factory=list)
    #: The resolved concepts plus everything one hop away. This is exactly the
    #: filter the vector search ran with, exposed so an answer can explain what
    #: it looked at.
    searched_concepts: list[str] = Field(default_factory=list)
    passages: list[Passage] = Field(default_factory=list)
    claims: list[dict[str, Any]] = Field(default_factory=list)
    disagreements: list[Disagreement] = Field(default_factory=list)

    @property
    def understood(self) -> bool:
        """Whether any part of the question reached the vocabulary at all."""
        return bool(self.resolved)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --------------------------------------------------------------- the sweep


def _words(question: str) -> list[str]:
    """The question's words, without the punctuation attached to them.

    "carbon filter?" is the same term as "carbon filter", and a gap entry
    called "pressure?" is a queue row nobody can act on. Stripped at the edges
    only, so a hyphen or an apostrophe inside a word survives -- "point-of-use"
    is one word and `normalise` is what decides how it folds.
    """
    stripped = (word.strip(".,;:!?()[]{}\"'“”‘’") for word in question.split())
    return [word for word in stripped if word]


def candidate_terms(
    question: str,
    *,
    index: ResolverIndex | None,
    embed: Callable[[str], list[float]] | None = None,
    aliases: dict[str, str] | None = None,
) -> list[resolve.Resolution]:
    """Every phrase in the question the cascade had a verdict about.

    Two passes, because they cost different amounts. The first sweeps the
    question left to right, longest phrase first, against the index's exact map
    -- a dictionary lookup, so it is free and every phrase can be tried. A
    matched phrase consumes its words, which is what makes "carbon filter"
    resolve as one term rather than as "carbon" and "filter" separately.

    The second pass takes the words nothing matched and runs the full cascade,
    embedding stage included, over at most ``MAX_CANDIDATE_TERMS`` phrases in
    longest-first order. That bound is real: each phrase is one Titan call, and
    a question has quadratically many phrases.

    Returns the cascade's own verdicts rather than a decision about them, for
    the same reason ``resolve.resolve`` is pure: a curator's search box wants
    the verdict without the queue write.

    ``aliases`` is threaded to both passes and changes only what the dictionary
    is asked for. It matters most in pass 1: "installtion process" is close to
    nothing, but the sweep then tries "installtion" alone, the alias makes it
    exact, and the word is consumed -- so the leftover never reaches pass 2 and
    never costs a Titan call.
    """
    if index is None:
        return []

    words = _words(question)
    verdicts: list[resolve.Resolution] = []
    consumed = [False] * len(words)

    # Pass 1: exact only. No embedder is passed, so the cascade stops after its
    # second stage -- including on a collision, which abstains loudly here
    # exactly as it does during ingestion.
    position = 0
    while position < len(words):
        for length in range(min(config.MAX_TERM_WORDS, len(words) - position), 0, -1):
            phrase = " ".join(words[position : position + length])
            verdict = resolve.resolve(phrase, index=index, aliases=aliases)
            if verdict.stage is Stage.exact:
                verdicts.append(verdict)
                for offset in range(length):
                    consumed[position + offset] = True
                position += length
                break
        else:
            position += 1

    if embed is None:
        return verdicts

    # Pass 2: what is left, longest first, capped. Longest first is both the
    # useful order -- a longer phrase is a more specific term -- and the order
    # that makes the cap drop the least informative candidates.
    #
    # An aliased span goes ahead of all of them, because length is a guess about
    # where a term ends and an alias is evidence. This is the third form of the
    # straddling bug: "quelle est la pression de la bouteille de gaz" offered
    # "pression de la bouteille" first -- four words, content at both edges, so
    # nothing above rejects it -- consumed the middle of the question and left
    # "gaz" on its own. The sanitiser had already named "bouteille de gaz" as one
    # term; the sweep just was not listening.
    candidates: list[tuple[int, int, int, str]] = []
    for length in range(config.MAX_TERM_WORDS, 0, -1):
        for start in range(len(words) - length + 1):
            span = range(start, start + length)
            if any(consumed[i] for i in span):
                continue
            # A candidate may not begin or end with a function word. Judging
            # that afterwards was not enough: the sweep takes the longest window
            # first, so "how do I check the gas bottle pressure" offered
            # "I check the gas" before it ever offered "gas bottle pressure",
            # consumed the words, and queued an entry about nothing. Shaping the
            # candidates is what makes the longest-first order pick out phrases
            # rather than windows.
            edges = (normalise(words[start]), normalise(words[start + length - 1]))
            if any(edge in _FUNCTION_WORDS for edge in edges):
                continue
            phrase = " ".join(words[start : start + length])
            normalised = normalise(phrase)
            if normalised:
                rank = 0 if normalised in (aliases or {}) else 1
                candidates.append((rank, start, length, phrase))

    candidates.sort(key=lambda c: (c[0], -c[2], c[1]))

    seen = 0
    for _rank, start, length, phrase in candidates:
        if seen >= config.MAX_CANDIDATE_TERMS:
            break
        if any(consumed[i] for i in range(start, start + length)):
            continue
        seen += 1
        verdict = resolve.resolve(phrase, index=index, embed=embed, aliases=aliases)
        # A phrase is consumed when the cascade matched it, and also when it
        # came close enough to be worth reporting as a gap. Without the second
        # case "the gas bottle", "gas bottle" and "gas" would each open an
        # entry for one mention of one thing.
        if verdict.matched or _worth_reporting(verdict):
            verdicts.append(verdict)
            for offset in range(length):
                consumed[start + offset] = True

    return verdicts


#: Words that cannot make a phrase worth queueing on their own: interrogatives,
#: auxiliaries, determiners, prepositions, and the handful of generic verbs a
#: question about equipment is built from. A gap entry is a *thing the documents
#: should have a word for*, so an action is not a candidate even when it scores
#: well -- "check" is a question about Inspection, not a missing term.
_FUNCTION_WORDS_EN = frozenset(
    """
    a an the this that these those my our your its it they them he she we i you
    is are was were be been being am do does did done doing have has had having
    can could shall should will would may might must
    how what when where why which who whom whose whether
    to of in on at by for from with without into onto about over under after
    before during between through as and or but not no nor if then than so
    check checking find finding get getting know knowing see seeing tell telling
    use using need needing want wanting make making take taking put putting
    replace replacing replaced change changing changed clean cleaning fix fixing
    often long much many always ever again also just still now soon
    please help me my mine ok okay
    """.split()
)

#: The same job in the languages a question actually arrives in. English-only was
#: not a choice so much as an oversight, and the read path's own test found it:
#: "quelle est la pression de la bouteille de gaz" offered "quelle est la
#: pression" as a candidate, because to an English list those are four content
#: words. That is the "I check the gas" bug again in another language, and it
#: reaches a curator as a queue row made of somebody's grammar.
#:
#: Deliberately not exhaustive and not a language detector. These are the closed
#: classes -- articles, pronouns, auxiliaries, prepositions, interrogatives -- of
#: the languages the vocabulary carries labels in plus the ones the sanitiser is
#: most often asked to translate from. A word missing here costs one noisy queue
#: row, which is the failure mode worth having.
_FUNCTION_WORDS_OTHER = frozenset(
    """
    le la les un une des du de d au aux ce cet cette ces mon ma mes votre vos
    est sont etait je tu il elle nous vous ils elles qui que quoi quel
    quelle quels quelles comment pourquoi quand ou dois faire fait
    dans sur sous avec sans pour par entre apres avant plus moins tres
    de het een deze dit die dat mijn uw zijn haar ik jij hij zij wij
    is zijn was waren heb heeft hebben kan kunnen moet moeten zal zullen
    hoe wat waar waarom wanneer welke wie doe doen maak maken
    in op aan van voor met zonder naar bij over onder tussen na
    der die das ein eine einen dem den des ich du er sie wir ihr
    ist sind war waren habe hat haben kann koennen muss muessen wird werden
    wie was wo warum wann welche wer mache machen
    im auf an von fuer mit ohne nach bei ueber unter zwischen
    el los las uno una unos unas mi mis su sus este esta estos estas
    es son era eran tengo tiene tienen puedo pueden debe deben
    como que donde por cuando cual quien hago hacer
    en sobre bajo con sin para entre despues antes
    """.split()
)

#: What the two rules below actually consult. Kept as a union rather than as one
#: list because the English half is argued for word by word above and the rest is
#: closed-class grammar; merging them would lose which is which.
_FUNCTION_WORDS = _FUNCTION_WORDS_EN | _FUNCTION_WORDS_OTHER

#: Nouns that name no thing in particular. `_FUNCTION_WORDS` covers the verbs a
#: question is built from but not these, so "what is the installation process"
#: resolved "installation" in pass 1, consumed it, and queued the stranded
#: "process" as a term a curator was invited to add a label for. Kept separate
#: from the function words because these are only ever judged as a whole phrase:
#: a generic noun *inside* a term is fine, and "point of use" would not survive
#: being trimmed the way `content_phrase` trims the edges.
#:
#: Safe to list a word the vocabulary uses. Exact match runs first, so "part"
#: resolves to Component in pass 1 and never reaches this.
_GENERIC_NOUNS = frozenset(
    """
    process processes procedure procedures method methods
    thing things stuff item items way ways
    system systems information info data
    detail details step steps question questions
    """.split()
)


def content_phrase(phrase: str) -> str:
    """The phrase with its function words trimmed from both ends.

    "how do I check the gas bottle" becomes "gas bottle". Trimming rather than
    filtering, because a function word *inside* a term is part of it: "rate of
    flow" survives whole, where filtering would leave "rate flow".

    The edges are still absolute, and "point of use" is the case that shows it:
    `use` is a listed generic verb, so the phrase trims to "point". A term whose
    last word is a function word cannot be reported through here. That is a real
    limit rather than a bug -- it is the same rule that stops "I check the gas"
    becoming a queue row -- and it costs nothing today because such a term
    resolves at the exact stage, which runs first and never reaches this.
    """
    words = [word for word in normalise(phrase).split() if word]
    while words and words[0] in _FUNCTION_WORDS:
        words.pop(0)
    while words and words[-1] in _FUNCTION_WORDS:
        words.pop()
    return " ".join(words)


def _worth_reporting(verdict: resolve.Resolution) -> bool:
    """Whether an abstention is worth a curator's attention.

    **Structural, not a threshold, and that is a correction.** This used to
    require the top shortlist score to clear ``CHAT_GAP_FLOOR``, on the
    assumption that a real term scores higher against the vocabulary than a
    function word does. Measured against the deployed index, that assumption is
    simply false -- these are the top scores for one question's phrases:

        0.496  "check"       -> Inspection
        0.395  "do"          -> Reverse Osmosis
        0.366  "how"         -> Fault
        0.348  "gas bottle"  -> Single-Use Bottles Avoided

    The term the whole demonstrator is built on ranks *below* three function
    words. Titan embeds two-word labels, so it returns lexical similarity, and
    lexical similarity to a short label is not evidence of aboutness. No
    threshold on this number can separate the cases, so the filter asks a
    different question: does the phrase, with its function words trimmed away,
    still name something?

    The score is not used at all by default, and that is the strongest form of
    the same finding. "limescale" -- the term ADR-003's second loop is built on,
    and one the vocabulary genuinely lacks -- scores 0.170, while the verb
    "replace" scores 0.393. The number is not weak evidence of aboutness; on
    this vocabulary it points the wrong way. `CHAT_GAP_FLOOR` therefore defaults
    to 0 and stays only as an escape hatch for a deployment whose embeddings
    behave differently.
    """
    if verdict.matched:
        return False
    # The *corrected* form, when there is one. Found live: "how often do I replce
    # the carbon filtre" queued an entry for `replce`. The sanitiser had already
    # said it meant "replace", which is a listed function word and exactly the
    # kind of phrase this refuses -- but the check was reading the typed form,
    # where the typo hid the function word from the list. Judging the words the
    # lookup was actually made with is both simpler and right: a term is worth a
    # curator's attention because of what it means, not how it was spelled.
    trimmed = content_phrase(verdict.lookup or verdict.surface_form)
    if not trimmed:
        return False
    # A phrase made only of generic words names nothing a label could be added
    # for. "process" is the one the console showed, with Fault, Dispenser and
    # Component offered as its closest terms -- three unrelated concepts, because
    # the question the score answers is not the question the chip was asking.
    if all(word in _FUNCTION_WORDS or word in _GENERIC_NOUNS for word in trimmed.split()):
        return False
    if not config.CHAT_GAP_FLOOR:
        return True
    return bool(verdict.shortlist) and verdict.shortlist[0].score >= config.CHAT_GAP_FLOOR


# ------------------------------------------------------------- the expansion


def expand(
    store: pyoxigraph.Store,
    concept_ids: list[str],
    *,
    depth: int | None = None,
) -> list[str]:
    """The seed concepts plus everything within ``depth`` hops.

    Iterative with a visited set rather than a SPARQL property path, for the
    reason ``concept_children.rq`` already gives: a path cannot report how far
    it travelled, and iteration terminates even if a cycle reaches the graph.
    """
    limit = config.EXPAND_DEPTH if depth is None else depth
    seen = set(concept_ids)
    frontier = list(concept_ids)

    for _ in range(max(limit, 0)):
        nxt: list[str] = []
        for concept_id in frontier:
            for neighbour in vocabulary.neighbours(store, concept_id):
                if neighbour not in seen:
                    seen.add(neighbour)
                    nxt.append(neighbour)
        if not nxt:
            break
        frontier = nxt

    return sorted(seen)


# ----------------------------------------------------------------- the claims


def _merge_claim_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per claim, with its conflicts collected.

    ``facts_for_concept.rq`` returns the conflict flag alongside the value on
    purpose, which means a claim conflicting with two others arrives as two
    rows. Folding them here keeps that guarantee -- the flag never travels
    separately from the value -- without making a caller count rows.
    """
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        claim_id = str(row["claim"])
        record = merged.get(claim_id)
        if record is None:
            record = {k: v for k, v in row.items() if k != "conflicts_with"}
            record["conflicts_with"] = []
            merged[claim_id] = record
        if row.get("conflicts_with"):
            record["conflicts_with"].append(str(row["conflicts_with"]))

    for record in merged.values():
        record["conflicts_with"] = sorted(set(record["conflicts_with"]))
    return sorted(
        merged.values(), key=lambda r: (r["predicate"], r["source_file"], r["line_range"])
    )


def _disagreements(claims: list[dict[str, Any]]) -> list[Disagreement]:
    """Group flagged claims into the sets that disagree.

    ``facts.flag_conflict`` writes the relation symmetrically and completely, so
    every member of a group names every other and ``{claim} | conflicts`` is the
    same set whichever member it is computed from. That is what makes grouping a
    dictionary lookup rather than a union-find.
    """
    by_id = {str(claim["claim"]): claim for claim in claims}
    groups: dict[frozenset[str], list[dict[str, Any]]] = {}

    for claim_id, claim in by_id.items():
        if not claim["conflicts_with"]:
            continue
        key = frozenset({claim_id, *claim["conflicts_with"]})
        groups.setdefault(key, [])

    found: list[Disagreement] = []
    for key in groups:
        members = [by_id[claim_id] for claim_id in sorted(key) if claim_id in by_id]
        if len(members) < 2:
            # The counterpart is about a concept this question did not reach.
            # Reporting one side alone would read as a settled value, which is
            # the exact failure the symmetric flag exists to prevent.
            continue
        found.append(
            Disagreement(
                subject_concept=str(members[0].get("subject_concept") or ""),
                predicate=str(members[0]["predicate"]),
                claims=members,
            )
        )
    return sorted(found, key=lambda d: (d.subject_concept, d.predicate))


# ------------------------------------------------------------------ retrieval


def retrieve(
    question: str,
    store: pyoxigraph.Store,
    *,
    index: ResolverIndex | None,
    embed_one: Callable[[str], list[float]] | None = None,
    top_k: int | None = None,
    depth: int | None = None,
    record_gaps: bool = True,
    gaps_table: Any = None,
    vectors_client: Any = None,
    run_id: str | None = None,
    sanitise_client: Any = None,
) -> Answer:
    """Answer material for one question. Reads the graph; writes only gaps.

    **When nothing resolves, nothing is retrieved.** The obvious fallback --
    search the vectors unfiltered -- is the plain vector search this design
    exists to replace, and it would answer confidently from wording alone about
    a term the vocabulary has never heard of. Returning the unresolved phrases
    instead is the honest result, and it is also the useful one: those phrases
    are now in the queue.

    **The second attempt.** A phrase that failed may have failed over spelling or
    language rather than over meaning, so a miss buys one `sanitise` call and a
    re-sweep. A question whose every phrase resolved never pays for it; a French
    question resolves nothing and always does, which is the case it exists for.

    Cost ceiling on the miss path: pass 2 runs twice, so up to
    ``2 * MAX_CANDIDATE_TERMS`` Titan calls, plus one Nova call. The happy path
    is unchanged.
    """
    answer = Answer(question=question)

    verdicts = candidate_terms(question, index=index, embed=embed_one)

    # `not verdicts` is the important half and it is easy to leave out: a
    # question where nothing at all was recognised produces no verdicts rather
    # than unmatched ones, and that is the strongest case for a second attempt,
    # not the absence of one. Without it a wholly mistyped or wholly non-English
    # question was the one thing the sanitiser never ran on.
    if index is not None and (not verdicts or any(not v.matched for v in verdicts)):
        alias_map = _prune_aliases(sanitise.aliases(question, client=sanitise_client), index)
        if alias_map:
            verdicts = candidate_terms(question, index=index, embed=embed_one, aliases=alias_map)

    # Gap writes happen here, after the second attempt and never before it, so a
    # phrase the sanitiser rescues leaves no queue row behind.
    for verdict in verdicts:
        if verdict.matched and verdict.concept_id:
            answer.resolved.append(
                ResolvedTerm(
                    surface_form=verdict.surface_form,
                    concept_id=verdict.concept_id,
                    pref_label=(index.pref_labels.get(verdict.concept_id, verdict.concept_id))
                    if index
                    else verdict.concept_id,
                    stage=verdict.stage.value,
                    score=verdict.score,
                )
            )
            continue

        gap_id = None
        if record_gaps:
            gap_id = _record_chat_miss(question, verdict, gaps_table=gaps_table, run_id=run_id)
        answer.unresolved.append(
            UnresolvedTerm(
                surface_form=verdict.surface_form,
                suggestions=[_as_suggestion(c) for c in verdict.shortlist],
                gap_id=gap_id,
            )
        )

    if not answer.resolved:
        return answer

    seeds = [term.concept_id for term in answer.resolved]
    answer.searched_concepts = expand(store, seeds, depth=depth)

    if embed_one is not None:
        answer.passages = [
            _as_passage(hit)
            for hit in vectors.query(
                embed_one(question),
                concepts=answer.searched_concepts,
                top_k=top_k,
                client=vectors_client,
            )
        ]

    rows: list[dict[str, Any]] = []
    for concept_id in answer.searched_concepts:
        for row in facts.read_claims(store, concept_id=concept_id):
            rows.append({**row, "subject_concept": concept_id})

    answer.claims = _merge_claim_rows(rows)
    answer.disagreements = _disagreements(answer.claims)
    return answer


def _prune_aliases(alias_map: dict[str, str], index: ResolverIndex) -> dict[str, str]:
    """The two checks that need the vocabulary, which `sanitise` deliberately cannot see.

    **The original must not already be a label.** A word the index knows is not a
    misspelling, whatever a model thinks, and correcting one can only take away a
    term that was already resolving.

    **A multi-word original may not be the thing that makes a term resolve.**
    This one is narrow and it is bought with a measurement. Asked in French about
    the gas bottle, Nova 2 Lite returned `bouteille de gaz -> gas cylinder`: the
    right phrase, and a *substituted* English term rather than a translated one.
    It happened to be harmless -- "gas cylinder" is not a label either, so the
    question still missed -- but the same move landing on "CO2 cylinder" would
    have resolved, and the gap entry this demonstrator turns on would be gone.

    Single-word aliases are exempt, and the asymmetry is the point. "installtion"
    and "koolstoffilter" are the cases that must resolve, and a one-word original
    leaves no room for the phrase-level reinterpretation that was observed. A
    multi-word alias loses nothing by being refused a resolution: what the
    multilingual case actually needs is for the *miss* to be filed under the
    English form, and a miss is exactly what this leaves intact.
    """
    kept: dict[str, str] = {}
    for original, corrected in alias_map.items():
        if index.exact(original):
            continue
        if " " in original and index.exact(corrected):
            continue
        kept[original] = corrected
    return kept


def _as_suggestion(candidate: Candidate) -> dict[str, Any]:
    return {
        "concept_id": candidate.concept_id,
        "pref_label": candidate.pref_label,
        "score": candidate.score,
    }


def _as_passage(hit: dict[str, Any]) -> Passage:
    source_file = str(hit.get("source_file") or "")
    line_range = str(hit.get("line_range") or "")
    concepts = hit.get("concept") or []
    return Passage(
        source_file=source_file,
        line_range=line_range,
        locator=f"{source_file}:{line_range}",
        snippet=str(hit.get("snippet") or ""),
        doc_type=hit.get("doc_type"),
        doc_version=hit.get("doc_version"),
        concepts=[c for c in concepts if c != "_none"],
        distance=hit.get("distance"),
    )


def _record_chat_miss(
    question: str,
    verdict: resolve.Resolution,
    *,
    gaps_table: Any = None,
    run_id: str | None = None,
) -> str | None:
    """Put one unrecognised phrase in the queue, with the question as evidence.

    The locator is derived from the question rather than from the turn, so the
    same question asked twice is one piece of evidence and two occurrences.
    Evidence answers "what did somebody actually say"; the count answers "how
    often", and conflating them would let one person asking repeatedly look like
    a term in wide use.

    When a sanitiser alias applied, the entry merges on the English form and the
    typed words become its variant. The evidence text stays the verbatim
    question either way -- a curator judging whether a term is really used needs
    to read what somebody actually asked, not a tidied version of it.
    """
    if not _worth_reporting(verdict):
        return None

    digest = hashlib.sha1(question.strip().encode("utf-8")).hexdigest()[:12]  # noqa: S324 - a dedup key
    return gaps.record_miss(
        verdict.surface_form,
        source=GapSource.chat,
        evidence=GapEvidence(
            source=GapSource.chat,
            text=question.strip()[: config.MAX_EVIDENCE_TEXT],
            locator=f"chat:{digest}",
            occurred_at=_now(),
        ),
        gap_type=GapType.add_alt_label,
        suggestions=verdict.shortlist,
        run_id=run_id,
        merge_as=verdict.lookup if verdict.aliased else None,
        table_resource=gaps_table,
    )
