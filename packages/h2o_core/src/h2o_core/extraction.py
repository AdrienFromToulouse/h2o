"""The one step of ingestion that calls a language model.

ADR-002 step 3. Everything around it is deterministic; this is the mirror image
of the chat agent, which is a model that calls deterministic tools.

The model is given a line-numbered chunk and a forced tool call against a strict
schema. It returns facts. It does not decide what is true, what conflicts, or
what any mention refers to -- those are steps 4 and 5, and they are code.

**The verbatim-snippet gate is the load-bearing part.** A snippet must be an
exact substring of the source text. A row that fails is persisted as a Rejection
carrying the offending text, not silently dropped and not "fixed" into passing.
A fact the model knows but cannot evidence is a rejection, by design: the
guarantee this buys is that every stored fact is quotable, and recall is the
price.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from h2o_core import config
from h2o_core.chunking import Chunk, SourceText, locate

__all__ = ["ExtractedFact", "Extraction", "Rejection", "extract_chunk"]

TOOL_NAME = "record_facts"

_SYSTEM_PROMPT = """\
You read one passage from a technical document and record the facts it states.

Record only what this passage says. Never use prior knowledge about water
dispensers, filtration or any manufacturer, and never fill a gap with what is
typical for similar products. A passage that states no facts records none: an
empty list is a correct answer and a common one.

Every fact needs a `snippet` copied **character for character** from the passage.
Copy it exactly, including any spelling or spacing the passage happens to have.
A tidied quote is not a quote and will be rejected.

Do not include the `L###|` line-number prefix in a snippet. It is there so you
can see where you are, and it is not part of the document.

`subject` is the thing the fact is about, in the passage's own words -- "carbon
filter", "gas bottle", "FS-500-SPK". Do not normalise it, do not expand an
abbreviation, and do not substitute a term you think is more correct. Something
else resolves these against a controlled vocabulary afterwards, and it needs the
words the document actually used.

`predicate` is what is being stated about the subject, as a short lowercase
phrase: "replacement interval", "dispense rate", "operating pressure".

