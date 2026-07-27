"""Validation report models for session.validate_entities()."""

from typing import Literal, Optional

from pydantic import BaseModel

from .entity import EntityRef


class ValidationIssue(BaseModel):
    """A single validation problem found during entity resolution."""

    kind: Literal[
        "missing_namespace",
        "missing_entity",
        "wrong_kind",
        "missing_parent_experiment",
        "missing_derived_sample",
    ]
    context: str  # human-readable location, e.g., "Strain ACN2586 mutation #2 target"
    ref: Optional[EntityRef] = None
    detail: str


class ValidationReport(BaseModel):
    """Result of session.validate_entities()."""

    issues: list[ValidationIssue] = []
    checked_experiments: int = 0
    checked_strains: int = 0
    checked_vectors: int = 0
    checked_entity_refs: int = 0

    @property
    def ok(self) -> bool:
        return not self.issues
