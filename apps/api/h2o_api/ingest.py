"""The asynchronous half of POST /ingest.

The router returns a run id inside API Gateway's 29 seconds; this runs for as
long as the corpus takes, under the function's own 900 second timeout, and its
whole job is to own the run record while `h2o_core.pipeline` does the work.

**The run row is written here and never by the pipeline.** `ingest_corpus`
returns counts and touches no runs table, which is what lets test_pipeline.py
stay a library test with no DynamoDB in it at all.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from h2o_core import config, embeddings, fanout, graph, pipeline, registry, resolver, store
from h2o_core.registry import DocumentRecord

#: The manifest travels with the corpus rather than with the code: the bucket is
#: the source of truth for what has been made available to ingest.
MANIFEST_KEY = "registry.json"


def new_run_id() -> str:
    return f"ingest-{uuid.uuid4().hex[:12]}"


def now() -> str:
    """One definition of when, shared by the route and the worker.

    Every run row carries this as `started_at`, which is the runs table's
    by-kind sort key -- and DynamoDB refuses an empty string in a secondary
    index key, so "not started yet" has to be a real time rather than a blank.
    Queued *is* a time: it is when somebody asked.
    """
    return datetime.now(UTC).isoformat(timespec="seconds")


def _read(key: str, *, client: Any = None) -> str:
    s3 = client or graph.s3()
    body = s3.get_object(Bucket=config.RAW_DOCS_BUCKET, Key=key)["Body"].read()
    return body.decode("utf-8") if isinstance(body, bytes) else str(body)


def documents(
    only: list[str] | None = None, *, client: Any = None
) -> list[tuple[DocumentRecord, str]]:
    """Every registered document and its text, or the named subset.

    A name that is not in the manifest raises rather than being skipped: ADR-002
    is explicit that documents are registered and then ingested, never guessed
    at, and quietly ingesting nothing is the failure that looks like success.
    """
    records = registry.parse_manifest(_read(MANIFEST_KEY, client=client))
    if only:
        known = {record.filename for record in records}
        if unknown := sorted(set(only) - known):
            raise ValueError(f"not in the document registry: {', '.join(unknown)}")
        records = [record for record in records if record.filename in only]
    return [(record, _read(record.filename, client=client)) for record in records]


def run(
    run_id: str,
    *,
    only: list[str] | None = None,
    s3_client: Any = None,
    runs: Any = None,
    gaps: Any = None,
    registry_table: Any = None,
    vectors_client: Any = None,
) -> dict[str, Any]:
    """Ingest the corpus and record what happened, whether or not it worked."""
    started = now()
    envelope: dict[str, Any] = {
        "run_id": run_id,
        "kind": "ingest",
        "status": "running",
        "started_at": started,
    }
    store.write_run(envelope, table_resource=runs)

    try:
        corpus = documents(only, client=s3_client)

        # The first ingest of a fresh deployment runs before anything has ever
        # been published, so there is no index to be current. Building one here
        # rather than failing keeps the bootstrap path from needing a separate
        # ceremony -- and it is the same function fan-out step 1 calls.
        index = resolver.current()
        if index is None:
            index = fanout.rebuild_resolver_index()

        snapshot = graph.load(client=s3_client)
        result = pipeline.ingest_corpus(
            corpus,
            snapshot.store,
            index=index,
            # Passed explicitly. The pipeline defaults this to None, which is
            # right for a library test and silently disables both the cascade's
            # embedding stage and every vector write, so a production run that
            # forgot it would resolve nothing and hold everything.
            embed_one=embeddings.embed_one,
            run_id=run_id,
            gaps_table=gaps,
            registry_table=registry_table or store.registry_table(),
            vectors_client=vectors_client,
        )

        # The same conditional PUT the publish transaction uses. Ingestion and
        # publish both write the one dataset object, so an ingest that started
        # before a publish landed must lose here rather than overwrite it -- a
        # silently clobbered publish is undetectable afterwards.
        payload = graph.dump(snapshot.store)
        graph.put(payload, snapshot.etag, client=s3_client)

        for position, step in enumerate(result.as_steps(), start=1):
            store.write_step(run_id, position, step, table_resource=runs)

        envelope |= {
            "status": "succeeded",
            "finished_at": now(),
            "counts": result.as_counts(),
            "watermark": graph.digest(payload),
        }
        store.write_run(envelope, table_resource=runs)
        return envelope

    except Exception as failure:  # noqa: BLE001 - the run record is the report
        # Recorded and swallowed, deliberately. This runs as an asynchronous
        # self-invocation, and Lambda retries those twice on an unhandled error
        # -- which would re-ingest the corpus. Claims are content-addressed so
        # they would survive that, but gap counts are not: they would treble,
        # and the count is the number ADR-004 orders the queue by. The run row
        # is the report, and /runs is where a failure is meant to be seen.
        envelope |= {
            "status": "failed",
            "finished_at": now(),
            # The message reaches a curator through /runs, so it says what went
            # wrong rather than naming a Python type.
            "error": str(failure) or failure.__class__.__name__,
        }
        store.write_run(envelope, table_resource=runs)
        return envelope