`value` is the value as written, with its unit in `unit` if it has one. Preserve
the number exactly as printed.
"""

_RETRY_NUDGE = (
    f"Record the facts by calling the {TOOL_NAME} tool. "
    "If the passage states no facts, call it with an empty list."
)


class ExtractedFact(BaseModel):
    """One row per fact, exactly the shape ADR-002 step 3 specifies."""

    subject: str = Field(description="The thing the fact is about, in the passage's own words.")
    predicate: str = Field(description="What is stated about it, as a short lowercase phrase.")
    value: str = Field(description="The value as written.")
    unit: str | None = Field(default=None, description="The unit, if the value has one.")
    snippet: str = Field(description="The sentence, copied character for character.")
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)


class DocumentExtraction(BaseModel):
    facts: list[ExtractedFact] = Field(default_factory=list)


@dataclass
class Rejection:
    """A fact the model produced that could not be evidenced.

    Kept rather than dropped so the loss is inspectable. ADR-002 trades recall
    for the guarantee that every stored fact is quotable, and a rejection record
    is what makes that trade visible instead of invisible.
    """

    source_file: str
    reason: str
    subject: str = ""
    predicate: str = ""
    value: str = ""
    snippet: str = ""

    def truncated(self) -> Rejection:
        return Rejection(
            source_file=self.source_file,
            reason=self.reason,
            subject=self.subject[:200],
            predicate=self.predicate[:200],
            value=self.value[:200],
            snippet=self.snippet[: config.MAX_REJECTION_SNIPPET],
        )


@dataclass
class Extraction:
    """What one chunk yielded: facts that passed the gate, and what did not."""

    facts: list[dict[str, Any]] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)


_runtime: Any = None


def runtime() -> Any:
    global _runtime
    if _runtime is None:
        import boto3

        _runtime = boto3.client("bedrock-runtime", region_name=config.AWS_REGION)
    return _runtime


def _tool_config() -> dict[str, Any]:
    return {
        "tools": [
            {
                "toolSpec": {
                    "name": TOOL_NAME,
                    "description": "Record the facts stated in this passage.",
                    "inputSchema": {"json": DocumentExtraction.model_json_schema()},
                }
            }
        ],
        # Forced: the model must answer through the schema, so there is no
        # free-text path for a fact to arrive on.
        "toolChoice": {"tool": {"name": TOOL_NAME}},
    }


def _numbered(chunk: Chunk) -> str:
    """Line-numbered text, so the model can see where it is in the document.

    The prefix is banned from snippets by the prompt and stripped defensively
    below, because a snippet carrying "L412|" fails the gate for a reason that
    has nothing to do with whether the quotation is honest.
    """
    return "\n".join(
        f"L{chunk.start_line + offset}|{line}"
        for offset, line in enumerate(chunk.text.splitlines())
    )


def _strip_gutter(text: str) -> str:
    return "\n".join(
        line.split("|", 1)[1]
        if line[:1] == "L" and "|" in line[:8] and line[1:8].split("|")[0].isdigit()
        else line
        for line in text.splitlines()
    )


def _converse(client: Any, messages: list[dict[str, Any]], doc_type: str) -> dict[str, Any]:
    return dict(
        client.converse(
            modelId=config.MODEL_ID,
            system=[{"text": _SYSTEM_PROMPT + f"\nThis passage is from a {doc_type}."}],
            messages=messages,
            toolConfig=_tool_config(),
            # Extraction must give the same answer for the same corpus, so it
            # runs without sampling. The chat agent deliberately does not.
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


def extract_chunk(
    chunk: Chunk,
    source: SourceText,
    *,
    doc_type: str = "technical document",
    client: Any = None,
) -> Extraction:
    """Extract facts from one chunk, keeping only what can be quoted."""
    target = client or runtime()
    messages: list[dict[str, Any]] = [{"role": "user", "content": [{"text": _numbered(chunk)}]}]

    response = _converse(target, messages, doc_type)
    payload = _tool_payload(response)

    if payload is None:
        # Retry by moving the conversation forward rather than resending: at
        # temperature 0 an identical request produces an identical answer, so a
        # plain retry would fail exactly the same way.
        messages = [
            *messages,
            {"role": "assistant", "content": [{"text": _text_of(response) or "(no answer)"}]},
            {"role": "user", "content": [{"text": _RETRY_NUDGE}]},
        ]
        response = _converse(target, messages, doc_type)
        payload = _tool_payload(response)

    if payload is None:
        return Extraction(
            rejections=[
                Rejection(
                    source_file=chunk.source_file,
                    reason="the model did not call the tool, twice",
                ).truncated()
            ]
        )

    return _gate(payload, chunk, source)


def _gate(payload: dict[str, Any], chunk: Chunk, source: SourceText) -> Extraction:
    """The verbatim-snippet gate. Reject, do not repair."""
    result = Extraction()

    try:
        parsed = DocumentExtraction.model_validate(payload)
    except Exception as error:  # noqa: BLE001 - the offending payload is the evidence
        result.rejections.append(
            Rejection(
                source_file=chunk.source_file,
                reason=f"the tool call did not match the schema: {error}",
                snippet=json.dumps(payload)[: config.MAX_REJECTION_SNIPPET],
            ).truncated()
        )
        return result

    for fact in parsed.facts:
        snippet = _strip_gutter(fact.snippet).strip()

        if not snippet:
            result.rejections.append(
                Rejection(
                    source_file=chunk.source_file,
                    reason="no snippet, so the fact cannot be evidenced",
                    subject=fact.subject,
                    predicate=fact.predicate,
                    value=fact.value,
                ).truncated()
            )
            continue

        located = locate(source, snippet)
        if located is None:
            # The fact may well be true. It is not quotable, and ADR-002 keeps
            # only what can be quoted -- so this is recorded, not repaired.
            result.rejections.append(
                Rejection(
                    source_file=chunk.source_file,
                    reason="snippet is not verbatim in the source",
                    subject=fact.subject,
                    predicate=fact.predicate,
                    value=fact.value,
                    snippet=snippet,
                ).truncated()
            )
            continue

        start, end = located
        result.facts.append(
            {
                "subject": fact.subject.strip(),
                "predicate": fact.predicate.strip(),
                "value": fact.value.strip(),
                "unit": (fact.unit or "").strip() or None,
                "snippet": snippet,
                "confidence": fact.confidence,
                "source_file": chunk.source_file,
                "line_range": f"{start}-{end}",
            }
        )

    return result
