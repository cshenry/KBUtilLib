"""Domain schema and registration namespace for KBUtilLib.

Sub-packages under ``kbutillib.domains`` hold pydantic v2 input/output
models for each capability domain (e.g. ``biochem``, ``thermo``).  They are
imported lazily at decoration time so no heavy deps are pulled in at
module-level.
"""

# Intentionally lightweight — no heavy imports here.
