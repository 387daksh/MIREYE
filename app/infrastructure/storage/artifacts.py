from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ArtifactIntegrityError(ValueError):
    pass


class LocalArtifactStore:
    """Content-addressed local storage; directories are created only on writes."""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    def put(self, data: bytes, *, extension: str, media_type: str, role: str) -> dict:
        digest = _sha256(data)
        relative = Path("sha256") / digest[:2] / f"{digest}.{extension.lstrip('.')}"
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_bytes(data)
            temporary.replace(path)
        return {
            "sha256": digest,
            "byte_size": len(data),
            "media_type": media_type,
            "role": role,
            "storage_key": relative.as_posix(),
        }

    def path(self, artifact: dict) -> Path:
        path = (self.root / artifact["storage_key"]).resolve()
        if self.root.resolve() not in path.parents or not path.is_file():
            raise ArtifactIntegrityError("World artifact was not found.")
        if _sha256(path.read_bytes()) != artifact["sha256"]:
            raise ArtifactIntegrityError("World artifact hash verification failed.")
        return path


class S3ArtifactStore:
    """S3-compatible adapter using an injected client and verified local cache."""

    def __init__(self, bucket: str, client: Any, cache_root: Path | str):
        self.bucket, self.client, self.cache_root = bucket, client, Path(cache_root)

    def put(self, data: bytes, *, extension: str, media_type: str, role: str) -> dict:
        digest = _sha256(data)
        key = f"sha256/{digest[:2]}/{digest}.{extension.lstrip('.')}"
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=media_type,
            Metadata={"sha256": digest, "role": role},
        )
        return {"sha256": digest, "byte_size": len(data), "media_type": media_type, "role": role, "storage_key": key}

    def path(self, artifact: dict) -> Path:
        path = self.cache_root / artifact["storage_key"]
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            self.client.download_file(self.bucket, artifact["storage_key"], str(path))
        if _sha256(path.read_bytes()) != artifact["sha256"]:
            raise ArtifactIntegrityError("S3 artifact hash verification failed.")
        return path
