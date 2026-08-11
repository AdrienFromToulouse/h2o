# ADR-007: A Zero-Idle Graph Runtime, the GraphStore Port, and CloudFormation Stack Decomposition

**Status:** Proposed
**Date:** 2026-08-11
**Authors:** Adrien
**References:** [ADR-001](001-human-authored-skos-vocabulary.md), [ADR-002](002-ingestion-against-the-vocabulary.md), [ADR-003](003-otel-fleet-signals-to-skos.md), [ADR-005](005-governance-and-downstream-orchestration.md)

## Context

Amazon Neptune is the obvious choice for an RDF knowledge graph on AWS, and it was this project's starting assumption. It does not survive contact with the numbers.

**Neptune Serverless has a floor.** `MinCapacity` cannot be set below **1.0 NCU**, and that capacity is billed continuously. There is no scale-to-zero. At roughly $0.16 per NCU-hour that is about **$115/month idle**, before storage and I/O. Neptune is also VPC-only, so a Lambda API needs interface endpoints or a NAT gateway, adding roughly $30/month and a cold-start penalty. Stopping the cluster reduces the bill to storage alone, but **Neptune automatically restarts a stopped cluster after seven days** to apply maintenance, so "stopped" is not a stable state without a scheduled job to re-stop it.

*Verify current pricing at build time. Do not budget from this document.*

Against that floor, consider the artefact being governed. The vocabulary is 80 concepts: a few thousand triples, kilobytes of Turtle. The fact graph for a demonstrator corpus adds low tens of thousands. **None of Neptune's value is exercised at that size**, neither horizontal scale, nor managed high availability, nor a client-facing endpoint. We would be paying an always-on cluster to hold a file that fits in an email.

**There is no AWS-managed zero-idle RDF store.** Neptune has the 1-NCU floor; Neptune Analytics is property-graph and vector rather than RDF, and also bills continuously. That absence is what forces this decision rather than making it a preference.

## Decision

### 1. Embedded Oxigraph over S3 as the default; Neptune behind the same port

**`OxigraphStore` (default).** `pyoxigraph` runs in-process inside the API Lambda. Oxigraph is an RDF store written in Rust implementing SPARQL 1.1 Query and Update; the useful mental model is *SQLite for RDF*: an engine you import, not a server you connect to.

- The dataset is one N-Quads object in S3, so named graphs ([ADR-005](005-governance-and-downstream-orchestration.md)) are preserved natively rather than emulated.
- It loads on cold start in milliseconds and is cached across warm invocations.
- Writes are read-modify-write, persisted with an S3 **conditional PUT** (`If-Match` on the ETag), so a concurrent publish fails loudly instead of silently clobbering.
- **S3 object versioning is enabled**, giving the dataset point-in-time history independently of the in-graph `history/` named graphs.

**`NeptuneStore`.** SigV4-signed HTTPS to the Neptune SPARQL endpoint, Lambda in VPC. Written and CloudFormation-authored; not deployed by default.

Both speak the **SPARQL 1.1 Query and Update protocol**, so the templates in `packages/h2o_core/sparql/` are byte-identical across backends. Selection is by `H2O_GRAPH_BACKEND`.

The same shape covers the other external dependencies, for the same reason:

| Port | AWS | Local / default |
| --- | --- | --- |
| `GraphStore` | Neptune SPARQL endpoint | embedded Oxigraph over S3 |
| `VectorStore` | S3 Vectors | NumPy cosine index |
| `OtelSource` | collector store | replayed fixture ([ADR-003](003-otel-fleet-signals-to-skos.md)) |
| `Orchestrator` | Step Functions | synchronous in-process runner |

Embeddings come from Bedrock Titan in every configuration, including local development, because it works from a laptop with credentials and substituting a different embedding model would invalidate local retrieval results.

### 2. The biggest consequence is the VPC, not the cost

No Neptune means **no VPC at all**: no private subnets, no interface endpoints, no NAT gateway, no Lambda-in-VPC cold-start penalty, and one fewer CloudFormation stack. The demonstrator deploys as plain regional services.

That is what makes it *shareable* rather than something to stand up before a meeting. A demo that can stay deployed indefinitely at effectively zero idle cost is a link you send; a demo with a $150/month floor is one you tear down and rebuild, and rebuild badly.

### 3. When to swap to Neptune, as written triggers

The swap is a `H2O_GRAPH_BACKEND` change plus deploying `90-neptune.yaml` and `91-network.yaml`. Carrying the port is justified only if the conditions for using it are stated in advance rather than argued about later.

- The dataset outgrows a comfortable Lambda load: roughly **more than ~50 MB or low millions of triples**, or cold-start load exceeding ~1s.
- **Sustained concurrent writers**, where conditional-PUT retries become observable rather than theoretical. One curator is fine; a curation team is not.
- A client wants **their own SPARQL endpoint** against the live graph.
- **Multi-tenant isolation, high availability, or point-in-time recovery** becomes a requirement rather than a preference.

### 4. CloudFormation

