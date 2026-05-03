"""Vector and VectorType models."""

from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, model_validator

from .entity import EntityKind

# Load the vector_types.yaml registry once at import time.
_REGISTRY_PATH = Path(__file__).resolve().parent.parent / "vector_types.yaml"
_REGISTRY: dict = {}


def _load_registry() -> dict:
    global _REGISTRY
    if not _REGISTRY:
        with open(_REGISTRY_PATH) as f:
            _REGISTRY = yaml.safe_load(f)
    return _REGISTRY


def get_registry() -> dict:
    """Return the loaded vector types registry."""
    return _load_registry()


class VectorType(BaseModel):
    """Validated against vector_types.yaml open registry."""

    domain: str
    scale: str
    projection: Optional[str] = None

    @model_validator(mode="after")
    def _validate_against_registry(self) -> "VectorType":
        reg = _load_registry()
        if self.domain not in reg.get("domains", {}):
            raise ValueError(
                f"Unknown domain {self.domain!r}. "
                f"Valid: {list(reg.get('domains', {}).keys())}"
            )
        if self.scale not in reg.get("scales", {}):
            raise ValueError(
                f"Unknown scale {self.scale!r}. "
                f"Valid: {list(reg.get('scales', {}).keys())}"
            )
        if self.projection is not None and self.projection not in reg.get(
            "projections", {}
        ):
            raise ValueError(
                f"Unknown projection {self.projection!r}. "
                f"Valid: {list(reg.get('projections', {}).keys())}"
            )
        return self


class Vector(BaseModel):
    """A typed numerical vector stored as Parquet."""

    id: str
    type: VectorType
    experiment_id: str
    entity_kind: EntityKind
    entity_namespace: str
    columns: list[str]
    parquet_path: str  # relative to .kbcache/
    content_hash: str
    derivation: Optional[str] = None
    parents: list[str] = []
    created_at: datetime
