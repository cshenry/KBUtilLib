"""Filesystem blob store — content-addressed files in .kbcache/blobs/."""

from __future__ import annotations

from pathlib import Path


class BlobStore:
    """Write / read / delete content-addressed blobs."""

    def __init__(self, blobs_dir: Path) -> None:
        self.blobs_dir = blobs_dir
        self.blobs_dir.mkdir(parents=True, exist_ok=True)

    def blob_path(self, content_hash: str, ext: str) -> Path:
        """Return the path for a blob given its hash and extension."""
        return self.blobs_dir / f"{content_hash}{ext}"

    def exists(self, content_hash: str, ext: str) -> bool:
        return self.blob_path(content_hash, ext).exists()

    def write(self, content_hash: str, ext: str, data: bytes) -> Path:
        """Write *data* to the blob file. Returns the path."""
        p = self.blob_path(content_hash, ext)
        p.write_bytes(data)
        return p

    def read(self, content_hash: str, ext: str) -> bytes:
        return self.blob_path(content_hash, ext).read_bytes()

    def delete(self, content_hash: str, ext: str) -> None:
        p = self.blob_path(content_hash, ext)
        if p.exists():
            p.unlink()
