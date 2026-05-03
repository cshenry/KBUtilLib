"""Strain and Mutation models."""

from typing import Literal, Optional

from pydantic import BaseModel

from .entity import EntityRef


class Mutation(BaseModel):
    """A genetic modification applied to a strain."""

    kind: Literal[
        "knockout", "knockin", "point", "insertion", "deletion", "overexpression"
    ]
    target: EntityRef  # gene
    source_organism: Optional[str] = None
    source_gene: Optional[str] = None
    description: Optional[str] = None


class Strain(BaseModel):
    """A biological strain with optional mutations."""

    id: str
    parent_genome: str  # name of MSGenome blob in catalog
    mutations: list[Mutation] = []
    description: Optional[str] = None
