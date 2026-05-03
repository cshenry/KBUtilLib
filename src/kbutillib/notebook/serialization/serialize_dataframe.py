"""DataFrame serializer — stores pandas DataFrames as Parquet via pyarrow."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import register_serializer


class DataFrameSerializer:
    type_name = "dataframe"
    file_extension = ".parquet"

    def can_handle(self, obj: Any) -> bool:
        try:
            import pandas as pd
            return isinstance(obj, pd.DataFrame)
        except ImportError:
            return False

    def serialize(self, obj: Any, path: Path) -> dict[str, Any]:
        obj.to_parquet(path, engine="pyarrow")
        return {"shape": list(obj.shape), "columns": list(obj.columns)}

    def deserialize(self, path: Path, metadata: dict) -> Any:
        import pandas as pd
        return pd.read_parquet(path, engine="pyarrow")


register_serializer(DataFrameSerializer())
