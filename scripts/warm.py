#!/usr/bin/env python3
"""Wake the Aurora cluster, and optionally the Lambda, before anything that is watched.

    eval "$(terraform -chdir=infra/main output -raw shell_exports)"
    python scripts/warm.py            # the database
    python scripts/warm.py --endpoint # and a real tool call through the deployed server

Run it before recording. A cluster at zero ACU takes about fifteen seconds to resume, an
API Gateway HTTP API gives up at thirty, and the first tool call after an idle period is
the one that lands in front of an audience. Warm it, do not explain it.

Two things worth being precise about, because they change what this script is for.

A paused cluster is not slow, it is absent: the Data API answers with an error while the
cluster wakes rather than blocking until it is ready. So the retry is not politeness, it
is the mechanism. That retry lives in server/dataapi.py and applies to every call the
server makes, which means a cold first request recovers on its own — this script exists so
that recovery happens off camera, not so that it happens at all.

And --endpoint warms a different thing. The database resuming is one cold start; the
Lambda's is another, and it is the larger of the two on a first invocation because it is
importing pydantic and building four tool schemas. Warming the database alone still leaves
that in the recording.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from server.dataapi import DataApi  # noqa: E402

PROTOCOL_VERSION = "2026-07-28"

# The stateless envelope, spelled out rather than left to a client library, because
# getting it wrong is the single most likely reason a hand-made request is rejected and
# the error codes are more specific than they first look. Three separate rejections,
# earned in order against the deployed server:
#
#   -32602  params._meta is missing the required envelope key(s):
#           io.modelcontextprotocol/clientCapabilities
#           Both envelope keys live inside params._meta. clientCapabilities as a sibling
#           of _meta is not the same thing and does not count.
#
#   -32020  mcp-method header does not match the request body's method
#           Every request carries its JSON-RPC method twice, once in the body and once as
#           the Mcp-Method header, and the server checks they agree. An absent header
#           fails this rather than skipping it.
#
#   and for tools/call, prompts/get and resources/read the same applies to Mcp-Name,
#   which mirrors the params key named in the SDK's NAME_BEARING_METHODS.
META_PROTOCOL = "io.modelcontextprotocol/protocolVersion"
META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"

NAME_BEARING_METHODS = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}


def envelope(method: str, params: dict | None = None) -> tuple[dict, dict[str, str]]:
    """One JSON-RPC request body and the headers that must agree with it."""
    params = dict(params or {})
    params["_meta"] = {
        META_PROTOCOL: PROTOCOL_VERSION,
        META_CAPABILITIES: {},
        **(params.get("_meta") or {}),
    }

    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "mcp-protocol-version": PROTOCOL_VERSION,
        "mcp-method": method,
    }

    name_key = NAME_BEARING_METHODS.get(method)
    if name_key and isinstance(params.get(name_key), str):
        headers["mcp-name"] = params[name_key]

    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, headers


def warm_database() -> float:
    api = DataApi.from_env()
    started = time.monotonic()
    rows = api.query("select 1 as ok")
    elapsed = time.monotonic() - started
    if not rows or rows[0].get("ok") != 1:
        raise SystemExit(f"The database answered, but not with 1: {rows!r}")
    return elapsed


def call(endpoint: str, method: str, params: dict | None = None) -> tuple[float, int, dict]:
    body, headers = envelope(method, params)
    request = urllib.request.Request(
        endpoint, data=json.dumps(body).encode(), headers=headers, method="POST"
    )

    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload, status = response.read(), response.status
    except urllib.error.HTTPError as exc:
        # The body of a 4xx is the JSON-RPC error object, which is the useful part. urllib
        # raises before anyone can read it, so read it here.
        payload, status = exc.read(), exc.code
    elapsed = time.monotonic() - started

    return elapsed, status, json.loads(payload or b"{}")


def warm_endpoint(endpoint: str) -> tuple[float, int, int]:
    elapsed, status, payload = call(endpoint, "tools/list")
    if "error" in payload:
        raise SystemExit(f"tools/list failed with {status}: {payload['error']}")
    return elapsed, status, len(payload.get("result", {}).get("tools", []))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--endpoint",
        nargs="?",
        const=os.environ.get("BIZDATA_MCP_ENDPOINT", ""),
        help="also call tools/list against the deployed server; defaults to BIZDATA_MCP_ENDPOINT",
    )
    args = parser.parse_args(argv)

    elapsed = warm_database()
    print(f"database  awake in {elapsed:.1f}s")

    if args.endpoint:
        elapsed, status, count = warm_endpoint(args.endpoint)
        print(f"endpoint  {status}, {count} tools, {elapsed:.1f}s")
    elif args.endpoint == "":
        raise SystemExit("--endpoint given with no URL and BIZDATA_MCP_ENDPOINT is not set.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
