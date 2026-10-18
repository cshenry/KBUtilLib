"""Fixture wiring for the council tests.

The store contract's fixtures live in :mod:`.store_contract` so that a second
test module can run the same contract against the same implementations.  They
are re-exported here rather than imported by each test module because a test
module that imports a fixture then shadows that name with a parameter in every
test that uses it -- a redefinition the linter objects to once per test.  A
conftest has no test functions, so there is nothing to shadow.
"""

from __future__ import annotations

from .store_contract import (  # noqa: F401
    make_store,
    store,
    store_backend,
    store_url,
)
