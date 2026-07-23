"""MSMedia serializer — round-trip via to_dict("complete") + envelope for id/name.

modelseedpy.core.msmedia.MSMedia.from_dict() drops the media's id and name
(it always returns ``MSMedia("media")``), but downstream callers depend on
the id/name surviving the cache round-trip — KBaseMediaPkg.build_package
keys its open-all-uptakes behaviour off ``media.name == "Complete"``.

We wrap the compound payload in an envelope that also stores id, name, and
media_ref, and rehydrate by constructing the MSMedia and restoring those
fields.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import register_serializer


class MSMediaSerializer:
    type_name = "msmedia"
    file_extension = ".json"

    def can_handle(self, obj: Any) -> bool:
        return (
            type(obj).__name__ == "MSMedia"
            and hasattr(obj, "to_dict")
            and hasattr(obj, "mediacompounds")
        )

    def serialize(self, obj: Any, path: Path) -> dict[str, Any]:
        envelope = {
            "id": getattr(obj, "id", None),
            "name": getattr(obj, "name", None),
            "media_ref": getattr(obj, "media_ref", None),
            "compounds": obj.to_dict("complete"),
        }
        path.write_text(json.dumps(envelope), encoding="utf-8")
        return {}

    def deserialize(self, path: Path, metadata: dict) -> Any:
        try:
            from modelseedpy.core.msmedia import MSMedia
        except ImportError as exc:
            raise ImportError(
                "modelseedpy is required to deserialize MSMedia objects. "
                "Install with: pip install modelseedpy"
            ) from exc

        envelope = json.loads(path.read_text(encoding="utf-8"))
        media = MSMedia.from_dict(envelope["compounds"])
        if envelope.get("id") is not None:
            media.id = envelope["id"]
        if envelope.get("name") is not None:
            media.name = envelope["name"]
        if envelope.get("media_ref") is not None:
            media.media_ref = envelope["media_ref"]
        return media


register_serializer(MSMediaSerializer())
