"""Call the deployed API with a signed request.

The API is IAM-authorised (ADR-007), so plain curl gets a 403: every request
needs a SigV4 signature. This is the smallest thing that can make one, and it is
what the end-to-end checks in the README are actually run with.

    uv run python scripts/api.py GET /vocabulary
    uv run python scripts/api.py POST /ingest '{"only": ["04-support-faq.md"]}'
    uv run python scripts/api.py GET /runs/latest --params kind=ingest
    uv run python scripts/api.py POST /ingest --wait

`--wait` polls the run it just started until it stops running, which is the
shape every one of these calls has: start something, then watch /runs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

ENV = os.getenv("H2O_ENV", "prod")
REGION = os.getenv("AWS_REGION", "eu-west-1")


def base_url() -> str:
    """The API's URL, from the environment or from the stack that owns it."""
    if url := os.getenv("H2O_API_URL"):
        return url.rstrip("/")

    outputs = boto3.client("cloudformation", region_name=REGION).describe_stacks(
        StackName=f"h2o-{ENV}-api"
    )["Stacks"][0]["Outputs"]
    for output in outputs:
        if output["OutputKey"] == "ApiUrl":
            return str(output["OutputValue"]).rstrip("/")
    raise SystemExit(f"h2o-{ENV}-api has no ApiUrl output; is it deployed?")


def call(method: str, path: str, body: str | None = None, params: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    url = f"{base_url()}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    request = AWSRequest(
        method=method,
        url=url,
        data=body.encode() if body else None,
        headers={"Content-Type": "application/json"} if body else {},
    )
    credentials = boto3.Session().get_credentials()
    if credentials is None:
        raise SystemExit("no AWS credentials; set AWS_PROFILE")
    SigV4Auth(credentials.get_frozen_credentials(), "execute-api", REGION).add_auth(request)

    try:
        with urllib.request.urlopen(  # noqa: S310 - the URL is our own API
            urllib.request.Request(
                url, data=request.body, headers=dict(request.headers), method=method
            )
        ) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as failure:
        payload = failure.read()
        try:
            return failure.code, json.loads(payload or b"null")
        except json.JSONDecodeError:
            return failure.code, payload.decode(errors="replace")


def wait_for(run_id: str, *, timeout: float = 900.0) -> dict:  # type: ignore[type-arg]
    """Poll a run until it stops running.

    2.5 seconds, the same interval ADR-006 gives the console's polling hook, so
    the command line and the UI put the same load on the API.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, run = call("GET", f"/runs/{run_id}")
        if status == 200 and run and run.get("status") not in ("queued", "running"):
            return dict(run)
        print(f"  {run.get('status', '?') if run else '?'} ...", file=sys.stderr)
        time.sleep(2.5)
    raise SystemExit(f"run {run_id} did not finish inside {timeout:.0f}s")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("method", choices=["GET", "POST", "PUT", "DELETE"])
    parser.add_argument("path")
    parser.add_argument("body", nargs="?", default=None)
    parser.add_argument("--params", nargs="*", default=[], metavar="KEY=VALUE")
    parser.add_argument(
        "--wait", action="store_true", help="poll the run this call started, until it finishes"
    )
    args = parser.parse_args()

    params = dict(pair.split("=", 1) for pair in args.params)
    status, payload = call(args.method, args.path, args.body, params)

    if args.wait and isinstance(payload, dict) and "run_id" in payload:
        print(json.dumps(payload), file=sys.stderr)
        payload = wait_for(payload["run_id"])
        status = 200

    print(json.dumps(payload, indent=2, ensure_ascii=False))
    # A failed run is a successful call, and the exit code says which is which.
    if status >= 400:
        return 1
    return 1 if isinstance(payload, dict) and payload.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
