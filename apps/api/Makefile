# Invoked by `sam build` (Metadata.BuildMethod: makefile) inside a scratch copy
# of CodeUri, so it can only use files that live in apps/api.
#
# --python-platform pins manylinux aarch64 wheels regardless of the build host,
# so no Docker container is needed to produce a Lambda-compatible bundle.
#
# It is manylinux_2_28, NOT manylinux2014. manylinux2014 is an alias for glibc
# 2.17, and pyoxigraph publishes only manylinux_2_28 wheels: against a 2_17
# target uv rejects the wheel, silently falls back to the sdist, and tries to
# compile Rust. On a macOS host that can succeed and put a Mach-O object inside
# an arm64 artefact, which fails at import time in Lambda with a message about
# neither. Lambda's python3.13 runtime is Amazon Linux 2023 (glibc 2.34), so
# 2_28 is correct and 2_17 is both wrong and needlessly strict.
#
# The three assertions below are what make this safe to ship as a zip rather
# than a container image. Each catches a different way the above goes wrong.

build-ApiFunction:
	uv pip install -r requirements.txt --target "$(ARTIFACTS_DIR)" \
		--python-platform aarch64-manylinux_2_28 --python-version 3.13 --link-mode copy
	uv pip install vendor/*.whl --no-deps --target "$(ARTIFACTS_DIR)" --link-mode copy
	cp -r h2o_api "$(ARTIFACTS_DIR)/h2o_api"
	@echo "--- bundle assertions ---"
	@du -sm "$(ARTIFACTS_DIR)" | awk '{ printf "  size: %sMB\n", $$1; \
		if ($$1 > 240) { print "  FAIL: exceeds Lambda'\''s 250MB unzipped limit"; exit 1 } }'
	@ls "$(ARTIFACTS_DIR)"/pyoxigraph/*.so >/dev/null 2>&1 \
		|| { echo "  FAIL: no pyoxigraph .so in the bundle -- the sdist fallback fired"; exit 1; }
	@file "$(ARTIFACTS_DIR)"/pyoxigraph/*.so | grep -q 'ARM aarch64' \
		|| { echo "  FAIL: pyoxigraph .so is not aarch64 -- wrong --python-platform"; \
		     file "$(ARTIFACTS_DIR)"/pyoxigraph/*.so; exit 1; }
	@test -f "$(ARTIFACTS_DIR)/h2o_core/sparql/concept_get.rq" \
		|| { echo "  FAIL: SPARQL templates missing -- the wheel did not carry its package data"; exit 1; }
	@echo "  ok: aarch64 pyoxigraph, SPARQL templates present, within the size limit"
