"""COBRA model serializer — uses cobra.io.json."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import register_serializer


class CobraModelSerializer:
    type_name = "cobra_model"
    file_extension = ".json"

    def can_handle(self, obj: Any) -> bool:
        try:
            import cobra
            return isinstance(obj, cobra.Model)
        except ImportError:
            return False

    def serialize(self, obj: Any, path: Path) -> dict[str, Any]:
        import cobra.io
        cobra.io.save_json_model(obj, str(path))
        return {"model_id": obj.id, "n_reactions": len(obj.reactions)}

    def deserialize(self, path: Path, metadata: dict) -> Any:
        try:
            import cobra.io
        except ImportError:
            raise ImportError(
                "cobra is required to deserialize COBRA model objects. "
                "Install with: pip install cobra"
            )
        return cobra.io.load_json_model(str(path))


register_serializer(CobraModelSerializer())
