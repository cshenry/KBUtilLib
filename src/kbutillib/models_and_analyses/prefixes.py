"""The analysis kinds this app owns, and how a record's kind is bucketed.

This app renders MODEL analyses. The shared run database holds records from BOTH
KIND apps in one place, so every arc read returns records this app cannot
interpret — the annotation explorer's ``kbdl.annotation`` and ``kbdl.skani``
records live in the same per-arc index. Counting those as models would be a
defect that looks like data corruption.

The rule (binding, confront round 1): the prefix filter COUNTS what it excludes
and exposes the count as a single ``other`` bucket. A dropped foreign record
makes a filter bug look like missing data; a counted one is debuggable. A
missing prefix filter is a test failure, not a display quirk.

There is deliberately no fuzzy matching here. A kind is owned iff it is exactly
one of :data:`OWNED_KIND_PREFIXES`; anything else — including a well-formed kind
from a peer app and a malformed kind — is ``other``.
"""

from __future__ import annotations

from typing import Dict

# The exact analysis kinds this app owns. Ordered map: kind -> portfolio bucket.
# The bucket names are the count fields the portfolio exposes per arc. Every
# kind NOT in this map is counted under the single ``other`` bucket.
#
# Spec (task body, "FILTER BY PREFIX"): this app owns exactly these seven kinds.
# kbdl.annotation and kbdl.skani (the annotation explorer) are deliberately
# absent — they are foreign and must land in ``other``.
_OWNED_KIND_TO_BUCKET: Dict[str, str] = {
    "kbdl.model_build": "models_built",
    "kbutillib.reconstruct": "models_built",
    "kbutillib.gapfill": "gapfills",
    "kbutillib.fba": "fba_runs",
    "kbutillib.fva": "fva_runs",
    "kbdl.fitness_analysis": "fitness_analyses",
    "kbdl.fitness_prop": "fitness_analyses",
}

# The set of kinds this app owns, for a membership test and for import by tests.
OWNED_KIND_PREFIXES = frozenset(_OWNED_KIND_TO_BUCKET)

# The count buckets a portfolio arc summary carries, in a stable order. ``other``
# is always last and always present: it is the counted-exclusion bucket, never
# omitted, so a foreign record is visible rather than silently dropped.
PORTFOLIO_BUCKETS = (
    "models_built",
    "gapfills",
    "fba_runs",
    "fva_runs",
    "fitness_analyses",
    "other",
)

# The bucket a foreign / unowned kind is counted under.
OTHER_BUCKET = "other"

# Which owned kinds are model-build kinds — the ones whose records introduce a
# model into an arc. Used by the arc model endpoint to decide which records are
# a model's "build" record versus an analysis run against it.
MODEL_BUILD_KINDS = frozenset({"kbdl.model_build", "kbutillib.reconstruct"})


def classify_kind(kind: str) -> str:
    """Return the portfolio bucket for *kind*.

    An owned kind maps to its bucket (``models_built``, ``gapfills``,
    ``fba_runs``, ``fva_runs`` or ``fitness_analyses``). Every other kind —
    a foreign but well-formed kind from a peer app, or a malformed kind — maps
    to :data:`OTHER_BUCKET` (``other``). Never raises: an unrecognised kind is
    counted, not refused (this is the counted-exclusion rule).
    """
    return _OWNED_KIND_TO_BUCKET.get(kind, OTHER_BUCKET)


def is_owned(kind: str) -> bool:
    """True iff *kind* is one of the kinds this app owns."""
    return kind in OWNED_KIND_PREFIXES
