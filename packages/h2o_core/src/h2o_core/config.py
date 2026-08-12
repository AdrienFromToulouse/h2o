"""Environment-derived names, model identifiers, and every tuned constant.

Physical resource names all derive from ``H2O_ENV`` alone, so a deployment sets
one variable rather than eight (ADR-001: ``h2o-{env}-{resource}``). Each is
still individually overridable, so a test or a local run never touches a real
resource by accident.

Thresholds live here rather than at their call sites because several of them
encode a decision an ADR argued for, and a number buried in a function is a
decision nobody can find again.
"""

import os
from decimal import Decimal

H2O_ENV = os.getenv("H2O_ENV", "prod")
AWS_REGION = os.getenv("AWS_REGION", "eu-west-1")

# ---------------------------------------------------------------- the graph

#: One N-Quads object holding every named graph (ADR-007). Versioning is on, so
#: each publish is an object version as well as a history graph.
GRAPH_BUCKET = os.getenv("H2O_GRAPH_BUCKET", f"h2o-{H2O_ENV}-graph")
GRAPH_KEY = os.getenv("H2O_GRAPH_KEY", "graph/dataset.nq")
GRAPH_BACKEND = os.getenv("H2O_GRAPH_BACKEND", "oxigraph")

#: The four named graphs (ADR-005 §1). Strings rather than IRIs because they are
#: interpolated into SPARQL templates, which do their own term validation.
PUBLISHED_GRAPH = "h2o:graph/published"
DRAFT_GRAPH = "h2o:graph/draft"
FACTS_GRAPH = "h2o:graph/facts"
HISTORY_GRAPH_PREFIX = "h2o:graph/history/"

#: Concept and scheme namespaces (ADR-001 §3). Minted once, never reused.
ID_NAMESPACE = "https://vocab.h2o.example/id/"
SCHEME_NAMESPACE = "https://vocab.h2o.example/scheme/"
TELEMETRY_NAMESPACE = "https://vocab.h2o.example/telemetry/"

# ------------------------------------------------------- the resolver index

#: The index is a build artefact, not a live query (ADR-005 §5): resolution runs
#: on every user turn and every ingested mention, so it must be a dictionary
#: lookup rather than a graph round trip.
INDEX_PREFIX = "index/"
INDEX_POINTER_KEY = "index/current.json"

#: How long a warm process may serve a cached index before re-reading the
#: pointer. This is the staleness window ADR-005 exposes rather than hides.
INDEX_TTL_SECONDS = int(os.getenv("H2O_INDEX_TTL_SECONDS", "30"))

# --------------------------------------------------------------- retrieval

VECTOR_BUCKET = os.getenv("H2O_VECTOR_BUCKET", f"h2o-{H2O_ENV}-vectors")
#: Document chunks, filtered by resolved concept (ADR-002 step 2).
DOCUMENT_INDEX = os.getenv("H2O_DOCUMENT_INDEX", f"h2o-{H2O_ENV}-documents")
#: Concept-label embeddings. A separate index because its lifecycle is the
#: publish fan-out rather than ingestion (ADR-007 §4).
LABEL_INDEX = os.getenv("H2O_LABEL_INDEX", f"h2o-{H2O_ENV}-labels")

RAW_DOCS_BUCKET = os.getenv("H2O_RAW_DOCS_BUCKET", f"h2o-{H2O_ENV}-raw-docs")
TELEMETRY_BUCKET = os.getenv("H2O_TELEMETRY_BUCKET", f"h2o-{H2O_ENV}-telemetry")

EMBED_DIMENSIONS = 1024
DISTANCE_METRIC = "cosine"
TOP_K = 5

# ------------------------------------------------------- operational tables

GAPS_TABLE = os.getenv("H2O_GAPS_TABLE", f"h2o-{H2O_ENV}-vocabulary-gaps")
AUDIT_TABLE = os.getenv("H2O_AUDIT_TABLE", f"h2o-{H2O_ENV}-curation-audit")
REGISTRY_TABLE = os.getenv("H2O_REGISTRY_TABLE", f"h2o-{H2O_ENV}-document-registry")
RUNS_TABLE = os.getenv("H2O_RUNS_TABLE", f"h2o-{H2O_ENV}-runs")
FLEET_SIGNALS_TABLE = os.getenv("H2O_FLEET_SIGNALS_TABLE", f"h2o-{H2O_ENV}-fleet-signals")
PUBLISH_LOCK_TABLE = os.getenv("H2O_PUBLISH_LOCK_TABLE", f"h2o-{H2O_ENV}-publish-lock")

EVENT_BUS_NAME = os.getenv("H2O_EVENT_BUS", f"h2o-{H2O_ENV}-bus")

# ------------------------------------------------------------------ models

