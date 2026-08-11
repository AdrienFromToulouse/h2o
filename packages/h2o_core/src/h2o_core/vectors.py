"""S3 Vectors: document chunks and concept labels, in two indexes.

Two indexes rather than one because their lifecycles differ. Document vectors
are written by ingestion; label vectors are rebuilt by the publish fan-out when
the vocabulary changes. Sharing an index would mean a vocabulary edit and a
corpus re-ingest could not proceed independently.

Chunk metadata carries ``source_file``, ``doc_type``, ``doc_version`` and the
resolved ``concept`` as **filterable** keys, with ``snippet`` and ``line_range``
as payload (ADR-002). Filtering by resolved concept is the point: it is what
makes retrieval about the right thing rather than about similar wording.
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from h2o_core import config

__all__ = ["delete_for_source", "put_chunks", "query", "s3vectors"]

_client: Any = None


def s3vectors() -> Any:
    global _client
    if _client is None:
        import boto3

        _client = boto3.client("s3vectors", region_name=config.AWS_REGION)
    return _client


def put_chunks(
    vectors: list[dict[str, Any]], *, index: str | None = None, client: Any = None
) -> int:
    """Write chunk vectors.

    Each entry is {"key", "data", "metadata"}. PutVectors caps a request, so
    this batches rather than assuming the corpus stays small.
    """
    target = client or s3vectors()
    index_name = index or config.DOCUMENT_INDEX
    written = 0
    for start in range(0, len(vectors), 100):
        batch = vectors[start : start + 100]
        target.put_vectors(
            vectorBucketName=config.VECTOR_BUCKET,
            indexName=index_name,
            vectors=[
                {
                    "key": entry["key"],
                    "data": {"float32": entry["data"]},
                    "metadata": entry.get("metadata", {}),
                }
                for entry in batch
            ],
        )
        written += len(batch)
    return written


def _filter(concepts: list[str] | None, source_file: str | None) -> dict[str, Any] | None:
    """Build a metadata filter, or None when there is nothing to narrow by.

    An empty filter object is not the same as no filter to S3 Vectors, so this
    returns None rather than {} when nothing was asked for.
    """
    clauses: list[dict[str, Any]] = []
    if concepts:
        clauses.append({"concept": {"$in": concepts}})
    if source_file:
        clauses.append({"source_file": {"$eq": source_file}})
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


def query(
    vector: list[float],
    *,
    concepts: list[str] | None = None,
    source_file: str | None = None,
    top_k: int | None = None,
    index: str | None = None,
    client: Any = None,
) -> list[dict[str, Any]]:
    """Nearest chunks, optionally narrowed to a set of resolved concepts."""
    target = client or s3vectors()
    request: dict[str, Any] = {
        "vectorBucketName": config.VECTOR_BUCKET,
        "indexName": index or config.DOCUMENT_INDEX,
        "queryVector": {"float32": vector},
        "topK": top_k or config.TOP_K,
        "returnMetadata": True,
        "returnDistance": True,
    }
    if (metadata_filter := _filter(concepts, source_file)) is not None:
        request["filter"] = metadata_filter

    response = target.query_vectors(**request)
    return [
        {
            "key": item.get("key"),
            "distance": item.get("distance"),
            **(item.get("metadata") or {}),
        }
        for item in response.get("vectors", [])
    ]


def delete_for_source(keys: list[str], *, index: str | None = None, client: Any = None) -> None:
    """Remove a document's vectors before re-indexing it.

    Re-ingestion is idempotent on content, but a document whose text changed
    leaves orphaned chunks behind, and an orphan retrieves for a question about
    a passage that no longer exists.
    """
    if not keys:
        return
    target = client or s3vectors()
    try:
        target.delete_vectors(
            vectorBucketName=config.VECTOR_BUCKET,
            indexName=index or config.DOCUMENT_INDEX,
            keys=keys,
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") not in {"NotFoundException", "404"}:
            raise
