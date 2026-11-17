"""Tuning material for the GO molecular-function councils.

What goes in here is the WRITTEN STANDARD a council is asked to apply, as
opposed to the prompts that ask it (``..queries.builtin``) or the engine that
runs it (``..engine``).  Today that is one thing, :data:`.policy.MAPPING_POLICY`
-- the six mapping rules ``go_mf_adjudicate@1`` judges two rival ``go_mf``
mappings against.

Importing this package pulls in nothing but :mod:`.policy`, which is a single
string constant: no store, no backend, no optional dependency.
"""

from __future__ import annotations

from .policy import MAPPING_POLICY

__all__ = ["MAPPING_POLICY"]
