"""Titan Text Embeddings V2, via Bedrock.

The same model in every configuration, including local development. Substituting
a cheaper local embedder would make local retrieval results mean something
different from deployed ones, which is worse than the credential requirement
(ADR-007).

Vectors come back normalised, which is why cosine similarity elsewhere is a
plain dot product and why nothing in h2o needs numpy.
"""

from __future__ import annotations

import json
from typing import Any

from h2o_core import config

__all__ = ["embed", "embed_one"]

_bedrock: Any = None


def runtime() -> Any:
    global _bedrock
    if _bedrock is None:
        import boto3

        _bedrock = boto3.client("bedrock-runtime", region_name=config.AWS_REGION)
    return _bedrock


def embed_one(text: str, *, client: Any = None) -> list[float]:
    target = client or runtime()
    response = target.invoke_model(
        modelId=config.EMBED_MODEL_ID,
        body=json.dumps(
            {
                "inputText": text,
                "dimensions": config.EMBED_DIMENSIONS,
                # Unit vectors, so every similarity in h2o is a dot product.
                "normalize": True,
            }
        ),
    )
    payload = json.loads(response["body"].read())
    return list(payload["embedding"])


def embed(texts: list[str], *, client: Any = None) -> list[list[float]]:
    """Embed many.

    Titan has no batch endpoint, so this is a loop and says so rather than
    looking like one. At corpus scale (a few hundred labels, a few dozen chunks)
    the round trips are the cost of a single ingest run.
    """
    target = client or runtime()
    return [embed_one(text, client=target) for text in texts]
