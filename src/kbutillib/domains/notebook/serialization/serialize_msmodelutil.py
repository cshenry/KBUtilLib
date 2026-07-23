"""MSModelUtil serializer — wraps cobra.io.json + MSModelUtil restore."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import register_serializer


class MSModelUtilSerializer:
    type_name = "msmodelutil"
    file_extension = ".json"

    def can_handle(self, obj: Any) -> bool:
        return type(obj).__name__ == "MSModelUtil" and hasattr(obj, "model")

    def serialize(self, obj: Any, path: Path) -> dict[str, Any]:
        import cobra.io
        # Save the underlying cobra model
        cobra.io.save_json_model(obj.model, str(path))
        return {"model_id": obj.model.id}

    def deserialize(self, path: Path, metadata: dict) -> Any:
        try:
            import cobra.io
            from modelseedpy.core.msmodelutl import MSModelUtil
        except ImportError:
            raise ImportError(
                "modelseedpy and cobra are required to deserialize MSModelUtil objects. "
                "Install with: pip install modelseedpy cobra"
            )
        model = cobra.io.load_json_model(str(path))
        return MSModelUtil(model)


register_serializer(MSModelUtilSerializer())
