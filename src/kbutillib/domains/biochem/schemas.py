"""Pydantic v2 request/response schemas for the ``biochem`` capability domain.

These are faithful to the real method signatures and return shapes of
``MSBiochemUtilsImpl``.  They are imported lazily inside ``@capability``
decorator calls so pydantic is not required at module import time.

Classes
-------
SearchCompoundsInput / SearchCompoundsOutput
    ``MSBiochemUtilsImpl.search_compounds(query_identifiers, query_structures,
    query_formula)``
GetCompoundByIdInput / GetCompoundByIdOutput
    ``MSBiochemUtilsImpl.get_compound_by_id(compound_id)``
GetReactionByIdInput / GetReactionByIdOutput
    ``MSBiochemUtilsImpl.get_reaction_by_id(reaction_id)``
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

__all__ = [
    "SearchCompoundsInput",
    "SearchCompoundsOutput",
    "GetCompoundByIdInput",
    "GetCompoundByIdOutput",
    "GetReactionByIdInput",
    "GetReactionByIdOutput",
]


# ---------------------------------------------------------------------------
# search_compounds
# ---------------------------------------------------------------------------


class SearchCompoundsInput(BaseModel):
    """Input parameters for ``MSBiochemUtilsImpl.search_compounds``.

    Mirrors the method signature::

        search_compounds(
            query_identifiers: list[str] = [],
            query_structures: list[str] = [],
            query_formula: str | None = None,
        ) -> dict[str, dict]
    """

    query_identifiers: list[str] = Field(
        default_factory=list,
        description=(
            "Free-text or structured identifiers to search by.  Accepts "
            "ModelSEED IDs (e.g. 'cpd00001'), names ('glucose'), synonyms, "
            "or cross-reference aliases (e.g. 'KEGG:C00031')."
        ),
    )
    query_structures: list[str] = Field(
        default_factory=list,
        description=(
            "Chemical structure strings to search by (InChI, InChIKey, "
            "SMILES).  The hash type is auto-detected."
        ),
    )
    query_formula: str | None = Field(
        default=None,
        description=(
            "Molecular formula string (e.g. 'C6H12O6').  When provided, "
            "the search scores hits by element-count agreement."
        ),
    )


class SearchCompoundsOutput(BaseModel):
    """Output of ``MSBiochemUtilsImpl.search_compounds``.

    The method returns a ``dict[compound_id, hit_info]``.  This schema
    captures that as a mapping keyed by ModelSEED compound ID.  Each hit
    contains a numeric score and categorised match evidence.

    The ``hits`` mapping value is typed as ``dict[str, Any]`` because the
    exact sub-structure (identifier_hits, formula_hits, structure_hits,
    score) is an internal detail that may evolve.
    """

    hits: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Mapping from ModelSEED compound ID to hit metadata.  "
            "Each value contains at minimum a ``score`` (int), "
            "``identifier_hits``, ``formula_hits``, and ``structure_hits`` "
            "sub-dicts."
        ),
    )


# ---------------------------------------------------------------------------
# get_compound_by_id
# ---------------------------------------------------------------------------


class GetCompoundByIdInput(BaseModel):
    """Input parameters for ``MSBiochemUtilsImpl.get_compound_by_id``.

    Mirrors the method signature::

        get_compound_by_id(compound_id: str) -> Any | None
    """

    compound_id: str = Field(
        description=(
            "ModelSEED compound identifier (e.g. 'cpd00001' for water, "
            "'cpd00072' for glucose-6-phosphate)."
        ),
    )


class GetCompoundByIdOutput(BaseModel):
    """Output of ``MSBiochemUtilsImpl.get_compound_by_id``.

    The method returns a modelseedpy ``ModelSEEDCompound`` object or
    ``None`` if the ID is not found.  Since the compound object is a
    library-specific type (not a plain dict), this schema captures the
    result as an ``Any``-typed ``compound`` field — callers should
    treat ``None`` as "not found".
    """

    compound: Any = Field(
        default=None,
        description=(
            "The ModelSEED compound object for the given ID, or ``None`` "
            "if the compound was not found in the database."
        ),
    )

    model_config = {"arbitrary_types_allowed": True}


# ---------------------------------------------------------------------------
# get_reaction_by_id
# ---------------------------------------------------------------------------


class GetReactionByIdInput(BaseModel):
    """Input parameters for ``MSBiochemUtilsImpl.get_reaction_by_id``.

    Mirrors the method signature::

        get_reaction_by_id(reaction_id: str) -> Any | None
    """

    reaction_id: str = Field(
        description=(
            "ModelSEED reaction identifier (e.g. 'rxn00001' for "
            "phosphoglucose isomerase)."
        ),
    )


class GetReactionByIdOutput(BaseModel):
    """Output of ``MSBiochemUtilsImpl.get_reaction_by_id``.

    The method returns a modelseedpy ``ModelSEEDReaction`` object or
    ``None`` if the ID is not found.  Typed as ``Any`` for the same reason
    as :class:`GetCompoundByIdOutput`.
    """

    reaction: Any = Field(
        default=None,
        description=(
            "The ModelSEED reaction object for the given ID, or ``None`` "
            "if the reaction was not found in the database."
        ),
    )

    model_config = {"arbitrary_types_allowed": True}
