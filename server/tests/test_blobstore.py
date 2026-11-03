"""Local disk and S3 blob stores, plus blobstore_from_env wiring (no network)."""

import io

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import Stubber

from evidence_server.blobstore import (
    BlobNotFound,
    LocalBlobStore,
    S3BlobStore,
    blobstore_from_env,
    validate_key,
)

KEY = "claims/CLM-20261029-ABC123/plate-deadbeef.jpg"
DATA = b"\xff\xd8\xff\xe0fake-jpeg"
BAD_KEYS = [
    "",
    "/claims/x.jpg",
    "claims/../x.jpg",
    "a/../../b.jpg",
    "..",
    "../x.jpg",
    "claims\\x.jpg",
    "claims/a b.jpg",
    "claims/a*.jpg",
    "claims/caf\xc3\xa9.jpg",
    "claims/a?b.jpg",
]


def local(tmp_path) -> LocalBlobStore:
    return LocalBlobStore(tmp_path / "blobs")


def test_put_get_round_trip(tmp_path):
    store = local(tmp_path)
    store.put(KEY, DATA, "image/jpeg")
    assert store.get(KEY) == DATA


def test_exists_true_false(tmp_path):
    store = local(tmp_path)
    assert store.exists(KEY) is False
    store.put(KEY, DATA, "image/jpeg")
    assert store.exists(KEY) is True


def test_delete_then_missing(tmp_path):
    store = local(tmp_path)
    store.put(KEY, DATA, "image/jpeg")
    store.delete(KEY)
    assert store.exists(KEY) is False


def test_delete_missing_is_fine(tmp_path):
    local(tmp_path).delete(KEY)


def test_get_missing_raises(tmp_path):
    with pytest.raises(BlobNotFound):
        local(tmp_path).get(KEY)


def test_overwrite_replaces_bytes(tmp_path):
    store = local(tmp_path)
    store.put(KEY, DATA, "image/jpeg")
    store.put(KEY, DATA + b"v2", "image/jpeg")
    assert store.get(KEY) == DATA + b"v2"


@pytest.mark.parametrize("bad", BAD_KEYS)
def test_bad_keys_rejected_locally(tmp_path, bad: str):
    store = local(tmp_path)
    with pytest.raises(ValueError):
        validate_key(bad)
    with pytest.raises(ValueError):
        store.put(bad, DATA, "image/jpeg")
    with pytest.raises(ValueError):
        store.get(bad)
    with pytest.raises(ValueError):
        store.exists(bad)
    with pytest.raises(ValueError):
        store.delete(bad)


def s3_client():
    return boto3.client(
        "s3",
        region_name="auto",
        endpoint_url="https://example.invalid",
        aws_access_key_id="x",
        aws_secret_access_key="y",
    )


def test_s3_put_sends_content_type():
    store = S3BlobStore("b", s3_client())
    stub = Stubber(store.client)
    stub.add_response(
        "put_object", {}, {"Bucket": "b", "Key": KEY, "Body": DATA, "ContentType": "image/jpeg"}
    )
    with stub:
        store.put(KEY, DATA, "image/jpeg")


def test_s3_get_round_trip():
    store = S3BlobStore("b", s3_client())
    stub = Stubber(store.client)
    stub.add_response(
        "get_object",
        {"Body": StreamingBody(io.BytesIO(DATA), len(DATA))},
        {"Bucket": "b", "Key": KEY},
    )
    with stub:
        assert store.get(KEY) == DATA


def test_s3_get_missing_raises_blob_not_found():
    store = S3BlobStore("b", s3_client())
    stub = Stubber(store.client)
    stub.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
    with stub, pytest.raises(BlobNotFound):
        store.get(KEY)


def test_s3_exists_true_and_404_false():
    store = S3BlobStore("b", s3_client())
    stub = Stubber(store.client)
    stub.add_response("head_object", {}, {"Bucket": "b", "Key": KEY})
    with stub:
        assert store.exists(KEY) is True
    stub = Stubber(store.client)
    stub.add_client_error("head_object", service_error_code="404", http_status_code=404)
    with stub:
        assert store.exists(KEY) is False


def test_s3_other_errors_reraise():
    store = S3BlobStore("b", s3_client())
    stub = Stubber(store.client)
    stub.add_client_error("get_object", service_error_code="AccessDenied", http_status_code=403)
    with stub, pytest.raises(ClientError):
        store.get(KEY)


def test_s3_delete():
    store = S3BlobStore("b", s3_client())
    stub = Stubber(store.client)
    stub.add_response("delete_object", {}, {"Bucket": "b", "Key": KEY})
    with stub:
        store.delete(KEY)


def test_s3_bad_key_rejected_without_a_call():
    store = S3BlobStore("b", s3_client())
    with Stubber(store.client), pytest.raises(ValueError):
        store.put("../x.jpg", DATA, "image/jpeg")


def test_s3_url_returns_presigned_get():
    store = S3BlobStore("b", s3_client())
    url = store.url(KEY)
    assert isinstance(url, str) and "b" in url and KEY in url
    assert store.url(KEY, filename="plate.jpg") is not None
    assert store.url(KEY, expires=60) is not None


def test_s3_url_rejects_bad_key():
    store = S3BlobStore("b", s3_client())
    with pytest.raises(ValueError):
        store.url("../x.jpg")


def test_local_url_is_none_and_path_resolves(tmp_path):
    store = local(tmp_path)
    assert store.url(KEY) is None
    assert store.path(KEY) == store._path(KEY)


def clear_env(monkeypatch):
    for name in (
        "R2_ACCOUNT_ID",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_BUCKET",
        "S3_BUCKET",
        "AWS_ENDPOINT_URL_S3",
    ):
        monkeypatch.delenv(name, raising=False)


def test_from_env_picks_r2(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("R2_ACCOUNT_ID", "  abc123  ")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("R2_BUCKET", "  claims  ")
    store = blobstore_from_env(tmp_path)
    assert isinstance(store, S3BlobStore)
    assert store.bucket == "claims"
    assert "abc123" in store.client.meta.endpoint_url


def test_from_env_picks_generic_s3(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("S3_BUCKET", "  pics  ")
    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "  https://s3.example  ")
    store = blobstore_from_env(tmp_path)
    assert isinstance(store, S3BlobStore)
    assert store.bucket == "pics"


def test_from_env_defaults_to_local(tmp_path, monkeypatch):
    clear_env(monkeypatch)
    store = blobstore_from_env(tmp_path)
    assert isinstance(store, LocalBlobStore)
    assert store.root == tmp_path / "blobs"


# --- origin: where presigned links point, so the console's CSP can allow it ---


def test_local_store_has_no_outside_origin(tmp_path):
    assert local(tmp_path).origin() is None


def test_s3_origin_is_where_presigned_links_point():
    store = S3BlobStore("b", s3_client())
    origin = store.origin()
    assert origin.startswith("https://") and origin.count("/") == 2
    assert store.url(KEY).startswith(origin + "/")


def test_r2_origin_is_the_account_endpoint():
    client = boto3.client(
        "s3",
        region_name="auto",
        endpoint_url="https://acct123.r2.cloudflarestorage.com",
        aws_access_key_id="x",
        aws_secret_access_key="y",
    )
    store = S3BlobStore("claims-evidence", client)
    assert store.origin() == "https://acct123.r2.cloudflarestorage.com"
    assert store.url(KEY).startswith("https://acct123.r2.cloudflarestorage.com/")
