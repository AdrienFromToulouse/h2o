"""Answering a question from the vocabulary, and saying so when it cannot.

The mirror image of ingestion. Ingestion is a deterministic pipeline that calls
a model in exactly one step; this is a model that composes prose from exactly
one deterministic retrieval. Neither lets the model decide what is true.

**The data parts are derived from the retrieval, never from the model.** The
`concept` and `conflict` events below are built from what `h2o_core.retrieval`
returned, so a chip cannot be conjured by the model and -- just as important --
cannot be suppressed by it. ADR-006 §5 names both parts; the `miss` chip is the
one that matters most, because README step 1 is "finds nothing, says so
honestly" and without a chip an honest failure is indistinguishable from a bad
answer.

**Why this returns events rather than streaming them.** ADR-006 specifies SSE,
and the wire contract here is exactly those events in exactly that order. But
API Gateway's REST integration buffers a Lambda's response whole, so streaming
through it would deliver one lump at the end while claiming to stream. The
events are therefore returned as an array now and streamed unchanged when the
agent moves to AgentCore, which supports response streaming. The contract is the
part that had to be right first: `apps/frontend/lib/agent-events.ts` reads the
same shapes either way.
"""

from __future__ import annotations

import json
from typing import Any

from h2o_core import config, embeddings, graph, resolver, retrieval
from h2o_core.retrieval import Answer

#: ADR-001's grounding rules, given to the model as its system prompt.
#:
#: These are constraints on *what may be said*, not style guidance, which is why
#: they are absolute and why the interesting ones are prohibitions. The model
#: receives evidence that is already quotable -- the verbatim gate refused
#: anything that was not -- so its only remaining job is to not exceed it.
GROUNDING_RULES = """You answer questions about water dispensers for the people who service them.

You are given passages and claims that were retrieved from the company's own
documents. Every one carries the file it came from and the exact words it used.

Rules, in order of importance:

1. Answer ONLY from the material provided. If it does not contain the answer,
   say plainly that the documents do not say. Never fill a gap with general
   knowledge about water dispensers, however confident you are.
2. When sources disagree, give EVERY side, each attributed to its document and
   version: "The 2023 manual says six months; the 2024 bulletin says four." You
   may say which source looks more authoritative. You may NOT present one and
   quietly drop the others.
3. Attribute what you say. Name the document, and the version when there is one.
4. If a term in the question was not recognised, say so in plain words and do
   not guess what it meant. Do not silently substitute a term you think is
   close.
5. Write for a technician: short, direct, no preamble. No markdown headings.
6. Never mention vocabularies, concepts, schemes, identifiers, resolution,
   scores or any of the machinery. The person asking does not have one."""


def _bedrock() -> Any:
    import boto3

    return boto3.client("bedrock-runtime", region_name=config.AWS_REGION)


def _evidence(answer: Answer) -> str:
    """The retrieval, as the only thing the model is allowed to read."""
    lines: list[str] = []

    if answer.passages:
        lines.append("PASSAGES FROM THE DOCUMENTS:")
        for passage in answer.passages:
            where = f"{passage.source_file}"
            if getattr(passage, "line_range", None):
                where += f":{passage.line_range}"
            lines.append(f"- [{where}] {passage.snippet}")

    if answer.claims:
        lines.append("\nFACTS EXTRACTED FROM THE DOCUMENTS:")
        for claim in answer.claims:
            unit = f" {claim.get('unit')}" if claim.get("unit") else ""
            lines.append(
                f"- {claim.get('predicate')}: {claim.get('value')}{unit} "
                f"[{claim.get('source_file')} {claim.get('doc_version', '')}]"
                f' "{claim.get("snippet", "")}"'
            )

    if answer.disagreements:
        lines.append("\nTHESE SOURCES DISAGREE — report every side:")
        for disagreement in answer.disagreements:
            for side in disagreement.claims:
                lines.append(
                    f"- {disagreement.predicate}: {side.get('value')} "
                    f"[{side.get('source_file')} {side.get('doc_version', '')}]"
                )

    if answer.unresolved:
        lines.append("\nTERMS IN THE QUESTION THAT THE DOCUMENTS DO NOT USE:")
        for term in answer.unresolved:
            lines.append(f'- "{term.surface_form}"')

    return "\n".join(lines) if lines else "(nothing was retrieved)"


