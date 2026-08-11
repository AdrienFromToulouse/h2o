"""Fixtures for the API suite. No test here touches AWS.

The environment is set before any import that could construct a boto3 client.
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("AWS_REGION", "eu-west-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-west-1")
os.environ.setdefault("H2O_ENV", "test")

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The fakes live with h2o_core because that is what they stand in for, and the
#: API's tests need the same S3 behaviour rather than a second copy of it.
sys.path.insert(0, str(REPO_ROOT / "packages" / "h2o_core" / "tests"))

import pytest  # noqa: E402
from fakes import FakeS3, load_vocabulary  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from h2o_api.app import app  # noqa: E402
from h2o_core import config, graph  # noqa: E402


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A TestClient whose graph store is the real seed vocabulary in a FakeS3."""
    seeded = FakeS3(
        {
            config.GRAPH_KEY: graph.dump(
                graph.store_from_turtle(load_vocabulary(), config.PUBLISHED_GRAPH)
            )
        }
    )
    graph.forget_cached()
    monkeypatch.setattr(graph, "s3", lambda: seeded)
    return TestClient(app)