Layered stacks, parameterised on `Environment` with `AllowedValues`, `cfn-lint`-validated in CI. Each template's `Description` cites its owning ADR; `Outputs` export both name and ARN. Physical names follow `h2o-{env}-{resource}`.

Stacks `00`–`60` are the default deployment. The `9x` stacks are the Neptune swap and deploy only on demand.

```
infra/cloudformation/
  00-graph.yaml         versioned S3 dataset bucket + publish lock table
  10-data.yaml          S3 Vectors bucket + index, raw-docs bucket, DynamoDB
                        vocabulary-gaps + curation-audit + document-registry
  20-telemetry.yaml     OTEL collector ingest + fleet-signal store (ADR-003)
  30-orchestration.yaml EventBridge bus + Step Functions publish fan-out (ADR-005)
  40-api.yaml           FastAPI Lambda + API Gateway (IAM auth), no VPC
  50-agent.yaml         AgentCore Runtime + ECR repo + read-only execution role
  60-frontend.yaml      scoped IAM user for the Vercel BFF
  90-neptune.yaml       [swap only] Neptune Serverless, subnet group, IAM auth,
                        bulk-loader role + staging bucket
  91-network.yaml       [swap only] VPC, private subnets, SGs, VPC endpoints
```

`90-neptune.yaml` carries a prominent cost warning, defaults to `MinCapacity: 1.0`, and ships with a `make graph-down` target plus an EventBridge-scheduled re-stop Lambda, because a stopped cluster restarts itself after seven days.

**Seeding is symmetric.** On the default runtime, `vocab/*.ttl` loads through pyoxigraph. On Neptune, the bulk loader ingests the same files natively as `format: "turtle"`, with `parserConfiguration.namedGraphUri` set to `h2o:graph/published`. Either way the reviewed file is the loaded artefact, with no serialization step in between.

## Consequences

### Benefits

- **Effectively zero idle cost**, so the demonstrator stays deployed and shareable.
- **No VPC**, which removes a whole stack, the endpoint charges, and the Lambda cold-start penalty.
- **Reads never leave the process.** A SPARQL query is an in-process call, not an HTTPS round trip inside a VPC.
- **Stronger publish atomicity than expected.** The whole update runs against the loaded store and lands as a single conditional S3 write, so it either becomes a new object version or does not happen ([ADR-005](005-governance-and-downstream-orchestration.md)).
- **Free dataset-level history** from S3 object versioning, on top of the in-graph history graphs.
- **The Neptune path is a config change**, not a migration, because the port and the shared SPARQL templates exist from day one.

### Trade-offs

- **Oxigraph's own maintainers state that "SPARQL query evaluation has not been optimized yet."** The project prioritises correctness over speed. At a few thousand triples this is irrelevant, and it is recorded here so this choice is never mistaken for a production-database endorsement at scale.
- **Every publish rewrites the whole dataset** to S3. Correct at kilobytes, wrong at gigabytes. This is the first swap trigger.
- **One writer.** The conditional PUT makes a collision *safe*, not *concurrent*: the loser reloads and retries. This is our own concurrency control and needs an explicit lost-update test.
- **Packaging.** `pyoxigraph` is a compiled Rust wheel and needs a build matching the Lambda architecture. Handled by a container-image Lambda, but it is a real build step rather than `pip install` and done. The publish path additionally carries pySHACL and rdflib for the integrity gate ([ADR-005](005-governance-and-downstream-orchestration.md)); pySHACL offers an optional pyoxigraph backend, so the two compose rather than each keeping its own copy of the graph.
- **Cold-start load ties graph size to Lambda memory.** The fact graph from ingestion grows far faster than the vocabulary; that is the number the swap triggers watch, not the concept count.
- **Two backends means SPARQL dialect-drift risk.** Mitigated by running the *same* suite against both in CI and keeping every query in template files rather than inline strings. The Neptune path will still be less exercised than the default: an honest asymmetry, bounded by that shared suite.

### Out of scope

No Neptune deployment in the default path. No graph sharding or partitioning. No caching layer in front of the store, which is already in-process. No retention policy for S3 object versions.

## References

- [Oxigraph](https://github.com/oxigraph/oxigraph): Rust RDF store, SPARQL 1.1 Query and Update
- [pyoxigraph Store API](https://pyoxigraph.readthedocs.io/en/stable/store.html)
- [Neptune Serverless capacity scaling](https://docs.aws.amazon.com/neptune/latest/userguide/neptune-serverless-capacity-scaling.html): the 1.0 NCU minimum
- [Stopping and starting a Neptune DB cluster](https://docs.aws.amazon.com/neptune/latest/userguide/manage-console-stop-start.html): storage-only billing, 7-day auto-restart
- [Neptune bulk loader data formats](https://docs.aws.amazon.com/neptune/latest/userguide/bulk-load-tutorial-format.html)
- [S3 conditional requests](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-requests.html)
- [ADR-005](005-governance-and-downstream-orchestration.md): publish atomicity requirements
