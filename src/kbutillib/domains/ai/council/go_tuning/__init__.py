"""Tuning material for the GO molecular-function councils.

What goes in here is the WRITTEN STANDARD a council is asked to apply and the
MATERIAL it is asked to apply it to, as opposed to the prompts that ask it
(``..queries.builtin``) or the engine that runs it (``..engine``):

* :mod:`.policy` -- :data:`.policy.MAPPING_POLICY`, the six mapping rules
  ``go_mf_adjudicate@1`` judges two rival ``go_mf`` mappings against.
* :mod:`.conflicts` -- find and classify the places an ALCF bench council and
  the ``go_mf@4`` baseline disagree.
* :mod:`.sample` -- draw a reproducible tuning manifest from those conflicts.
* :mod:`.adjudicate` -- build the blinded ``go_mf_adjudicate@1`` items a
  manifest calls for, and run them as resumable tranches under a token
  ceiling and a spot-check gate.
* :mod:`.ledger` -- the run's append-only record, which is what a restart
  reads to find out what the previous invocation had already set in motion.
* :mod:`.table` -- the pilot table a human marks (``.xlsx`` plus an ``.md``
  copy) and the reader that turns those marks back into gold.
* :mod:`.calibrate` -- agreement, its Wilson interval, the class/source/side
  slices, the position-swap flip rates and the trust BAND the tranche gate
  reads.

Importing this package pulls in nothing but :mod:`.policy`, which is a single
string constant: no store, no backend, no optional dependency.  The other
modules are NOT re-exported here for exactly that reason -- :mod:`.conflicts`
reaches a SQLAlchemy store, :mod:`.adjudicate` reaches the council facade and
:mod:`.table` reaches ``openpyxl`` (the ``council-tuning`` extra), and
importing this package must stay free.  Import them by name.
"""

from __future__ import annotations

from .policy import MAPPING_POLICY

__all__ = ["MAPPING_POLICY"]
