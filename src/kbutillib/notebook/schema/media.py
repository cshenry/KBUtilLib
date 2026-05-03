"""Media model."""

from typing import Literal, Optional

from pydantic import BaseModel


class Media(BaseModel):
    """Growth media specification."""

    id: str
    source: Literal["kbase", "msmedia", "inline"] = "kbase"
    inline_composition: Optional[dict[str, float]] = None  # cpd_id -> mM
