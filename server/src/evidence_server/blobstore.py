"""Object storage for claim files: local disk for dev/tests, S3 API for prod.

Blob keys look like ``claims/CLM-20261029-ABC123/plate-<hex>.jpg``. Production
points at Cloudflare R2, which speaks the S3 API, so one boto3 client covers both.
"""

import os
import re
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import boto3
from botocore.exceptions import ClientError

_KEY_RE = re.compile(r"[A-Za-z0-9._/\-]+")
_MISSING_CODES = {"NoSuchKey", "NotFound", "404"}


class BlobNotFound(KeyError):
    ...


class BlobStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...
    def url(self, key: str, filename: str | None = None, expires: int = 300) -> str | None: ...
    def origin(self) -> str | None: ...


def validate_key(key: str) -> None:
    if not key or key.startswith("/") or "\\" in key:
        raise ValueError(f"Bad blob key: {key!r}")
    if ".." in key.split("/"):
        raise ValueError(f"Bad blob key: {key!r}")
    if not _KEY_RE.fullmatch(key):
        raise ValueError(f"Bad blob key: {key!r}")


def _is_missing(e: ClientError) -> bool:
    code = e.response.get("Error", {}).get("Code", "")
    status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in _MISSING_CODES or status == 404


class LocalBlobStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        validate_key(key)
        return self.root / key

    def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
            f.write(data)
            tmp = f.name
        os.replace(tmp, path)

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            raise BlobNotFound(key) from None

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def url(self, key: str, filename: str | None = None, expires: int = 300) -> str | None:
        return None

    def origin(self) -> str | None:
        return None

    def path(self, key: str) -> Path:
        return self._path(key)


class S3BlobStore:
    def __init__(self, bucket: str, client: Any):
        self.bucket = bucket
        self.client = client

    def put(self, key: str, data: bytes, content_type: str) -> None:
        validate_key(key)
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def get(self, key: str) -> bytes:
        validate_key(key)
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if _is_missing(e):
                raise BlobNotFound(key) from None
            raise
        with closing(resp["Body"]) as body:
            return body.read()

    def exists(self, key: str) -> bool:
        validate_key(key)
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if _is_missing(e):
                return False
            raise
        return True

    def delete(self, key: str) -> None:
        validate_key(key)
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def url(self, key: str, filename: str | None = None, expires: int = 300) -> str | None:
        validate_key(key)
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires,
        )

    def origin(self) -> str:
        """Scheme and host of the presigned links (signed locally, no network call)."""
        parts = urlsplit(self.url("origin-probe", expires=1))
        return f"{parts.scheme}://{parts.netloc}"


def blobstore_from_env(data_dir: Path) -> BlobStore:
    account = os.environ.get("R2_ACCOUNT_ID", "").strip()
    if account:
        client = boto3.client(
            "s3",
            endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
            region_name="auto",
            aws_access_key_id=os.environ.get("R2_ACCESS_KEY_ID", "").strip(),
            aws_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY", "").strip(),
        )
        return S3BlobStore(os.environ.get("R2_BUCKET", "").strip(), client)
    bucket = os.environ.get("S3_BUCKET", "").strip()
    endpoint = os.environ.get("AWS_ENDPOINT_URL_S3", "").strip()
    if bucket and endpoint:
        return S3BlobStore(bucket, boto3.client("s3", endpoint_url=endpoint))
    return LocalBlobStore(Path(data_dir) / "blobs")
