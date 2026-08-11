#!/usr/bin/env python3
"""Put the demonstrator back to the state the README describes.

Without this the demo is one-shot. Once "gas bottle" has been published, the gap
is closed, the twelve mentions are live, and the loop cannot be shown again
without redeploying the whole stack -- which is precisely the "no code change,
no redeploy" claim the loop exists to make.

**It resets by recomputing, not by undoing.** The published graph goes back to
the reviewed Turtle in `vocab/`, and then every claim is re-resolved against
that vocabulary from scratch: a claim whose surface form no longer matches any
label goes back to `held` and back into the queue. Hand-editing the specific
triples a demo happened to create would leave the graph agreeing with itself
only by luck, and would drift the first time somebody published something the
script had not anticipated.

What it does not touch: the documents, the extracted claims themselves, or the
run history. Re-ingesting is a separate and much slower operation, and the
evidence a claim carries did not change just because the vocabulary did.

    uv run python scripts/demo_reset.py [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pyoxigraph
from h2o_core import config, fanout, gaps, graph, resolver, store

ROOT = Path(__file__).resolve().parent.parent
VOCAB = ROOT / "vocab"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args()

    sources = {p.name: p.read_bytes() for p in sorted(VOCAB.glob("*.ttl"))}
    if not sources:
        print("no Turtle files in vocab/", file=sys.stderr)
        return 1

    snapshot = graph.load()

    # The reviewed vocabulary, plus the facts, and nothing else. History graphs
    # are dropped rather than kept: they are versions of concepts that, after
    # this, will never have existed.
    reset = pyoxigraph.Store()
    dropped = set()
    for quad in snapshot.store:
        name = str(getattr(quad.graph_name, "value", ""))
        if name == config.PUBLISHED_GRAPH:
            continue
        if name.startswith(config.HISTORY_GRAPH_PREFIX):
            dropped.add(name)
            continue
        reset.add(quad)
    for quad in graph.store_from_turtle(sources, config.PUBLISHED_GRAPH):
        reset.add(quad)

    # Built from the reset graph, so resolution is judged against the vocabulary
    # as it is about to be, not as it was.
    index = resolver.build(reset, watermark="demo-reset")
    counts = fanout.restate_claims(reset, index)

    reopened = [
        entry.gap_id
        for entry in gaps.list_gaps(status=gaps.GapStatus.actioned, limit=500)
        if entry.gap_id
    ]

    print(
        f"vocabulary: {len(sources)} Turtle files\n"
        f"history graphs dropped: {len(dropped)}\n"
        f"claims restated: {counts['claims']} -> {counts['active']} active, "
        f"{counts['held']} held\n"
        f"gap entries to reopen: {len(reopened)}"
    )

    if args.dry_run:
        print("\ndry run: nothing written")
        return 0

    payload = graph.dump(reset)
    graph.put(payload, snapshot.etag)
    print(f"wrote s3://{config.GRAPH_BUCKET}/{config.GRAPH_KEY}")

    # The index last, and rebuilt rather than left stale: every reader resolves
    # against it, so a reset that skipped this would leave the agent answering
    # from a vocabulary that no longer exists.
    watermark = fanout.rebuild_resolver_index().get("watermark", "")
    print(f"resolver index rebuilt at {str(watermark)[:16]}")

    for gap_id in reopened:
        gaps.reopen(gap_id)
    print(f"reopened {len(reopened)} gap entries")

    store.write_run(
        {
            "run_id": f"demo-reset-{graph.digest(payload)[:12]}",
            "kind": "publish",
            "status": "succeeded",
            "started_at": fanout._now(),
            "finished_at": fanout._now(),
            "summary": f"demo reset · {counts['held']} claims held · {len(reopened)} gaps reopened",
            "counts": counts,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
