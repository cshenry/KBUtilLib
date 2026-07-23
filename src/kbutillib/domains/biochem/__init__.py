"""Biochemistry domain — pydantic v2 request/response schemas and utilities.

Import the schemas lazily (inside functions or at module level in
non-hot paths) to avoid pulling in pydantic at top-level when not needed.

Example::

    from kbutillib.domains.biochem.schemas import (
        SearchCompoundsInput,
        SearchCompoundsOutput,
        GetCompoundByIdInput,
        GetCompoundByIdOutput,
        GetReactionByIdInput,
        GetReactionByIdOutput,
    )

    from kbutillib.domains.biochem import MSBiochemUtils, MSBiochemUtilsImpl
"""

from .ms_biochem_utils import (
    MSBiochemUtils,
    MSBiochemUtilsImpl,
    compartment_types,
)
from .schemas import (
    GetCompoundByIdInput,
    GetCompoundByIdOutput,
    GetReactionByIdInput,
    GetReactionByIdOutput,
    SearchCompoundsInput,
    SearchCompoundsOutput,
)

__all__ = [
    # WP3 schemas
    "SearchCompoundsInput",
    "SearchCompoundsOutput",
    "GetCompoundByIdInput",
    "GetCompoundByIdOutput",
    "GetReactionByIdInput",
    "GetReactionByIdOutput",
    # WP13 utilities
    "MSBiochemUtils",
    "MSBiochemUtilsImpl",
    "compartment_types",
]
