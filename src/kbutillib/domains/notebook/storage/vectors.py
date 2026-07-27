"""Parquet read/write for vector data."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd


class VectorStorage:
    """Read and write Parquet files in .kbcache/vectors/."""

    def __init__(self, vectors_dir: Path) -> None:
        self.vectors_dir = vectors_dir
        self.vectors_dir.mkdir(parents=True, exist_ok=True)

    def parquet_path(self, vector_id: str) -> Path:
        return self.vectors_dir / f"{vector_id}.parquet"

    def relative_path(self, vector_id: str) -> str:
        """Return the path relative to .kbcache/ (for catalog storage)."""
        return f"vectors/{vector_id}.parquet"

    def write(self, vector_id: str, df: pd.DataFrame) -> tuple[Path, str, int]:
        """Write a DataFrame to parquet. Returns (path, content_hash, n_bytes)."""
        p = self.parquet_path(vector_id)
        df.to_parquet(p, engine="pyarrow")
        data = p.read_bytes()
        content_hash = hashlib.sha256(data).hexdigest()
        return p, content_hash, len(data)

    def read(self, vector_id: str) -> pd.DataFrame:
        return pd.read_parquet(self.parquet_path(vector_id), engine="pyarrow")

    def delete(self, vector_id: str) -> None:
        p = self.parquet_path(vector_id)
        if p.exists():
            p.unlink()
