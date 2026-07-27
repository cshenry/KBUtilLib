"""MSExpression serializer — DataFrame (_data) as parquet + metadata via return dict."""

from __future__ import annotations

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
        # Return metadata dict (stored in catalog metadata_json column)
        return {
            "genome_ref": getattr(obj, "genome_ref", None),
            "expression_set_ref": getattr(obj, "expression_set_ref", None),
            "condition": getattr(obj, "condition", None),
        }

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
        expr = MSExpression.__new__(MSExpression)
        expr._data = df
        # Restore attributes from metadata dict
        for k in ("genome_ref", "expression_set_ref", "condition"):
            if k in metadata:
                setattr(expr, k, metadata[k])
        return expr


register_serializer(MSExpressionSerializer())
