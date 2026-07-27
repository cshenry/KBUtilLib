"""JSON serializer for plain JSON-serializable objects (lists, scalars, etc.)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import register_serializer


class JsonSerializer:
    type_name = "json"
    file_extension = ".json"

    def can_handle(self, obj: Any) -> bool:
        # Handle lists, strings, numbers, booleans, None — but NOT dicts
        # (dicts go to the dict serializer for higher priority).
        return isinstance(obj, (list, int, float, bool, type(None), str))

    def serialize(self, obj: Any, path: Path) -> dict[str, Any]:
        data = json.dumps(obj, indent=2, sort_keys=True).encode("utf-8")
        path.write_bytes(data)
        return {}

    def deserialize(self, path: Path, metadata: dict) -> Any:
        return json.loads(path.read_bytes())


register_serializer(JsonSerializer())
