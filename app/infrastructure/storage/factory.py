from __future__ import annotations

from pathlib import Path

import boto3

from app.infrastructure.config.settings import Settings
from app.infrastructure.storage.artifacts import LocalArtifactStore, S3ArtifactStore


def artifact_store_for(settings: Settings):
    if settings.artifact_store_backend == "local":
        return LocalArtifactStore(settings.world_asset_dir)
    if not settings.s3_bucket:
        raise ValueError("S3_BUCKET is required for the S3 artifact store.")
    client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key_id.get_secret_value() or None,
        aws_secret_access_key=settings.s3_secret_access_key.get_secret_value() or None,
    )
    return S3ArtifactStore(settings.s3_bucket, client, Path(settings.world_asset_dir) / "s3-cache")