def compose(answer: Answer, *, client: Any = None) -> str:
    """Turn retrieval into prose, or say honestly that there is none.

    The no-material case does not reach the model at all. Asking it to write
    "the documents do not say" is asking it to be disciplined about the one
    thing it is worst at, when the deterministic answer is already known.
    """
    if not answer.understood:
        terms = ", ".join(f"“{term.surface_form}”" for term in answer.unresolved) or "that"
        return (
            f"I don't have anything on {terms} — it isn't a term these documents use, "
            f"so I'd rather say so than guess."
        )

    if not answer.passages and not answer.claims:
        return "I found the right area in the documents, but nothing in them answers this."

    response = (client or _bedrock()).converse(
        modelId=config.MODEL_ID,
        system=[{"text": GROUNDING_RULES}],
        messages=[
            {
                "role": "user",
                "content": [{"text": f"QUESTION: {answer.question}\n\n{_evidence(answer)}"}],
            }
        ],
        inferenceConfig=config.DETERMINISTIC,
    )
    parts = response["output"]["message"]["content"]
    return "".join(part.get("text", "") for part in parts).strip()


def events(answer: Answer, text: str) -> list[dict[str, Any]]:
    """The ADR-006 wire contract, in order.

    Derived from the retrieval and the composed text, so every chip corresponds
    to something that really happened during the lookup.
    """
    stream: list[dict[str, Any]] = []

    for term in answer.resolved:
        stream.append(
            {
                "type": "concept",
                "item": {
                    "origin": "resolution",
                    "surface_form": term.surface_form,
                    "concept_id": term.concept_id,
                    "pref_label": term.pref_label,
                    "matched_on": term.stage,
                    "score": term.score,
                },
            }
        )

    for miss in answer.unresolved:
        # The honest failure, made visible. Without this a "not found in the
        # sources" answer looks exactly like a bad answer.
        stream.append(
            {
                "type": "concept",
                "item": {
                    "origin": "miss",
                    "surface_form": miss.surface_form,
                    "concept_id": None,
                    "pref_label": None,
                    "near_terms": [s.get("pref_label") for s in miss.suggestions[:3]],
                    "gap_id": miss.gap_id,
                },
            }
        )

    for disagreement in answer.disagreements:
        stream.append(
            {
                "type": "conflict",
                "item": {
                    "concept_id": disagreement.subject_concept,
                    "predicate": disagreement.predicate,
                    # Every side, always. ADR-002 forbids presenting one and
                    # dropping the others, and the UI can only honour that if
                    # the wire carries all of them.
                    "claims": disagreement.claims,
                },
            }
        )

    stream.append({"type": "text_delta", "text": text})
    stream.append({"type": "done"})
    return stream


def ask(
    question: str,
    *,
    session_id: str = "",
    bedrock: Any = None,
    gaps_table: Any = None,
    vectors_client: Any = None,
    s3_client: Any = None,
) -> dict[str, Any]:
    """One turn: retrieve deterministically, compose, and report what happened."""
    index = resolver.current(client=s3_client)
    snapshot = graph.cached(client=s3_client)

    answer = retrieval.retrieve(
        question,
        snapshot.store,
        index=index,
        embed_one=embeddings.embed_one,
        # The chat miss is written from inside retrieval, on the abstain branch
        # of the resolver -- the same function ingestion and the telemetry mapper
        # go through. There is deliberately no tool the model could call to
        # record one (ADR-004 §1).
        record_gaps=True,
        gaps_table=gaps_table,
        vectors_client=vectors_client,
        run_id=session_id or None,
    )

    text = compose(answer, client=bedrock)
    return {"events": events(answer, text), "watermark": index.watermark if index else None}


def as_sse(payload: dict[str, Any]) -> str:
    """The same events, in SSE framing, for when the transport can stream."""
    return "".join(f"data: {json.dumps(event)}\n\n" for event in payload["events"])
