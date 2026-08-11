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
from fakes import FakeS3, FakeTable, load_vocabulary  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from h2o_api.app import app  # noqa: E402
from h2o_core import config, graph, store  # noqa: E402

DOCS_DIR = REPO_ROOT / "data" / "docs"


def seeded_dataset() -> bytes:
    return graph.dump(graph.store_from_turtle(load_vocabulary(), config.PUBLISHED_GRAPH))


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> FakeS3:
    """The dataset and the raw corpus, in one fake.

    FakeS3 keys on object key alone, so the graph bucket and the raw-docs bucket
    share it without colliding -- the corpus filenames and `graph/dataset.nq`
    cannot be confused for one another.
    """
    objects = {config.GRAPH_KEY: seeded_dataset()}
    for path in sorted(DOCS_DIR.iterdir()):
        if path.is_file():
            objects[path.name] = path.read_bytes()

    fake = FakeS3(objects)
    graph.forget_cached()
    monkeypatch.setattr(graph, "s3", lambda: fake)
    return fake


@pytest.fixture
def runs_table(monkeypatch: pytest.MonkeyPatch) -> FakeTable:
    table = FakeTable(
        hash_key="run_id", range_key="sk", indexes={"by-kind": ("kind", "started_at")}
    )
    monkeypatch.setattr(store, "runs_table", lambda: table)
    return table


@pytest.fixture
def gaps_table(monkeypatch: pytest.MonkeyPatch) -> FakeTable:
    table = FakeTable(hash_key="gap_id")
    monkeypatch.setattr(store, "gaps_table", lambda: table)
    return table


@pytest.fixture
def registry_table(monkeypatch: pytest.MonkeyPatch) -> FakeTable:
    table = FakeTable(hash_key="filename", range_key="doc_version")
    monkeypatch.setattr(store, "registry_table", lambda: table)
    return table


@pytest.fixture
def client(s3: FakeS3) -> TestClient:
    """A TestClient whose graph store is the real seed vocabulary in a FakeS3."""
    return TestClient(app)
