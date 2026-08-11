"""Fixtures for the h2o_core suite. No test here touches AWS.

The environment is set before any import that could construct a boto3 client, so
a missing region never turns into a confusing failure three modules deep.
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("AWS_REGION", "eu-west-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-west-1")
os.environ.setdefault("H2O_ENV", "test")

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The checks in scripts/ are part of what this package guarantees -- they run
#: h2o_core's own functions against the real artefacts -- so the suite imports
#: them to assert there is exactly one definition of each.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import pyoxigraph  # noqa: E402
import pytest  # noqa: E402
from fakes import FakeS3, load_vocabulary  # noqa: E402
from h2o_core import config, graph  # noqa: E402


@pytest.fixture
def store() -> pyoxigraph.Store:
    """The real seed vocabulary loaded into h2o:graph/published.

    Tests read the 80 concepts that actually ship rather than a fixture, so a
    vocabulary change that breaks a projection fails here instead of in the
    console.
    """
    return graph.store_from_turtle(load_vocabulary(), config.PUBLISHED_GRAPH)


@pytest.fixture
def seeded_s3(store: pyoxigraph.Store) -> FakeS3:
    """A FakeS3 already holding the seeded dataset."""
    return FakeS3({config.GRAPH_KEY: graph.dump(store)})


@pytest.fixture(autouse=True)
def _no_warm_cache() -> None:
    """The warm-invocation cache is process-global; leaking it across tests
    would make one test's dataset another's."""
    graph.forget_cached()
