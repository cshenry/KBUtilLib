"""The council's halt CAUSE vocabulary: ``HALT_CAUSES``, and which resume.

The canonical definitions live one layer down, in
:mod:`kbutillib.domains.ai.llm.halt_causes`, because a cause is decided where
the HTTP response is in hand and that layer is forbidden from importing the
council (see :mod:`..llm.types`).  This module is where the council -- and
PersistentAI's campaign watchdog, which gates any automatic resume of a
halted run on the code -- reads them from, so the vocabulary has one import
path at the level the halt record itself belongs to.

Two names and the eight codes, nothing else::

    from kbutillib.domains.ai.council.halt_causes import (
        HALT_CAUSES, RESUMABLE_HALT_CAUSES,
    )

    if state["halt_cause"] in RESUMABLE_HALT_CAUSES:
        ...  # may be resumed automatically

A halt whose ``halt_cause`` is ``None`` -- a budget halt, a supervisor-written
halt, an unrecognised gateway failure -- is NOT resumable.  Absence is a
deliberate value, not a gap: the classifier never guesses a resumable cause.
"""

from __future__ import annotations

from ...ai.llm.halt_causes import (
    ADMINISTRATIVELY_DISABLED,
    CREDENTIALS_REVOKED,
    HALT_CAUSES,
    INSUFFICIENT_SCOPE,
    NETWORK_ERROR,
    QUOTA_EXHAUSTED,
    RESUMABLE_HALT_CAUSES,
    TOKEN_EXPIRED,
    UNCLASSIFIED_AUTH_DENIAL,
    UPSTREAM_5XX,
    classify_http_halt_cause,
)

__all__ = [
    "ADMINISTRATIVELY_DISABLED",
    "CREDENTIALS_REVOKED",
    "HALT_CAUSES",
    "INSUFFICIENT_SCOPE",
    "NETWORK_ERROR",
    "QUOTA_EXHAUSTED",
    "RESUMABLE_HALT_CAUSES",
    "TOKEN_EXPIRED",
    "UNCLASSIFIED_AUTH_DENIAL",
    "UPSTREAM_5XX",
    "classify_http_halt_cause",
]
