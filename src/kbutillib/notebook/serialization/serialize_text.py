"""Text serializer — stores strings as plain UTF-8 text files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import register_serializer


class TextSerializer:
    type_name = "text"
    file_extension = ".txt"

    def can_handle(self, obj: Any) -> bool:
        # Don't auto-dispatch to text — JSON serializer handles str.
        # Text serializer is used only via explicit type_hint="text".
        return False

    def serialize(self, obj: Any, path: Path) -> dict[str, Any]:
        data = str(obj).encode("utf-8")
        path.write_bytes(data)
        return {"length": len(data)}

    def deserialize(self, path: Path, metadata: dict) -> Any:
        return path.read_text(encoding="utf-8")


register_serializer(TextSerializer())
