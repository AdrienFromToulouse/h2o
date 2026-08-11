"""The read path, over HTTP."""

from fastapi.testclient import TestClient


def test_health_reports_the_dataset_it_loaded(client: TestClient) -> None:
    """The digest is what a stale resolver index is diagnosed against."""
    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["graph_backend"] == "oxigraph"
    assert body["quads"] == 864
    assert body["dataset_digest"]


def test_the_tree_is_the_six_business_vocabularies(client: TestClient) -> None:
    body = client.get("/vocabulary").json()

    assert [s["scheme_id"] for s in body["schemes"]] == [
        "equipment",
        "fault",
        "service",
        "sustainability",
        "treatment",
        "water-output",
    ]
    assert [c["pref_label"] for c in body["top_concepts"]["equipment"]] == [
        "Component",
        "Dispenser",
    ]


def test_the_machine_scheme_is_opt_in(client: TestClient) -> None:
    default = client.get("/vocabulary").json()
    opted_in = client.get("/vocabulary", params={"include_machine": True}).json()

    assert "telemetry" not in {s["scheme_id"] for s in default["schemes"]}
    assert "telemetry" in {s["scheme_id"] for s in opted_in["schemes"]}


def test_a_concept_reads_as_the_review_card(client: TestClient) -> None:
    body = client.get("/vocabulary/concepts/carbon-filter").json()

    assert body["pref_label"]["en"] == "Carbon Filter"
    assert body["parent"]["pref_label"] == "Filter"
    assert body["machine_signals"][0]["signal"] == "dispenser.filter.life_remaining"
    assert body["version"] == 1


def test_an_unknown_term_answers_in_plain_language(client: TestClient) -> None:
    """A 404 body can reach a curator, so it says what happened rather than
    naming an identifier the interface has promised never to show (ADR-006)."""
    response = client.get("/vocabulary/concepts/limescale")

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail == "There is no term by that name in the vocabulary."
    assert "limescale" not in detail
    assert "http" not in detail


def test_a_concept_id_is_a_slug_not_an_iri(client: TestClient) -> None:
    """The IRI reaches a response only inside `technical`, which the console
    renders behind a toggle that is off by default."""
    body = client.get("/vocabulary/concepts/carbon-filter").json()

    assert body["concept_id"] == "carbon-filter"
    assert body["technical"]["iri"] == "https://vocab.h2o.example/id/carbon-filter"
    assert body["parent"]["concept_id"] == "filter"


def test_browsing_a_scheme_lists_its_top_terms(client: TestClient) -> None:
    body = client.get("/vocabulary/schemes/fault/concepts").json()

    assert [c["pref_label"] for c in body] == ["Fault"]
    assert client.get("/vocabulary/schemes/nonsense/concepts").status_code == 404
