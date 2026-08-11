"""Where this process is running, and the action markers that say what to do.

`IS_LAMBDA` is the only environment discriminator in h2o, and it is read in
exactly one place: h2o_api.dispatch, which uses it to decide *who calls* the
asynchronous work -- the ingest run, and the publish fan-out steps. It must
never reach a step body. apps/api/tests asserts that.
"""

import os

from h2o_core import config as core

AWS_REGION = core.AWS_REGION
H2O_ENV = core.H2O_ENV

FUNCTION_NAME = os.getenv("AWS_LAMBDA_FUNCTION_NAME")
IS_LAMBDA = bool(FUNCTION_NAME)

EVENT_BUS_NAME = core.EVENT_BUS_NAME

#: Payload markers for the three non-HTTP shapes this function serves. They are
#: checked before Mangum, which would not know what to do with any of them.
INGEST_ACTION = "ingest"
PUBLISH_STEP_ACTION = "publish_step"
TELEMETRY_REPLAY_ACTION = "telemetry_replay"
