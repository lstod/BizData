#!/usr/bin/env python3
"""Prove the archive against the real bucket, including the parts that should fail.

    eval "$(terraform -chdir=infra/main output -raw shell_exports)"
    eval "$(terraform -chdir=infra/main output -raw auth_exports)"
    python scripts/check_archive.py
    python scripts/check_archive.py --wait-for-expiry     adds a real 900-second wait

check_publish.py asserts step 9's logic on seventeen seeds against a directory that models a
bucket. This asserts the mechanism, once, against the bucket — and the two are not the same
claim. A local archive that enforces its own grant table proves that the harness agrees with
itself. What has to be true for the design to hold is that *S3* refuses a presigned URL used
on another key, refuses one that has expired, and refuses one signed for a method other than
the one attempted. Only S3 can settle that.

The URLs here are minted by the deployed Lambda over HTTPS rather than by this script, which
matters more than it looks. A presigned URL carries the permissions of whoever signed it, so
a URL minted locally would be signed by an administrator and would prove nothing about the
boundary the Lambda actually runs inside. Minted by the function, the URL is exactly the one
Cowork gets.

Positive results prove less than negative ones, as in check_auth.py. That a URL writes its
key is also consistent with a URL that writes anything, so most of what follows is refusals.

Two conditions cannot be proved by a URL at all and are read off the applied infrastructure
instead: that the bucket has versioning and a Glacier lifecycle rule, and that the role's
policy is scoped to the runs/ prefix. The second is the fence behind the fence — the key is
validated in code, and this is what would still refuse it if that validation were wrong.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from check_auth import Checks, client_credentials_token, post_mcp  # noqa: E402

RUN_ID = "check-archive"
PERIOD = "2026-08"
WORKBOOK = f"engagement-book-{PERIOD}.xlsx"
DECK = f"delivery-review-{PERIOD}.pptx"
PREFIX = f"runs/{RUN_ID}"

ROLE_NAME = "bizdata-mcp-server"
POLICY_NAME = "bizdata-mcp-data-access"

# Small but not empty. publish_pack treats a zero-byte object as an artifact that did not
# arrive, which is deliberate — a failed PUT can leave one — so the probe bytes have to be
# real bytes.
WORKBOOK_BYTES = b"PK\x03\x04 not a real workbook, but a real object " + b"." * 512
DECK_BYTES = b"PK\x03\x04 not a real deck, but a real object " + b"." * 512


def put(url: str, data: bytes) -> tuple[int, str]:
    """A real HTTPS PUT, the way a sandbox would do it. Errors come back as data."""
    request = urllib.request.Request(url, data=data, method="PUT")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, ""
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        code = ""
        if "<Code>" in body:
            code = body.split("<Code>")[1].split("</Code>")[0]
        return exc.code, code or body[:80]
    except urllib.error.URLError as exc:
        return 0, str(exc)[:80]


def http_get(url: str) -> tuple[int, str]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, ""
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        code = body.split("<Code>")[1].split("</Code>")[0] if "<Code>" in body else body[:80]
        return exc.code, code
    except urllib.error.URLError as exc:
        return 0, str(exc)[:80]


def call_tool(endpoint: str, token: str, name: str, arguments: dict) -> tuple[int, dict, str]:
    status, _, body = post_mcp(
        endpoint, "tools/call", token=token, params={"name": name, "arguments": arguments}
    )
    payload = json.loads(body or b"{}")
    result = payload.get("result", {})
    text = "; ".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
    return status, result.get("structuredContent") or {}, text


# ------------------------------------------------------------------- the bucket as applied


def check_bucket(s3, bucket: str, checks: Checks) -> None:
    versioning = s3.get_bucket_versioning(Bucket=bucket)
    checks.add(
        "the bucket has versioning on",
        versioning.get("Status") == "Enabled",
        str(versioning.get("Status")),
    )

    rules = s3.get_bucket_lifecycle_configuration(Bucket=bucket).get("Rules", [])
    enabled = [r for r in rules if r.get("Status") == "Enabled"]
    transitions = [t for r in enabled for t in r.get("Transitions", [])]
    noncurrent = [t for r in enabled for t in r.get("NoncurrentVersionTransitions", [])]

    checks.add(
        "and a Glacier lifecycle rule at ninety days",
        any(t.get("Days") == 90 and t.get("StorageClass") == "GLACIER" for t in transitions),
        ", ".join(f"{t.get('Days')}d to {t.get('StorageClass')}" for t in transitions) or "none",
    )
    checks.add(
        "and the same rule for the versions the bucket keeps",
        any(
            t.get("NoncurrentDays") == 90 and t.get("StorageClass") == "GLACIER" for t in noncurrent
        ),
        ", ".join(f"{t.get('NoncurrentDays')}d to {t.get('StorageClass')}" for t in noncurrent) or "none",
    )

    block = s3.get_public_access_block(Bucket=bucket)["PublicAccessBlockConfiguration"]
    checks.add(
        "and no path to public access",
        all(block.values()),
        ", ".join(k for k, v in block.items() if not v) or "all four blocked",
    )


def check_role_is_scoped(iam, checks: Checks) -> None:
    """The fence behind the fence.

    server/tools/publish_pack.py builds the key and refuses anything that could leave the
    prefix. This is what would refuse it anyway. Read off the applied policy rather than the
    Terraform, because what is applied is what is enforced.
    """
    try:
        document = iam.get_role_policy(RoleName=ROLE_NAME, PolicyName=POLICY_NAME)["PolicyDocument"]
    except Exception as exc:  # noqa: BLE001
        checks.add("the role's data-access policy is readable", False, str(exc)[:96])
        return

    statements = document.get("Statement", [])
    by_action: dict[str, list[str]] = {}
    for statement in statements:
        actions = statement.get("Action", [])
        actions = [actions] if isinstance(actions, str) else actions
        resources = statement.get("Resource", [])
        resources = [resources] if isinstance(resources, str) else resources
        for action in actions:
            by_action.setdefault(action, []).extend(resources)

    checks.add(
        "the role can write objects",
        "s3:PutObject" in by_action,
        ", ".join(sorted(a for a in by_action if a.startswith("s3:"))) or "no s3 actions",
    )
    checks.add(
        "and only under the runs/ prefix",
        all(r.endswith("/runs/*") for r in by_action.get("s3:PutObject", ["no"])),
        ", ".join(by_action.get("s3:PutObject", [])),
    )
    checks.add(
        "and cannot delete anything it has archived",
        not any(a.startswith("s3:Delete") for a in by_action),
        ", ".join(sorted(a for a in by_action if a.startswith("s3:Delete"))) or "no delete actions",
    )
    checks.add(
        "and can read back its own tool-call log",
        "logs:FilterLogEvents" in by_action,
        ", ".join(by_action.get("logs:FilterLogEvents", [])) or "absent",
    )


# ------------------------------------------------------------------------ the grant itself


def check_grant(endpoint: str, token: str, s3, bucket: str, checks: Checks) -> dict[str, str]:
    status, minted, text = call_tool(
        endpoint, token, "publish_pack",
        {"run_id": RUN_ID, "period": PERIOD, "artifacts": [WORKBOOK, DECK]},
    )
    checks.add(
        "the deployed tool mints a url per artifact",
        status == 200 and len(minted.get("uploads", [])) == 2,
        f"HTTP {status} {text[:60]}",
    )
    urls = {u["filename"]: u["url"] for u in minted.get("uploads", [])}
    if len(urls) != 2:
        return {}

    checks.add(
        "the url is https and points at the archive bucket",
        all(u.startswith(f"https://{bucket}.s3") or f"/{bucket}/" in u for u in urls.values()),
        list(urls.values())[0].split("?")[0],
    )
    checks.add(
        "the url expires, and says when",
        all("X-Amz-Expires=900" in u for u in urls.values()),
        "X-Amz-Expires=900",
    )

    # The positive. One PUT, one key.
    status, detail = put(urls[WORKBOOK], WORKBOOK_BYTES)
    checks.add("a presigned url writes its one key", status == 200, f"HTTP {status} {detail}")

    head = s3.head_object(Bucket=bucket, Key=f"{PREFIX}/{WORKBOOK}")
    checks.add(
        "and the object in the bucket is the bytes that were sent",
        head["ETag"].strip('"') == hashlib.md5(WORKBOOK_BYTES).hexdigest()
        and head["ContentLength"] == len(WORKBOOK_BYTES),
        f"{head['ContentLength']} bytes, {head['ETag'].strip('\"')[:12]}",
    )

    # The refusals, which are the point.
    elsewhere = urls[WORKBOOK].replace(f"/{WORKBOOK}?", "/somewhere-else.xlsx?")
    status, detail = put(elsewhere, b"should not land")
    checks.add(
        "the same url is refused on any other key",
        status == 403 and detail == "SignatureDoesNotMatch",
        f"HTTP {status} {detail}",
    )
    checks.add(
        "and nothing landed at that other key",
        _absent(s3, bucket, f"{PREFIX}/somewhere-else.xlsx"),
        "absent",
    )

    outside = urls[WORKBOOK].replace(f"{PREFIX}/{WORKBOOK}?", "outside-the-prefix.xlsx?")
    status, detail = put(outside, b"should not land")
    checks.add(
        "a url edited to escape the prefix is refused",
        status == 403,
        f"HTTP {status} {detail}",
    )

    status, detail = http_get(urls[WORKBOOK])
    checks.add(
        "a url signed for PUT is refused for GET",
        status == 403,
        f"HTTP {status} {detail}",
    )

    status, detail = put(urls[WORKBOOK].split("?")[0], b"should not land")
    checks.add(
        "the same key with no signature at all is refused",
        status in (403, 400),
        f"HTTP {status} {detail}",
    )

    status, detail = put(urls[DECK], DECK_BYTES)
    checks.add("the second artifact writes its own key", status == 200, f"HTTP {status} {detail}")
    return urls


def _absent(s3, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return False
    except Exception:  # noqa: BLE001
        return True


def check_expiry(archive, s3, bucket: str, urls: dict[str, str], wait: bool, checks: Checks) -> None:
    """Expiry, twice: quickly with a short-lived url, and for real if asked.

    The short one is signed by whoever is running this script rather than by the Lambda,
    because the tool has no argument for a lifetime and should not grow one to make a test
    convenient. What it proves is the mechanism — S3 rejects a url past its expiry — which is
    a property of S3 and not of the signer. The real 900-second wait proves the number.
    """
    brief = archive.presign_put(f"{PREFIX}/expiring-probe.xlsx", 2)
    time.sleep(3)
    status, detail = put(brief.url, b"too late")
    checks.add(
        "an expired url is refused",
        status == 403 and detail in ("AccessDenied", "ExpiredToken", "SignatureDoesNotMatch"),
        f"HTTP {status} {detail}",
    )
    checks.add(
        "and nothing landed",
        _absent(s3, bucket, f"{PREFIX}/expiring-probe.xlsx"),
        "absent",
    )

    if not wait:
        return

    minted_at = dt.datetime.now(dt.timezone.utc)
    print(f"waiting 900s for the real url to expire, from {minted_at.isoformat(timespec='seconds')}")
    time.sleep(905)
    status, detail = put(urls[WORKBOOK], b"too late")
    checks.add(
        "the fifteen-minute url is refused after fifteen minutes",
        status == 403,
        f"HTTP {status} {detail}, minted {minted_at.isoformat(timespec='seconds')}",
    )


# ---------------------------------------------------------------------------- finalisation


def check_finalise(endpoint: str, token: str, s3, bucket: str, checks: Checks) -> None:
    status, archived, text = call_tool(
        endpoint, token, "publish_pack",
        {"run_id": RUN_ID, "period": PERIOD, "artifacts": [WORKBOOK, DECK], "finalize": True},
    )
    checks.add(
        "finalising writes the log and the ledger",
        status == 200 and archived.get("phase") == "archived",
        f"HTTP {status} {text[:60]}",
    )
    if archived.get("phase") != "archived":
        return

    listed = s3.list_objects_v2(Bucket=bucket, Prefix=f"{PREFIX}/")
    keys = {o["Key"] for o in listed.get("Contents", [])}
    expected = {f"{PREFIX}/{n}" for n in (WORKBOOK, DECK, "run-log.json", "ledger.json")}
    checks.add(
        "runs/<run_id>/ holds all four artifacts",
        expected <= keys,
        ", ".join(sorted(k.rsplit("/", 1)[-1] for k in keys)),
    )

    ledger = json.loads(s3.get_object(Bucket=bucket, Key=f"{PREFIX}/ledger.json")["Body"].read())
    checks.add(
        "the ledger in the bucket is complete and names this run",
        ledger.get("status") == "complete" and ledger.get("run_id") == RUN_ID,
        f"{ledger.get('status')}, {ledger.get('run_id')}",
    )
    checks.add(
        "and records the deployed build that wrote it",
        bool(ledger.get("server_version")) and "+" in str(ledger.get("server_version")),
        str(ledger.get("server_version")),
    )

    log = json.loads(s3.get_object(Bucket=bucket, Key=f"{PREFIX}/run-log.json")["Body"].read())
    checks.add(
        "the tool-call log came back out of CloudWatch",
        isinstance(log, list) and bool(log),
        f"{len(log) if isinstance(log, list) else 0} line(s)",
    )
    checks.add(
        "and holds only this run's lines",
        isinstance(log, list) and all(line.get("run_id") == RUN_ID for line in log),
        f"{len({line.get('run_id') for line in log}) if isinstance(log, list) else 0} distinct run id(s)",
    )
    checks.add(
        "and the ledger's count agrees with the file beside it",
        ledger.get("tool_calls") == (len(log) if isinstance(log, list) else -1),
        f"{ledger.get('tool_calls')} against {len(log) if isinstance(log, list) else 0}",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--endpoint", default=os.environ.get("BIZDATA_MCP_ENDPOINT", ""))
    parser.add_argument("--token-endpoint", default=os.environ.get("BIZDATA_OAUTH_TOKEN_ENDPOINT", ""))
    parser.add_argument("--client-id", default=os.environ.get("BIZDATA_TEST_CLIENT_ID", ""))
    parser.add_argument("--client-secret", default=os.environ.get("BIZDATA_TEST_CLIENT_SECRET", ""))
    parser.add_argument("--scope", default=os.environ.get("BIZDATA_OAUTH_SCOPES", "bizdata/read"))
    parser.add_argument("--bucket", default=os.environ.get("BIZDATA_RUNS_BUCKET", ""))
    parser.add_argument(
        "--wait-for-expiry", action="store_true",
        help="Sleep 905 seconds and prove the fifteen-minute url really is fifteen minutes.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    missing = [
        name
        for name, value in (
            ("BIZDATA_MCP_ENDPOINT", args.endpoint),
            ("BIZDATA_OAUTH_TOKEN_ENDPOINT", args.token_endpoint),
            ("BIZDATA_TEST_CLIENT_ID", args.client_id),
            ("BIZDATA_TEST_CLIENT_SECRET", args.client_secret),
            ("BIZDATA_RUNS_BUCKET", args.bucket),
        )
        if not value
    ]
    if missing:
        raise SystemExit(
            f"Missing {', '.join(missing)}. Run:\n"
            '  eval "$(terraform -chdir=infra/main output -raw shell_exports)"\n'
            '  eval "$(terraform -chdir=infra/main output -raw auth_exports)"'
        )

    import boto3

    from server.archive import S3Archive

    s3 = boto3.client("s3")
    iam = boto3.client("iam")
    checks = Checks()

    check_bucket(s3, args.bucket, checks)
    check_role_is_scoped(iam, checks)

    token = client_credentials_token(args.token_endpoint, args.client_id, args.client_secret, args.scope)
    urls = check_grant(args.endpoint, token, s3, args.bucket, checks)
    check_expiry(S3Archive(args.bucket), s3, args.bucket, urls, args.wait_for_expiry, checks)
    if urls:
        check_finalise(args.endpoint, token, s3, args.bucket, checks)

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
