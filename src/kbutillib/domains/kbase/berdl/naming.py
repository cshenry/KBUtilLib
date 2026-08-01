"""BERDL database-name normalization and personal-catalog alias translation.

The BERDL platform is in a dual-read window: ``get_databases()`` (and
equivalents) return **both** Iceberg-style dotted names (``aiale.dataset1``)
and legacy Delta-style underscored names (``aiale_dataset1``) for the *same*
logical dataset. Name alone does not disambiguate which is which, and naively
listing both makes it look like there are twice as many datasets as there
really are.

Rules (see ``agent-io/prds/berdl-lakehouse-skills/fullprompt.md``, "Name
normalization"):

- Prefer the dotted (Iceberg) form as the canonical, displayed name.
- When both forms exist for the same logical dataset, deduplicate them into a
  single entry and record the underscored form as a *legacy alias* on that
  entry — never drop it silently and never present the two as unrelated
  datasets.
- The personal-catalog alias is engine-specific: Spark uses the literal
  string ``"my"``; Trino requires the caller's own username. Translation
  between the two happens here, not at call sites.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

#: The Spark-side literal alias for a user's personal catalog.
SPARK_PERSONAL_ALIAS = "my"


@dataclass(frozen=True)
class NormalizedDatabase:
    """A single normalized, deduplicated BERDL database entry.

    Attributes:
        name: The preferred display name for this logical dataset. Always
            the dotted (Iceberg) form when one was seen for this dataset,
            otherwise the underscored (Delta) form that was seen.
        is_iceberg: ``True`` when ``name`` is the dotted/Iceberg form,
            ``False`` when the only known form is the legacy underscored
            one.
        legacy_alias: The underscored (Delta) counterpart name, if the
            platform returned one for this same logical dataset alongside
            the dotted form. ``None`` when no underscored counterpart was
            seen. This is how the underscored form is *marked* as legacy
            rather than hidden: it stays reachable from the deduplicated
            entry instead of being silently dropped or shown as a second,
            unrelated dataset.
    """

    name: str
    is_iceberg: bool
    legacy_alias: str | None = None


def _dotted_to_underscored(name: str) -> str:
    """Convert a dotted database name to its underscored counterpart.

    Only the first ``.`` is replaced, since the tenant/catalog prefix itself
    is not expected to contain a dot (``aiale.dataset1`` ->
    ``aiale_dataset1``; ``kbaseincubator.fitness`` ->
    ``kbaseincubator_fitness``). Names with no ``.`` are returned unchanged.
    """
    if "." not in name:
        return name
    prefix, rest = name.split(".", 1)
    return f"{prefix}_{rest}"


def normalize_databases(names: Iterable[str]) -> list[NormalizedDatabase]:
    """Pair, dedupe, and legacy-mark a raw BERDL database name list.

    Groups dotted and underscored names that refer to the same logical
    dataset (by comparing each dotted name's underscored form against the
    underscored names present), prefers the dotted form as the canonical
    display name, and folds the underscored counterpart into that single
    entry as ``legacy_alias`` rather than emitting it as a second, seemingly
    unrelated entry.

    Args:
        names: Raw database names as returned by the platform (a mix of
            dotted and underscored forms; order and duplicates are
            tolerated).

    Returns:
        One :class:`NormalizedDatabase` per distinct logical dataset, in
        first-seen order.
    """
    # First pass: bucket every raw name under its underscored "grouping key"
    # so a dotted name and its underscored counterpart land in the same
    # bucket regardless of which form was seen first.
    order: list[str] = []
    dotted_by_key: dict[str, str] = {}
    underscored_by_key: dict[str, str] = {}

    for raw in names:
        key = _dotted_to_underscored(raw)
        if key not in dotted_by_key and key not in underscored_by_key:
            order.append(key)
        if "." in raw:
            dotted_by_key.setdefault(key, raw)
        else:
            underscored_by_key.setdefault(key, raw)

    results: list[NormalizedDatabase] = []
    for key in order:
        dotted = dotted_by_key.get(key)
        underscored = underscored_by_key.get(key)
        if dotted is not None:
            results.append(
                NormalizedDatabase(
                    name=dotted, is_iceberg=True, legacy_alias=underscored
                )
            )
        else:
            # Only the legacy underscored form was ever seen for this
            # dataset -- still surfaced, just marked as non-Iceberg.
            assert underscored is not None
            results.append(
                NormalizedDatabase(
                    name=underscored, is_iceberg=False, legacy_alias=None
                )
            )
    return results


def to_trino_alias(name: str, username: str) -> str:
    """Translate a Spark-style personal-catalog reference to its Trino form.

    Spark's personal catalog alias is the literal string ``"my"``; Trino has
    no such alias and requires the caller's own username instead
    (``get_trino_connection`` style access). Only the alias segment (the
    catalog itself, or the leading ``my.`` prefix of a qualified name) is
    translated; names that do not reference the personal catalog are
    returned unchanged.

    Args:
        name: A Spark-style name or qualified name, e.g. ``"my"`` or
            ``"my.dataset1"``.
        username: The caller's BERDL/Trino username.

    Returns:
        The Trino-equivalent name, e.g. ``"{username}"`` or
        ``"{username}.dataset1"``.
    """
    if name == SPARK_PERSONAL_ALIAS:
        return username
    prefix = f"{SPARK_PERSONAL_ALIAS}."
    if name.startswith(prefix):
        return f"{username}.{name[len(prefix) :]}"
    return name


def to_spark_alias(name: str, username: str) -> str:
    """Translate a Trino-style personal-catalog reference to its Spark form.

    Inverse of :func:`to_trino_alias`: Trino references the personal catalog
    by the caller's own username; Spark uses the literal alias ``"my"``.
    Names that do not reference ``username`` (as the catalog itself, or as
    the leading segment of a qualified name) are returned unchanged.

    Args:
        name: A Trino-style name or qualified name, e.g. ``"{username}"`` or
            ``"{username}.dataset1"``.
        username: The caller's BERDL/Trino username.

    Returns:
        The Spark-equivalent name, e.g. ``"my"`` or ``"my.dataset1"``.
    """
    if name == username:
        return SPARK_PERSONAL_ALIAS
    prefix = f"{username}."
    if name.startswith(prefix):
        return f"{SPARK_PERSONAL_ALIAS}.{name[len(prefix) :]}"
    return name
