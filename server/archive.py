"""Where a run's artifacts land, and the only write path in the system.

The third seam in this codebase built the way ``server/db.py`` was built, and for the same
reason: the harness runs seventeen seeds on a laptop with no AWS account, the deployed
server runs against S3, and a write layer that only knows how to be one of those is a
rewrite the first time it meets the other. One protocol, two backends, chosen by
``BIZDATA_ARCHIVE_BACKEND``.

Three operations, and deliberately no more. ``presign_put`` mints a time-limited grant to
write exactly one key; ``head`` says whether a key is there and how big; ``put_bytes``
writes the two objects the *server* owns. There is no delete, no list and no read of an
artifact's contents, because nothing in this system needs them and every one of them would
widen the IAM statement.

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


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def content_type_for(filename: str) -> str:
    return CONTENT_TYPES.get(Path(filename).suffix.lower(), "application/octet-stream")


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
    """

    name = "s3"

    def __init__(self, bucket: str | None = None, prefix: str = DEFAULT_PREFIX) -> None:
        import boto3

        resolved = bucket or os.environ.get("BIZDATA_RUNS_BUCKET")
        if not resolved:
            raise ArchiveError(
                "The s3 archive backend needs BIZDATA_RUNS_BUCKET in the environment. "
                "It comes out of `terraform -chdir=infra/main output -raw runs_bucket`."
            )
        self.bucket = str(resolved)
        self.prefix = prefix
        self.client: Any = boto3.client("s3")

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
