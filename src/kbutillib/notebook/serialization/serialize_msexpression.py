"""MSExpression serializer — DataFrame (_data) as parquet + metadata as JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import register_serializer


class MSExpressionSerializer:
    type_name = "msexpression"
    file_extension = ".parquet"

    def can_handle(self, obj: Any) -> bool:
        return type(obj).__name__ == "MSExpression" and hasattr(obj, "_data")

    def serialize(self, obj: Any, path: Path) -> dict[str, Any]:
        # Write the DataFrame as parquet
        obj._data.to_parquet(path, engine="pyarrow")
        # Write metadata sidecar as JSON
        meta_path = path.with_suffix(".meta.json")
        meta = {
            "genome_ref": getattr(obj, "genome_ref", None),
            "expression_set_ref": getattr(obj, "expression_set_ref", None),
            "condition": getattr(obj, "condition", None),
        }
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return {"meta_path": str(meta_path.name)}

    def deserialize(self, path: Path, metadata: dict) -> Any:
        try:
            import pandas as pd
            from modelseedpy.core.msexpression import MSExpression
        except ImportError:
            raise ImportError(
                "modelseedpy and pandas are required to deserialize MSExpression objects. "
                "Install with: pip install modelseedpy pandas pyarrow"
            )
        df = pd.read_parquet(path, engine="pyarrow")
        meta_path = path.with_suffix(".meta.json")
        meta = {}
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        expr = MSExpression.__new__(MSExpression)
        expr._data = df
        for k, v in meta.items():
            setattr(expr, k, v)
        return expr


register_serializer(MSExpressionSerializer())
