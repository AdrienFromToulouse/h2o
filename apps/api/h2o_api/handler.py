"""Lambda entrypoint.

One function serves four shapes of event: HTTP requests proxied from API
Gateway, the asynchronous self-invocation that carries an ingest run, one
publish fan-out step per Step Functions Task, and a telemetry replay.

The three action payloads are checked first because none of them is an API
Gateway event and Mangum would not know what to do with any of them.
"""

from typing import Any

from mangum import Mangum

from h2o_api import config
from h2o_api.app import app

_asgi: Mangum | None = None


def _adapter() -> Mangum:
    """Build the ASGI adapter on first use.

    Mangum claims an asyncio event loop when it is constructed, so building it
    lazily keeps that out of module import and leaves the loop owned by whoever
    is actually running the request.
    """
    global _asgi
    if _asgi is None:
        _asgi = Mangum(app, lifespan="off")
    return _asgi


def lambda_handler(event: dict[str, Any], context: Any) -> Any:
    action = event.get("h2o_action") if isinstance(event, dict) else None

    if action == config.INGEST_ACTION:
        raise NotImplementedError("ingestion lands in M4")

    if action == config.PUBLISH_STEP_ACTION:
        raise NotImplementedError("the publish fan-out lands in M6")

    if action == config.TELEMETRY_REPLAY_ACTION:
        raise NotImplementedError("telemetry replay lands in M7")

    return _adapter()(event, context)
