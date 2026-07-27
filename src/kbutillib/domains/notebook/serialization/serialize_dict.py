"""Dict serializer — stores Python dicts as JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import register_serializer


class DictSerializer:
    type_name = "dict"
    file_extension = ".json"

    def can_handle(self, obj: Any) -> bool:
        return isinstance(obj, dict)

    def serialize(self, obj: Any, path: Path) -> dict[str, Any]:
        data = json.dumps(obj, indent=2, sort_keys=True).encode("utf-8")
        path.write_bytes(data)
        return {}

    def deserialize(self, path: Path, metadata: dict) -> Any:
        return json.loads(path.read_bytes())


register_serializer(DictSerializer())
