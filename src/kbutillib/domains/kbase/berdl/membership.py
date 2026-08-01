"""BERDL tenant membership decoding.

``get_my_groups()`` (and the governance ``list_available_groups()`` it must
be read against) returns a **flat** list of tenant/group names. Read-only
membership is encoded as a **name suffix**, not a structured field:
``"enigmaro"`` means read-only membership on the tenant ``"enigma"``.

The safe decode, per
``agent-io/prds/berdl-lakehouse-skills/fullprompt.md`` ("Membership
decoding"), is: strip a trailing ``"ro"`` and test whether the *remainder*
is a real, known tenant name (i.e. it appears in ``list_available_groups()``
or an equivalent available-groups fixture). A naive ``str.endswith("ro")``
check is wrong and must not be used on its own: it would misclassify any
tenant whose real, full name happens to end in those two letters (e.g. a
hypothetical tenant literally named ``"cairo"``) as read-only membership on
a truncated, non-existent tenant.
"""

from __future__ import annotations

from typing import Iterable, Literal

#: The suffix the platform appends to a group name to denote read-only
#: membership on that group.
_RO_SUFFIX = "ro"

PermissionLevel = Literal["rw", "ro"]


def decode_memberships(
    my_groups: Iterable[str],
    available_groups: Iterable[str],
) -> dict[str, PermissionLevel]:
    """Decode a flat ``get_my_groups()`` list into ``{tenant: 'rw' | 'ro'}``.

    For each raw group name: if it ends in ``"ro"`` *and* stripping that
    suffix yields a name present in ``available_groups``, the suffix is a
    genuine read-only marker and the entry is recorded against the stripped
    tenant name as ``"ro"``. Otherwise the raw name is a real, read-write
    tenant name as-is (whether or not it happens to end in the letters
    ``"ro"``) and is recorded verbatim as ``"rw"``.

    Args:
        my_groups: The flat, possibly ``ro``-suffixed group list as
            returned by ``get_my_groups()``.
        available_groups: The full set of real tenant/group names, as
            returned by ``list_available_groups()``, used to validate
            whether a stripped ``ro`` suffix is genuine.

    Returns:
        A mapping of tenant name to permission level, ``"rw"`` or ``"ro"``.
    """
    available = set(available_groups)
    memberships: dict[str, PermissionLevel] = {}

    for raw in my_groups:
        if raw.endswith(_RO_SUFFIX):
            stem = raw[: -len(_RO_SUFFIX)]
            if stem in available:
                memberships[stem] = "ro"
                continue
        # Either it doesn't end in "ro" at all, or stripping the suffix
        # doesn't land on a real tenant -- the trailing letters are part of
        # the genuine tenant name, and membership is read-write.
        memberships[raw] = "rw"

    return memberships
