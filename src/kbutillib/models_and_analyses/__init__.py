"""The Models and Analyses web app.

A FastAPI application that renders the KOROS model-analysis surface: every
project and arc under the runs tree, the model portfolio per arc, and the
per-model analysis history. It is one of the two KIND apps built on the shared
:mod:`kbutillib.koros_arc_store` scaffold; the annotation explorer is the other.

The module is split so the DATA LAYER carries no web framework and no
filesystem knowledge of its own:

  * :mod:`~kbutillib.models_and_analyses.prefixes` — the exact set of analysis
    ``kind`` prefixes this app owns, and the classification of a record's kind
    into a portfolio bucket (or ``other`` for a foreign kind).
  * :mod:`~kbutillib.models_and_analyses.metrics` — the arc-level
    model-versus-experiment confusion matrix (TP/FP/TN/FN, MCC, precision,
    recall), reading the fitness class vocabulary from
    :mod:`kbutillib.domains.notebook.fitness_dashboard` rather than restating it.
  * :mod:`~kbutillib.models_and_analyses.service` — the pure data layer:
    :func:`build_portfolio` and :func:`build_arc_models` take a
    :class:`~kbutillib.koros_arc_store.store.KorosArcStore` (or the shipped fake)
    and return the endpoint JSON. It reaches its data through the store and
    NOTHING else — it imports no ``pathlib`` to walk the runs tree.
  * :mod:`~kbutillib.models_and_analyses.app` — the FastAPI wiring. ``fastapi``
    and ``uvicorn`` are imported LAZILY inside functions, exactly as
    :mod:`kbutillib.interfaces.api.app` does, so importing this package never
    requires them and the data layer is testable without a server.

CAC identity comes from :mod:`kbutillib.arc_context` (``APP_ID`` / ``app_us``)
— never hardcoded here (CAC invariant I4).
"""

from __future__ import annotations

from .prefixes import (
    OWNED_KIND_PREFIXES,
    PORTFOLIO_BUCKETS,
    classify_kind,
)
from .service import (
    CONTRACT_VERSION,
    build_arc_models,
    build_portfolio,
    check_startup_contract_version,
    resolve_app_state_dir,
)

__all__ = [
    "OWNED_KIND_PREFIXES",
    "PORTFOLIO_BUCKETS",
    "classify_kind",
    "build_portfolio",
    "build_arc_models",
    "resolve_app_state_dir",
    "check_startup_contract_version",
    "CONTRACT_VERSION",
]
