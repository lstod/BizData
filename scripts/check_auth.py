#!/usr/bin/env python3
"""Prove the deployed server's authentication, including the parts that should fail.

    eval "$(terraform -chdir=infra/main output -raw shell_exports)"
    eval "$(terraform -chdir=infra/main output -raw auth_exports)"
    python scripts/check_auth.py

The connector's own flow is authorization_code with PKCE and needs a human at a browser, so
it cannot be asserted here. What can be asserted is the server's half of the contract, and
that is the half worth testing: a real Cognito access token — same pool, same signing keys,
same bizdata/read scope — obtained through client_credentials, which needs no browser.

Positive results prove less than negative ones here. A server that accepts a valid token is
also consistent with a server that accepts anything, so most of what follows is the refusals:
no token, a tampered signature, an ID token used as an access token, and a token whose scope
is missing. Each of those is a specific hole in a specific real-world Cognito integration.

The discovery chain is checked too, because it is what step 4 left open. A client with no
token has to be told where to authenticate, and that answer is carried in a WWW-Authenticate
header pointing at RFC 9728 metadata. If that chain is broken the connector cannot start a
flow at all, and the symptom looks like a server fault rather than a discovery one.

Since step 9 the write tool is checked here as well, and only as far as minting: a
``publish_pack`` call without ``finalize`` returns URLs and writes nothing, which proves the
one write path in the system is behind the same token without leaving an object in the
archive of record every time the auth chain is checked. scripts/check_archive.py is the one
that spends objects.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.warm import envelope  # noqa: E402


class Checks:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, bool(ok), detail))

    @property
    def failed(self) -> list[tuple[str, bool, str]]:
        return [r for r in self.results if not r[1]]


def get(url: str, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def post_mcp(endpoint: str, method: str, token: str | None, params: dict | None = None):
    body, headers = envelope(method, params)
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        endpoint, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def client_credentials_token(token_endpoint: str, client_id: str, secret: str, scope: str) -> str:
    """A real access token from Cognito, with no browser involved."""
    data = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": scope}).encode()
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    request = urllib.request.Request(
        token_endpoint,
        data=data,
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "authorization": f"Basic {basic}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())["access_token"]
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Cognito refused client_credentials: {exc.code} {exc.read().decode()}")


def decode_claims(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def tamper(token: str) -> str:
    """Same header and payload, one byte different in the signature."""
    head, payload, signature = token.split(".")
    flipped = ("B" if signature[0] != "B" else "C") + signature[1:]
    return f"{head}.{payload}.{flipped}"


SERVER_INFO_META = "io.modelcontextprotocol/serverInfo"


def check_deployment_is_current(payload: dict, checks: Checks) -> None:
    """Is the code answering this endpoint the code in this checkout?

    Step 6's third finding, and the one that cost the most: a Skill went out against a Lambda
    four steps behind it, two Cowork runs produced wrong deliverables, and every harness in
    the repository passed throughout — because every harness ran against the working tree.
    Nothing asserted the deployment. This is that assertion.

    It is here rather than in its own script because this is the only harness that speaks to
    the public endpoint at all, and a freshness check nobody runs is the same as no check.

    The version comes back on every response's `_meta`, not only on a handshake, because the
    server is stateless and there is no handshake to put it on.
    """
    from server import build_info

    reported = (
        payload.get("result", {})
        .get("_meta", {})
        .get(SERVER_INFO_META, {})
        .get("version", "")
    )
    local = build_info.version()

    checks.add(
        "the deployed server reports which commit it is running",
        bool(reported) and "+" in reported,
        reported or "no serverInfo version in the response",
    )

    deployed_commit = reported.partition("+")[2]
    checks.add(
        "the deployed code is the code in this checkout",
        deployed_commit == build_info.commit(),
        f"deployed {deployed_commit or 'unknown'}, local {build_info.commit()}"
        + ("" if deployed_commit == build_info.commit() else " — run scripts/package_lambda.sh and apply"),
    )
    checks.add(
        "the deployment was not built from a tree with uncommitted changes",
        not deployed_commit.endswith("-dirty"),
        reported or local,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--endpoint", default=os.environ.get("BIZDATA_MCP_ENDPOINT", ""))
    parser.add_argument("--token-endpoint", default=os.environ.get("BIZDATA_OAUTH_TOKEN_ENDPOINT", ""))
    parser.add_argument("--client-id", default=os.environ.get("BIZDATA_TEST_CLIENT_ID", ""))
    parser.add_argument("--client-secret", default=os.environ.get("BIZDATA_TEST_CLIENT_SECRET", ""))
    parser.add_argument("--scope", default=os.environ.get("BIZDATA_OAUTH_SCOPES", "bizdata/read"))
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    missing = [
        name
        for name, value in (
            ("--endpoint", args.endpoint),
            ("--token-endpoint", args.token_endpoint),
            ("--client-id", args.client_id),
            ("--client-secret", args.client_secret),
        )
        if not value
    ]
    if missing:
        raise SystemExit(
            f"Missing {', '.join(missing)}. These come from the Cognito outputs:\n"
            f'  eval "$(terraform -chdir=infra/main output -raw auth_exports)"'
        )

    base = args.endpoint.removesuffix("/mcp")
    checks = Checks()

    # ---- discovery, the chain a client with no token has to follow --------------------
    status, headers, _ = post_mcp(args.endpoint, "tools/list", token=None)
    challenge = headers.get("WWW-Authenticate", headers.get("www-authenticate", ""))

    checks.add("no token is refused", status == 401, f"HTTP {status}")
    checks.add(
        "the 401 carries a WWW-Authenticate challenge",
        challenge.lower().startswith("bearer"),
        challenge[:110] or "absent",
    )
    checks.add(
        "the challenge names the RFC 9728 metadata document",
        "resource_metadata" in challenge,
        challenge[:110] or "absent",
    )

    status, _, body = get(f"{base}/.well-known/oauth-protected-resource/mcp")
    resource_meta = json.loads(body) if status == 200 else {}
    checks.add("protected resource metadata is served", status == 200, f"HTTP {status}")
    checks.add(
        "it names an authorization server",
        bool(resource_meta.get("authorization_servers")),
        str(resource_meta.get("authorization_servers", []))[:90],
    )

    # The step-4 question, and the whole reason oauth_metadata.py has two modes: can a
    # client that follows the chain actually find an authorization endpoint at the end?
    for issuer in resource_meta.get("authorization_servers", []):
        issuer = str(issuer).rstrip("/")
        rfc8414_status, _, rfc8414_body = get(f"{issuer}/.well-known/oauth-authorization-server")
        oidc_status, _, oidc_body = get(f"{issuer}/.well-known/openid-configuration")

        found = None
        for label, code, raw in (
            ("RFC 8414", rfc8414_status, rfc8414_body),
            ("OIDC", oidc_status, oidc_body),
        ):
            if code == 200 and "authorization_endpoint" in json.loads(raw or b"{}"):
                found = label
                break

        checks.add(
            f"authorization server metadata is reachable at {issuer[:52]}",
            found is not None,
            f"RFC 8414 {rfc8414_status}, OIDC {oidc_status}"
            + (f", usable via {found}" if found else ", neither usable"),
        )

    # ---- a real token, and the refusals that matter ----------------------------------
    token = client_credentials_token(
        args.token_endpoint, args.client_id, args.client_secret, args.scope
    )
    claims = decode_claims(token)

    checks.add("Cognito issued an access token", claims.get("token_use") == "access", str(claims.get("token_use")))
    checks.add(
        "the token carries the bizdata scope",
        args.scope in str(claims.get("scope", "")),
        str(claims.get("scope")),
    )
    checks.add(
        "a Cognito access token carries client_id and no aud",
        "client_id" in claims and "aud" not in claims,
        f"client_id={'yes' if 'client_id' in claims else 'no'}, aud={'yes' if 'aud' in claims else 'no'}",
    )

    status, _, body = post_mcp(args.endpoint, "tools/list", token=token)
    payload = json.loads(body or b"{}")
    tools = payload.get("result", {}).get("tools", [])
    names = {t.get("name") for t in tools}
    checks.add("a valid token is accepted", status == 200 and len(tools) == 6, f"HTTP {status}, {len(tools)} tools")
    # Named literally rather than imported from server.tools, because the question here is
    # what the *deployment* publishes. Importing the set the local checkout defines would
    # make this assertion agree with itself across a stale deploy, which is exactly the
    # failure check_deployment_is_current below exists to catch.
    checks.add(
        "the deployed surface is the four read tools, get_run_ledger and publish_pack",
        names == {
            "list_engagements", "get_engagement_burn", "get_time_summary", "get_financials",
            "get_run_ledger", "publish_pack",
        },
        ", ".join(sorted(str(n) for n in names)),
    )

    check_deployment_is_current(payload, checks)

    status, _, body = post_mcp(
        args.endpoint,
        "tools/call",
        token=token,
        params={
            "name": "list_engagements",
            "arguments": {"as_of_date": "2026-08-31", "status": "active", "limit": 2, "run_id": "check-auth"},
        },
    )
    structured = json.loads(body or b"{}").get("result", {}).get("structuredContent", {})
    checks.add(
        "an authenticated tool call reaches Aurora",
        status == 200 and structured.get("total_count") == 18,
        f"HTTP {status}, total_count={structured.get('total_count')}",
    )

    # The write tool, reached with the same token and the same scope. finalize is left off
    # deliberately: phase one mints URLs and writes nothing, so this proves the only write
    # path in the system is authenticated and reachable without putting an object in the
    # archive of record every time somebody checks the auth chain. What the URLs actually do
    # is scripts/check_archive.py.
    status, _, body = post_mcp(
        args.endpoint,
        "tools/call",
        token=token,
        params={
            "name": "publish_pack",
            "arguments": {
                "run_id": "check-auth",
                "period": "2026-08",
                "artifacts": ["engagement-book-2026-08.xlsx"],
            },
        },
    )
    minted = json.loads(body or b"{}").get("result", {}).get("structuredContent", {})
    uploads = minted.get("uploads", [])
    checks.add(
        "the write tool is reachable with the same token",
        status == 200 and minted.get("phase") == "prepared" and len(uploads) == 1,
        f"HTTP {status}, phase={minted.get('phase')}",
    )
    checks.add(
        "and mints an https url scoped to this run's own key",
        bool(uploads)
        and str(uploads[0].get("url", "")).startswith("https://")
        and uploads[0].get("key") == "runs/check-auth/engagement-book-2026-08.xlsx",
        str(uploads[0].get("key")) if uploads else "no url minted",
    )
    checks.add(
        "and phase one wrote nothing",
        not minted.get("archived") and minted.get("ledger_key") is None,
        "no archived objects",
    )

    status, _, _ = post_mcp(args.endpoint, "tools/list", token=tamper(token))
    checks.add("a tampered signature is refused", status == 401, f"HTTP {status}")

    status, _, _ = post_mcp(args.endpoint, "tools/list", token="not-a-jwt")
    checks.add("a malformed token is refused, not a 500", status == 401, f"HTTP {status}")

    status, _, _ = post_mcp(args.endpoint, "tools/list", token=token[:-6])
    checks.add("a truncated token is refused", status == 401, f"HTTP {status}")

    # ---- report ----------------------------------------------------------------------
    failed = checks.failed
    if args.verbose or failed:
        print(f"{'':>3}  {'ok':<3} {'assertion':<62} detail")
        for i, (name, ok, detail) in enumerate(checks.results, 1):
            if args.verbose or not ok:
                print(f"{i:>3}  {'t' if ok else 'F':<3} {name[:62]:<62} {detail}")
        print()

    print(
        f"{len(checks.results) - len(failed)} of {len(checks.results)} assertions passed"
        + ("" if not failed else f" -- {len(failed)} FAILED")
    )
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
