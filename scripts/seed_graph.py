#!/usr/bin/env python3
"""Load vocab/*.ttl into h2o:graph/published.

The reviewed Turtle files are the loaded artefact, with no serialisation step in
between that could differ from what a reviewer read (ADR-007). On Neptune the
same files go through the bulk loader with the same target graph.

This is the bulk-load path of ADR-001, and `make seed-graph` depends on
`check-vocab` so it cannot bypass the integrity gate that a console edit
enforces. Git and the console are equally safe or neither is.

By default it refuses to overwrite a dataset that already carries facts or
history, because re-seeding is a bootstrap operation and the graph is where
published curation work lives.

    uv run python scripts/seed_graph.py [--force] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pyoxigraph
from h2o_core import config, graph

ROOT = Path(__file__).resolve().parent.parent
VOCAB = ROOT / "vocab"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace the published graph even if the dataset already holds facts or history",
    )
    parser.add_argument("--dry-run", action="store_true", help="report what would be written")
    args = parser.parse_args()

    sources = {p.name: p.read_bytes() for p in sorted(VOCAB.glob("*.ttl"))}
    if not sources:
        print("no Turtle files in vocab/", file=sys.stderr)
        return 1

    seeded = graph.store_from_turtle(sources, config.PUBLISHED_GRAPH)
    print(f"parsed {len(sources)} Turtle files -> {len(seeded)} quads")

    snapshot = graph.load()
    existing = _named_graphs(snapshot.store)
    curated = {g for g in existing if g.startswith(config.HISTORY_GRAPH_PREFIX)} | (
        {config.FACTS_GRAPH} & existing
    )

    if curated and not args.force:
        print(
            f"\nrefusing to seed: the dataset already holds {sorted(curated)}.\n"
            "Seeding replaces the published graph; those graphs reference it.\n"
            "Re-run with --force if that is really what you want.",
            file=sys.stderr,
        )
        return 1

    # Everything outside the published graph is carried across untouched: facts,
    # drafts and history are not part of what the Turtle files describe.
    merged = pyoxigraph.Store()
    published = pyoxigraph.NamedNode(config.PUBLISHED_GRAPH)
    for quad in snapshot.store:
        if quad.graph_name != published:
            merged.add(quad)
    for quad in seeded:
        merged.add(quad)

    payload = graph.dump(merged)
    print(
        f"dataset: {len(merged)} quads across {sorted(_named_graphs(merged))}\n"
        f"payload: {len(payload):,} bytes, sha256 {graph.digest(payload)[:16]}"
    )

    if args.dry_run:
        print("\ndry run: nothing written")
        return 0

    graph.put(payload, snapshot.etag)
    print(f"\nwrote s3://{config.GRAPH_BUCKET}/{config.GRAPH_KEY}")
    return 0


def _named_graphs(store: pyoxigraph.Store) -> set[str]:
    return {str(q.graph_name.value) for q in store if hasattr(q.graph_name, "value")}


if __name__ == "__main__":
    raise SystemExit(main())
