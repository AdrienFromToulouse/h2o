# Freeze a concept's current triples into a history graph, then clear them from
# published. The first half of ADR-005 §2's atomic update.
#
# One DELETE/INSERT/WHERE rather than two operations. The WHERE clause binds
# once and both halves apply to those bindings, so the copy cannot see a graph
# the delete has already touched -- sequencing an archive and then a clear would
# leave a window where the concept exists in neither graph, and a failure inside
# that window would lose the prior version outright.
#
# It runs against the in-memory store and persists as a single conditional S3
# write, which is what makes the whole publish land or not land: there is no
# state in between that anyone can observe.
#
# The *new* version is not inserted here. A concept has a variable number of
# labels and relations, and this repo's one rule for SPARQL is that placeholders
# bind RDF terms only -- a different query shape is a different file. Generating
# a VALUES block or an n-way INSERT DATA from a draft would be exactly the
# LLM-authored SPARQL that rule exists to prevent, so the reviewed version is
# built as quads in publish.py, the same way facts.py builds a claim.

DELETE {
  GRAPH <h2o:graph/published> { {{concept}} ?p ?o }
}
INSERT {
  GRAPH {{history}} { {{concept}} ?p ?o }
}
WHERE {
  GRAPH <h2o:graph/published> { {{concept}} ?p ?o }
}
