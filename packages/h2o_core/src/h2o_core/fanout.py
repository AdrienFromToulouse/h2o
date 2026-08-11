"""The consequences of a publish, as four ordered steps (ADR-005 §5).

Publishing is the beginning of the work, not the end of it. A changed label
means nothing until the resolver index is rebuilt, held claims are re-resolved
and affected documents are re-indexed, and this module is those steps.

**None of these functions takes a client, a context, or an environment
argument.** They are called from a Step Functions Task in Lambda and from
BackgroundTasks locally, and the dispatcher in apps/api decides *who calls*
them, never *what they do*. The moment one of them needs to know where it is
running, the dispatcher stops being a dispatcher.

Only `rebuild_resolver_index` exists so far. It is here rather than in the
ingest worker because it is fan-out step 1, and ingestion needs it for a
different reason: the first ingest runs before any publish has ever happened,
so there is no index yet to be current.
"""

from __future__ import annotations

from h2o_core import embeddings, graph, resolver

__all__ = ["rebuild_resolver_index"]


def rebuild_resolver_index() -> resolver.ResolverIndex:
    """Recompile normalised labels to concept ids and publish the artefact.

    ADR-005 puts this first and lets its failure abort the run: without it
    nothing else in the fan-out matters, because every later step resolves
    against the index this produces.

    The watermark is the dataset's own sha256, so rebuilding an unchanged graph
    yields the same watermark and staleness is diagnosable by comparing two
    strings rather than by guessing.
    """
    snapshot = graph.load()
    watermark = graph.digest(graph.dump(snapshot.store))
    index = resolver.build(snapshot.store, watermark, embed=embeddings.embed)
    resolver.publish(index)
    resolver.forget_cached()
    return index
