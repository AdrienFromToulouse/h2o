from fastapi import FastAPI

from h2o_api.routers import health, vocabulary

app = FastAPI(
    title="h2o vocabulary API",
    description=(
        "Read and govern the SKOS vocabulary that defines h2o's entities, and the "
        "claims resolved against it. Every fact carries the file it came from, the "
        "document version, and the exact snippet it was extracted from."
    ),
    version="0.1.0",
)

app.include_router(health.router)
app.include_router(vocabulary.router)