#: An inference profile, and it differs by deployment (ADR-001). Verify the
#: available profile at build time rather than trusting this default.
MODEL_ID = os.getenv("MODEL_ID", "eu.amazon.nova-2-lite-v1:0")
EMBED_MODEL_ID = os.getenv("EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")

#: Inference settings for the pipeline's reading tasks. Extraction must give the
#: same answer for the same corpus, so it runs without sampling. The chat agent
#: is deliberately not covered: conversation is the one place variation is
#: wanted.
DETERMINISTIC = {"temperature": 0.0, "maxTokens": 8192}

# -------------------------------------------------------------- resolution

#: Cosine similarity a label must reach before the cascade stops abstaining.
RESOLVE_THRESHOLD = float(os.getenv("H2O_RESOLVE_THRESHOLD", "0.82"))

#: How far the best candidate must beat the second. A term that is equally close
#: to two concepts is a duplicate-label signal, not a resolution (ADR-005's
#: check 2 exists because picking one arbitrarily fails silently).
RESOLVE_MARGIN = float(os.getenv("H2O_RESOLVE_MARGIN", "0.05"))

#: How many candidates a gap entry carries as its suggested attachment point.
SHORTLIST_SIZE = 5

#: The scheme holding firmware's names for things (ADR-003). Named once because
#: two rules depend on it: the console never shows it by default (ADR-006 §2),
#: and the resolver's embedding stage never ranks it -- a document mention must
#: not resolve to an instrument, and a curator must never be offered one as a
#: place to attach a business term.
MACHINE_SCHEME = "telemetry"

# ---------------------------------------------------------------- the read path

#: The longest phrase the question sweep will try to resolve, in whitespace
#: words. The vocabulary's longest label is three words, and `normalise` splits
#: a hyphen, so "Single-Use Bottles Avoided" is written three ways a reader
#: might type it and four the index might hold. Four covers all of them; five
#: would only buy n-grams no label can match.
MAX_TERM_WORDS = 4

#: How many phrases from one question may reach the embedding stage. Exact
#: matches are free -- they are a dictionary lookup -- but every remaining
#: phrase costs one Titan call, and a long question has quadratically many. The
#: cap is stated rather than emergent so a slow answer has a known ceiling; the
#: phrases dropped are the shortest, which are the least specific.
MAX_CANDIDATE_TERMS = 12

#: How far retrieval walks from a resolved concept before searching. One hop
#: reaches a term's sub-terms, its related terms and the telemetry concepts
#: mapped to it, which is the difference between asking about "filter" and
#: finding what the documents say about carbon filters.
EXPAND_DEPTH = 1

#: Off by default, which is a finding rather than an oversight. It was 0.65, on
#: the assumption that a real term scores higher against the vocabulary than a
#: function word does. Measured against the deployed index the assumption points
#: the wrong way: "limescale" -- a term the vocabulary genuinely lacks -- scores
#: 0.170, while the verb "replace" scores 0.393. What reaches a curator is now
#: decided structurally in `retrieval._worth_reporting`; this remains only as an
#: escape hatch for a deployment whose embeddings behave differently.
CHAT_GAP_FLOOR = float(os.getenv("H2O_CHAT_GAP_FLOOR", "0"))

# ------------------------------------------------------------- the gap queue

#: A dismissed surface form resurfaces when its count grows by this multiple:
#: "volume that changes by 100x is new information" (ADR-004 §5).
RESURFACE_MULTIPLIER = 100

#: Evidence is capped per source and deduplicated by locator, so one noisy
#: document cannot crowd out the chat turns that prove a term is really used.
MAX_EVIDENCE_PER_SOURCE = 5
MAX_EVIDENCE_TEXT = 400

# --------------------------------------------------------------- ingestion

#: Section-aware chunking (ADR-002 step 2).
CHUNK_TARGET_TOKENS = 400
CHUNK_MAX_TOKENS = 500
CHUNK_MIN_TOKENS = 300

MAX_REJECTION_SNIPPET = 500

#: Two values of the same dimension closer than this are the same measurement
#: reported to different precision, not a contradiction.
RELATIVE_TOLERANCE = Decimal("0.01")

# ----------------------------------------------------------------- publish

#: Conditional-PUT retries before a publish gives up and tells the curator
#: somebody else got there first (ADR-007: the collision is safe, not absent).
PUBLISH_ATTEMPTS = int(os.getenv("H2O_PUBLISH_ATTEMPTS", "3"))

#: Advisory only. The conditional PUT is what makes a concurrent publish safe;
#: this lock only makes it rare, so two curators do not both run a full SHACL
#: validation before one of them loses on the ETag.
PUBLISH_LOCK_TTL_SECONDS = 120
