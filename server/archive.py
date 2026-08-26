"""Where a run's artifacts land, and the only write path in the system.

The third seam in this codebase built the way ``server/db.py`` was built, and for the same
reason: the harness runs seventeen seeds on a laptop with no AWS account, the deployed
server runs against S3, and a write layer that only knows how to be one of those is a
rewrite the first time it meets the other. One protocol, two backends, chosen by
``BIZDATA_ARCHIVE_BACKEND``.

Four operations, and deliberately no more. ``presign_put`` mints a time-limited grant to
write exactly one key; ``head`` says whether a key is there and how big; ``put_bytes``
writes the objects the *server* owns; ``get_bytes`` reads back one of them. There is still
no delete and no list, because nothing needs them and both would widen the IAM statement.

``get_bytes`` arrived at step 12 and is the one that looks like it should have. It does
not: ``s3:GetObject`` on ``runs/*`` has been granted since step 9, because HeadObject is
authorised as GetObject and ``head`` is how ``publish_pack`` checks that the artifacts it
is about to vouch for arrived. So the run ledger became readable without a single line of
Terraform moving, which is why step 12 has no ``infra/`` change. The read is also narrow in
the way that matters: the only caller is ``get_run_ledger``, and the only thing it reads is
the period pointer the server itself wrote.

A missing key is a ``None``, never an exception. A period nobody has run yet *is* a missing
key, and it has to read as "first run" rather than as a failure — the same rule ``head``
already follows, and for the same reason.

The split matters and is the design of step 9. The agent produces the workbook and the
deck, so it gets a presigned URL for each — a credential that can write one key and then
stops working, rather than an AWS credential. The tool-call log and the ledger entry are
the *server's* record of what happened, so the server writes them itself, after checking
that the artifacts it is about to vouch for actually arrived.

On the local backend's honesty. ``LocalArchive`` models S3's grant semantics rather than
implementing them: a token that names one key, refuses any other, and expires. That is
enough for scripts/check_publish.py to assert the *logic* on all seventeen seeds, and it is
not evidence that S3 behaves that way. scripts/check_archive.py proves the real thing
against the real bucket, which is the same division of labour as check_tools.py running in
memory and check_auth.py running over HTTPS.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import secrets
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

REPO_ROOT = Path(__file__).resolve().parents[1]

# Everything a run writes sits under this one prefix, which is also the prefix the Lambda's
# IAM statement is scoped to. Key confinement is therefore enforced twice: once here, where
# the key is built, and once in IAM, where a key built wrongly would still be refused.
DEFAULT_PREFIX = "runs"

# Step 12's index: one object per period, holding the run that last covered it. Under the
# same prefix, so the existing IAM statements reach it and nothing in infra/ moves.
#
# The leading underscore is a fence rather than a convention. A run's objects are keyed
# runs/<run_id>/<filename>, and RUN_ID_RE in server/tools/publish_pack.py requires a run id
# to begin with a letter or digit — so no run can ever be called "_periods", and no
# presigned URL handed to an agent can be signed for a key under here. The pointer is
# reachable only by the server, because the only way in is a key the server built.
PERIODS_PREFIX = f"{DEFAULT_PREFIX}/_periods"

# Fifteen minutes, from the decision record. Long enough for a sandbox to write two files it
# already has on disk, short enough that a URL captured from a transcript is worthless by
# the time anyone reads the transcript.
DEFAULT_TTL_SECONDS = 900

DEFAULT_LOCAL_DIR = REPO_ROOT / "build" / "archive"

CONTENT_TYPES = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".json": "application/json",
}


class ArchiveError(RuntimeError):
    """The archive could not do what was asked. Never raised for a missing key — that is a
    ``None`` from ``head`` and a decision for the caller."""


@dataclass(frozen=True)
class ObjectInfo:
    """What is actually in the archive at a key, read back rather than remembered."""

    key: str
    size_bytes: int
    etag: str


@dataclass(frozen=True)
class Grant:
    """One-key, one-method, time-limited permission to write."""

    key: str
    url: str
    expires_at: dt.datetime


class Archive(Protocol):
    name: str

    @property
    def destination(self) -> str: ...

    def presign_put(self, key: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> Grant: ...

    def head(self, key: str) -> ObjectInfo | None: ...

    def put_bytes(self, key: str, data: bytes, content_type: str) -> ObjectInfo: ...

    def get_bytes(self, key: str) -> bytes | None: ...


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def content_type_for(filename: str) -> str:
    return CONTENT_TYPES.get(Path(filename).suffix.lower(), "application/octet-stream")


def period_key(period_label: str) -> str:
    """The pointer key for one period, built here so both callers build it identically.

    ``publish_pack`` writes it and ``get_run_ledger`` reads it, and a key assembled twice
    is a key that can be assembled two ways. ``period_label`` is always the output of
    ``server.tools.common.as_period_start`` formatted ``%Y-%m``, so it is four digits, a
    hyphen and two digits by construction and never a caller's string.
    """
    return f"{PERIODS_PREFIX}/{period_label}.json"


# ------------------------------------------------------------------------------------ S3


class S3Archive:
    """The real archive: one bucket, one prefix, signed with the Lambda's own role.

    Nothing here passes ``ContentType`` into the signature, and that is a decision rather
    than an omission. Signing the content type binds the uploader to send a byte-identical
    header, which buys nothing the key scoping does not already give — the URL can still
    only write the one key — and costs a Cowork run that fails on a header mismatch with an
    S3 error message that does not say which header. The server sets the content type on
    the two objects it writes itself, where it knows it is right.

    A presigned URL is only valid while the credentials that signed it are, so in Lambda the
    real ceiling on a URL's life is the execution role's session rather than ``ExpiresIn``.
    Lambda refreshes those on a scale of hours and this asks for fifteen minutes, so the
    stated expiry is always the binding one — but it is the direction the surprise would
    come from if a URL ever died early.

    The signature version is set explicitly, and it has to be. Left to itself boto3 presigns
    S3 URLs with **SigV2** — the giveaway is ``AWSAccessKeyId=`` in the query string where
    SigV4 puts ``X-Amz-Algorithm=AWS4-HMAC-SHA256`` — against the global ``s3.amazonaws.com``
    endpoint, and a SigV2 URL signed for the wrong region is refused by a bucket in us-west-2
    with ``403 SignatureDoesNotMatch``. That is the same status and the same error code S3
    returns when a URL is used on a key it was not signed for, so the bug and the security
    property it breaks are indistinguishable from the outside: every refusal assertion in
    scripts/check_archive.py passed while nothing could be uploaded at all. The positive
    assertion is what caught it.
    """

    name = "s3"

    def __init__(self, bucket: str | None = None, prefix: str = DEFAULT_PREFIX) -> None:
        import boto3
        from botocore.config import Config

        resolved = bucket or os.environ.get("BIZDATA_RUNS_BUCKET")
        if not resolved:
            raise ArchiveError(
                "The s3 archive backend needs BIZDATA_RUNS_BUCKET in the environment. "
                "It comes out of `terraform -chdir=infra/main output -raw runs_bucket`."
            )
        self.bucket = str(resolved)
        self.prefix = prefix
        # Region explicitly too, for the other half of the same problem: SigV4 binds the
        # region into the signature, so signing for us-east-1 against a us-west-2 bucket
        # fails the same way. Lambda always sets AWS_REGION; locally boto3 resolves it from
        # the profile, which is why the fallback is None rather than a guess.
        self.client: Any = boto3.client(
            "s3",
            region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
            config=Config(signature_version="s3v4"),
        )

    @property
    def destination(self) -> str:
        return f"s3://{self.bucket}"

    def presign_put(self, key: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> Grant:
        url = self.client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=int(ttl_seconds),
            HttpMethod="PUT",
        )
        return Grant(key=key, url=url, expires_at=utcnow() + dt.timedelta(seconds=int(ttl_seconds)))

    def head(self, key: str) -> ObjectInfo | None:
        try:
            response = self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # botocore.exceptions.ClientError, without importing it
            if _is_not_found(exc):
                return None
            raise ArchiveError(f"head s3://{self.bucket}/{key} failed: {exc}") from exc
        return ObjectInfo(
            key=key,
            size_bytes=int(response.get("ContentLength", 0)),
            etag=str(response.get("ETag", "")).strip('"'),
        )

    def put_bytes(self, key: str, data: bytes, content_type: str) -> ObjectInfo:
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
        except Exception as exc:
            raise ArchiveError(f"put s3://{self.bucket}/{key} failed: {exc}") from exc
        return ObjectInfo(key=key, size_bytes=len(data), etag=hashlib.md5(data).hexdigest())

    def get_bytes(self, key: str) -> bytes | None:
        """Read one key back, or ``None`` if it is not there.

        This is the one place S3's error model has to be met head on. A caller **without**
        ``s3:ListBucket`` gets ``403 AccessDenied`` for a key that does not exist, not
        ``404 NoSuchKey`` — S3 refuses to confirm absence to anyone who is not allowed to
        enumerate, so the two answers are deliberately indistinguishable from outside.

        This role does not have ``ListBucket``, and it is not getting it. Granting it is
        the obvious way to make 404 mean 404, and it would cost the property step 9's IAM
        comment names: no enumeration of other runs. A prefix condition does not help,
        because the existence check evaluates ``ListBucket`` with no ``s3:prefix`` in
        context, so a conditioned grant is denied and the answer is 403 again.

        So both are read as absent, and the reason that is safe here rather than sloppy is
        that the permission is not in question: the same policy document that grants
        ``PutObject`` on ``runs/*`` grants ``GetObject`` on ``runs/*``, and the only key
        this method is ever called with is one the server itself wrote under that prefix.
        A denial that is not an absence would mean the policy had been changed underneath
        the function, which is a deploy-time fact rather than a runtime one.

        Left unhandled this is the sharpest local-versus-deployed split in the build:
        LocalArchive returns None for a missing file, so a first run on a period passes
        every seeded harness and raises on Lambda. scripts/check_archive.py asserts the
        real behaviour against the real bucket, which is the only place it can be proven.
        """
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            if _is_not_found(exc) or _is_denied(exc):
                return None
            raise ArchiveError(f"get s3://{self.bucket}/{key} failed: {exc}") from exc
        return bytes(response["Body"].read())


def _is_not_found(exc: Exception) -> bool:
    """A 404 from head_object, without importing botocore to find out.

    boto3 raises ``ClientError`` for both "no such key" and "no permission", and the two are
    genuinely different answers to step 9's question. Reading the status code rather than
    the message is what keeps a permissions mistake from being reported as a missing file.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return status == 404 or response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound")


def _is_denied(exc: Exception) -> bool:
    """A 403 from S3, which for a reader without ListBucket is also how absence looks.

    Kept separate from ``_is_not_found`` on purpose. ``head`` still treats a 403 as a real
    error, because there it would mean the server could not verify an artifact it is about
    to vouch for and saying "it did not arrive" would be a lie. Only ``get_bytes`` accepts
    it as absence, and only for a key under a prefix this role provably holds GetObject on.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return status == 403 or response.get("Error", {}).get("Code") in ("403", "AccessDenied")


# --------------------------------------------------------------------------------- local


class LocalArchive:
    """A directory that behaves like the bucket, close enough to assert against.

    The grant table is the point. S3 refuses a presigned URL used against another key
    because the key is inside the signature; this refuses one because the token names a key
    and the path has to match it. Same semantics, different mechanism, and the docstring at
    the top of this module says plainly which one is proof.

    Grants are held in the instance rather than on disk, so they die with the process. That
    is correct for a harness and would be wrong for anything else, which is one more reason
    this backend is never the deployed one.
    """

    name = "local"

    def __init__(self, root: str | Path | None = None, prefix: str = DEFAULT_PREFIX) -> None:
        self.root = Path(root or os.environ.get("BIZDATA_ARCHIVE_DIR") or DEFAULT_LOCAL_DIR)
        self.prefix = prefix
        self.root.mkdir(parents=True, exist_ok=True)
        self._grants: dict[str, Grant] = {}

    @property
    def destination(self) -> str:
        return f"file://{self.root}"

    def _path(self, key: str) -> Path:
        return self.root / key

    def presign_put(self, key: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> Grant:
        token = secrets.token_urlsafe(24)
        expires_at = utcnow() + dt.timedelta(seconds=int(ttl_seconds))
        url = (
            f"file://{self.root}/{urllib.parse.quote(key)}"
            f"?token={token}&expires={int(expires_at.timestamp())}"
        )
        grant = Grant(key=key, url=url, expires_at=expires_at)
        self._grants[token] = grant
        return grant

    def accept_put(self, url: str, data: bytes) -> ObjectInfo:
        """Write through a grant, the way an HTTPS PUT writes through a presigned URL.

        Only scripts/check_publish.py calls this. It exists so the local harness can assert
        that a grant writes its one key and refuses any other, rather than reaching around
        the grant and writing the file directly — which would make every one of those
        assertions a test of the harness.
        """
        parsed = urllib.parse.urlparse(url)
        token = urllib.parse.parse_qs(parsed.query).get("token", [""])[0]
        grant = self._grants.get(token)
        if grant is None:
            raise ArchiveError("no such grant: the URL was not minted by this archive")
        if utcnow() > grant.expires_at:
            raise ArchiveError(f"grant expired at {grant.expires_at.isoformat()}")

        asked = urllib.parse.unquote(parsed.path).removeprefix(f"{self.root}/")
        if asked != grant.key:
            raise ArchiveError(f"grant is for {grant.key}, not {asked}")

        return self.put_bytes(grant.key, data, content_type_for(grant.key))

    def head(self, key: str) -> ObjectInfo | None:
        path = self._path(key)
        if not path.is_file():
            return None
        data = path.read_bytes()
        return ObjectInfo(key=key, size_bytes=len(data), etag=hashlib.md5(data).hexdigest())

    def put_bytes(self, key: str, data: bytes, content_type: str) -> ObjectInfo:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return ObjectInfo(key=key, size_bytes=len(data), etag=hashlib.md5(data).hexdigest())

    def get_bytes(self, key: str) -> bytes | None:
        """A missing file is None, which is the one thing this backend gets right for free.

        Worth naming what it therefore cannot prove. S3 answers a missing key with 403
        rather than 404 for a reader without ListBucket, and ``S3Archive.get_bytes`` folds
        both into None to survive it. A directory has no such distinction to make, so every
        assertion in scripts/check_ledger.py about a period that has never run passes here
        whether or not that fold exists. scripts/check_archive.py is where it is real.
        """
        path = self._path(key)
        return path.read_bytes() if path.is_file() else None


# ------------------------------------------------------------------------------ selection


BACKENDS = {"local": LocalArchive, "s3": S3Archive}


def get_archive(backend: str | None = None) -> Archive:
    name = (backend or os.environ.get("BIZDATA_ARCHIVE_BACKEND") or "local").lower()
    try:
        cls = BACKENDS[name]
    except KeyError:
        raise SystemExit(
            f"Unknown BIZDATA_ARCHIVE_BACKEND {name!r}. Expected one of: {', '.join(sorted(BACKENDS))}."
        ) from None
    return cls()  # type: ignore[return-value]


_archive: Archive | None = None


def archive() -> Archive:
    """The process-wide archive, built on first use.

    Lazy for the same reason ``db.backend`` is: importing server.app must not require a
    bucket to exist, or a reachable AWS account, or credentials.
    """
    global _archive
    if _archive is None:
        _archive = get_archive()
    return _archive
