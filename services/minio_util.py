"""MinIO 封装：上传、下载对象。"""
import io
import urllib.parse

from minio import Minio

import config


def _parse_endpoint(endpoint: str):
    """把 http://host:port 解析成 (host, secure)。"""
    parts = urllib.parse.urlparse(endpoint)
    return parts.netloc, parts.scheme == "https"


def get_client() -> Minio:
    host, secure = _parse_endpoint(config.MINIO_ENDPOINT)
    return Minio(
        host,
        access_key=config.MINIO_ACCESS_KEY,
        secret_key=config.MINIO_SECRET_KEY,
        secure=secure,
    )


def download_bytes(object_key: str) -> bytes:
    client = get_client()
    resp = client.get_object(config.MINIO_BUCKET, object_key)
    try:
        return resp.read()
    finally:
        resp.close()
        resp.release_conn()


def upload_bytes(object_key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    client = get_client()
    client.put_object(
        config.MINIO_BUCKET,
        object_key,
        io.BytesIO(data),
        length=len(data),
        content_type=content_type,
    )
    return object_key


def ensure_bucket() -> None:
    client = get_client()
    if not client.bucket_exists(config.MINIO_BUCKET):
        client.make_bucket(config.MINIO_BUCKET)
