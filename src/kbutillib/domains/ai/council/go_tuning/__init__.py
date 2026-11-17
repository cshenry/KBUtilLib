"""Tuning material for the GO molecular-function councils.

What goes in here is the WRITTEN STANDARD a council is asked to apply and the
MATERIAL it is asked to apply it to, as opposed to the prompts that ask it
(``..queries.builtin``) or the engine that runs it (``..engine``):

* :mod:`.policy` -- :data:`.policy.MAPPING_POLICY`, the six mapping rules
  ``go_mf_adjudicate@1`` judges two rival ``go_mf`` mappings against.
* :mod:`.conflicts` -- find and classify the places an ALCF bench council and
  the ``go_mf@4`` baseline disagree.
* :mod:`.sample` -- draw a reproducible tuning manifest from those conflicts.

Importing this package pulls in nothing but :mod:`.policy`, which is a single
string constant: no store, no backend, no optional dependency.  The other two
modules are NOT re-exported here for exactly that reason -- :mod:`.conflicts`
reaches a SQLAlchemy store and importing this package must stay free.  Import
them by name.
"""

from __future__ import annotations

from .policy import MAPPING_POLICY

__all__ = ["MAPPING_POLICY"]
