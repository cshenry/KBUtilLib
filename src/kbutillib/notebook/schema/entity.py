"""Entity kind and reference types."""

from enum import Enum

from pydantic import BaseModel


class EntityKind(str, Enum):
    """Kinds of biological entities that can appear in vectors."""

    GENE = "gene"
    REACTION = "reaction"
    METABOLITE = "metabolite"


class EntityRef(BaseModel):
    """Lazy reference to a biological entity.

    No verification at construction; resolved lazily via
    ``session.validate_entities()`` (Phase 2).
    """

    kind: EntityKind
    id: str
    namespace: str  # genome_id or model_id this entity belongs to
