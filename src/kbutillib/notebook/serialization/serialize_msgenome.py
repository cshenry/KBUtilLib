"""MSGenome serializer — uses MSGenome.to_json() / from_json()."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import register_serializer


class MSGenomeSerializer:
    type_name = "msgenome"
    file_extension = ".json"

    def can_handle(self, obj: Any) -> bool:
        return type(obj).__name__ == "MSGenome" and hasattr(obj, "to_json")

    def serialize(self, obj: Any, path: Path) -> dict[str, Any]:
        json_str = obj.to_json()
        path.write_text(json_str, encoding="utf-8")
        return {}

    def deserialize(self, path: Path, metadata: dict) -> Any:
        try:
            from modelseedpy.core.msgenome import MSGenome
        except ImportError:
            raise ImportError(
                "modelseedpy is required to deserialize MSGenome objects. "
                "Install with: pip install modelseedpy"
            )
        return MSGenome.from_json(path.read_text(encoding="utf-8"))


register_serializer(MSGenomeSerializer())
