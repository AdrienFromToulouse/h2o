"""The document registry: step 1 of ingestion.

ADR-002 is explicit that a document is **registered and then ingested**, never
guessed at and never silently skipped. So this is the successor to a hardcoded
corpus manifest -- the same explicitness, now data -- and `data/docs/registry.json`
is what seeds it.

`doc_version` is the load-bearing field. It travels onto every claim extracted
from the document, which is what lets an answer say "per manual v3" and what
lets two documents disagree without either being wrong.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Any

from boto3.dynamodb.conditions import Key
from pydantic import BaseModel, Field

from h2o_core import store

__all__ = [
    "Authority",
    "DocFormat",
    "DocType",
    "DocumentRecord",
    "load_manifest",
    "meta_for",
    "register",
    "registered",
]


class DocType(StrEnum):
    installation_manual = "installation_manual"
    service_bulletin = "service_bulletin"
    support_faq = "support_faq"
    spec_sheet = "spec_sheet"
    support_article = "support_article"


class DocFormat(StrEnum):
    markdown = "markdown"
    html = "html"


class Authority(StrEnum):
    """Who stands behind the document.

    Recorded and shown, never used to pick a winner. ADR-002 rejects source
    precedence outright: ranking sources is a few lines of code and permanently
    hides a real disagreement behind a plausible answer.
    """

    official = "official"
    customer_facing = "customer_facing"


class DocumentRecord(BaseModel):
    filename: str
    doc_type: DocType
    doc_version: str
    issued: str
    valid_from: str | None = None
    valid_to: str | None = None
    format: DocFormat
    authority: Authority
    applies_to: list[str] = Field(default_factory=list)

    @property
    def is_html(self) -> bool:
        """HTML is cited against its de-marked-up text (ADR-002)."""
        return self.format is DocFormat.html


def load_manifest(path: Path) -> list[DocumentRecord]:
    """Read data/docs/registry.json.

    The file carries seeded_contradictions and seeded_gap alongside the
    documents; those are assertions for scripts/check_corpus.py rather than
    registry data, so they are deliberately not read here. The pipeline must
    discover the contradictions from the documents, not be told about them.
    """
    payload = json.loads(path.read_text())
    return [DocumentRecord.model_validate(entry) for entry in payload["documents"]]


def register(record: DocumentRecord, *, table_resource: Any = None) -> None:
    """Record a document and its version.

    Idempotent on (filename, doc_version), which is what makes re-ingestion
    idempotent at the top of the pipeline (ADR-002).
    """
    target = table_resource or store.registry_table()
    target.put_item(Item=store.to_dynamo(record.model_dump(mode="json")))


def registered(*, table_resource: Any = None) -> list[DocumentRecord]:
    target = table_resource or store.registry_table()
    items = target.scan().get("Items", [])
    records = [DocumentRecord.model_validate(store.from_dynamo(item)) for item in items]
    return sorted(records, key=lambda r: r.filename)


def meta_for(filename: str, *, table_resource: Any = None) -> DocumentRecord | None:
    """The newest registered version of one document."""
    target = table_resource or store.registry_table()
    response = target.query(
        KeyConditionExpression=Key("filename").eq(filename),
        ScanIndexForward=False,
        Limit=1,
    )
    items = response.get("Items", [])
    if not items:
        return None
    return DocumentRecord.model_validate(store.from_dynamo(items[0]))
