.PHONY: check check-vocab check-corpus

# Full check. Grows as the implementation lands (ruff, mypy, pytest, vitest,
# next build, cfn-lint). Today it is the vocabulary and corpus gates.
check: check-vocab check-corpus

# Validates the seed vocabulary against the SHACL shapes in vocab/shapes/
# (ADR-005), plus resolver parity, the two deliberate-gap assertions, and the
# ADR-003 OTEL mapping table executed against the recorded fixture.
check-vocab:
	uv run --quiet --with pyshacl python scripts/check_vocab.py

# Checks the document corpus against what the ADRs claim it contains: the
# registry agrees with the directory, the seeded contradictions are really
# there, the seeded gap really resolves to nothing, and the HTML document
# really exercises the lossless de-markup rule (ADR-002).
check-corpus:
	uv run --quiet --with pyshacl python scripts/check_corpus.py
