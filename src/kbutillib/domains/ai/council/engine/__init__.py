"""The engine: the decision rule, and the worker that drives the queue.

Two modules, split along the line between deciding and doing:

* :mod:`.decide` -- ``decide()``, the pure adjudication rule.  No I/O, no
  backend, no store, no clock it does not take as an argument.
* :mod:`.worker` -- ``CouncilWorker``, which submits jobs, claims call rows,
  runs them through the submitting user's own backends, and advances each
  item proposers -> critic -> meta -> verdict by asking ``decide()`` what the
  item needs next.

Importing this package pulls in neither SQLAlchemy nor a backend: the worker
talks to the ``CouncilStore`` and ``Backend`` protocols only, and the query
registry is imported inside the one method that needs it.
"""

from __future__ import annotations

from .decide import APPROVING_VERDICTS, SettledCall, critic_approves, decide, tally
from .worker import CouncilWorker, aevaluate, evaluate

__all__ = [
    "decide",
    "critic_approves",
    "SettledCall",
    "APPROVING_VERDICTS",
    "tally",
    "CouncilWorker",
    "evaluate",
    "aevaluate",
]
