"""Experiment models — Sample, Computation, ExternalDataset, and the discriminated union."""

from datetime import datetime
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, field_validator, model_validator

from .media import Media


class Sample(BaseModel):
    """Wet-lab experiment."""

    id: str
    media: Media
    strains: dict[str, float]  # strain_id -> abundance (sums to 1.0)
    replicates: list[str] = []
    description: Optional[str] = None

    @field_validator("strains")
    @classmethod
    def _abundances_sum_to_one(cls, v: dict[str, float]) -> dict[str, float]:
        if not v:
            raise ValueError("strains must not be empty")
        if any(abundance <= 0 for abundance in v.values()):
            raise ValueError(
                f"all strain abundances must be > 0, got {v}"
            )
        total = sum(v.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"strain abundances must sum to 1.0, got {total}"
            )
        return v


class Computation(BaseModel):
    """In-silico experiment."""

    id: str
    model_ref: str  # name of stored model blob
    media: Media
    parameters: dict[str, Any] = {}
    derived_from_sample: Optional[str] = None
    description: Optional[str] = None


class ExternalDataset(BaseModel):
    """Literature / public / collaborator dataset."""

    id: str
    source: Literal["literature", "public_db", "collaborator", "other"]
    citation: Optional[str] = None
    url: Optional[str] = None
    organism: Optional[str] = None
    description: Optional[str] = None


class Experiment(BaseModel):
    """Top-level experiment — discriminated union over Sample | Computation | ExternalDataset."""

    id: str
    kind: Literal["sample", "computation", "external"]
    payload: Union[Sample, Computation, ExternalDataset]
    notebook: Optional[str] = None
    parents: list[str] = []
    created_at: datetime

    @model_validator(mode="after")
    def _payload_matches_kind(self) -> "Experiment":
        expected = {
            "sample": Sample,
            "computation": Computation,
            "external": ExternalDataset,
        }
        if not isinstance(self.payload, expected[self.kind]):
            raise ValueError(
                f"kind={self.kind!r} but payload is {type(self.payload).__name__}"
            )
        return self
