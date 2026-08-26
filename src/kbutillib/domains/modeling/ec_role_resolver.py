"""EC-number -> ModelSEED role name resolution via ``Annotations/Roles.tsv``.

Why this exists
----------------
EC accessions extracted from genome annotation (KOfamScan, BAKTA, ...) need a
path to reactions. Rather than inventing a fuzzy text join against an external
enzyme-description vocabulary (e.g. ExPASy), this module reuses ModelSEED's
own curated role vocabulary: a large fraction of ModelSEED roles embed an EC
number directly in the role name, e.g. ``"Arylsulfatase B precursor (EC
3.1.6.12)"``. Those roles already chain through ``Complexes.tsv`` to reactions
via the machinery downstream model reconstruction consumes, so emitting
role-formatted strings reaches reactions through curated biology instead of
string similarity.

This module does **not** perform any fuzzy or substring matching. The index is
built by parsing the literal ``"(EC x.x.x.x)"`` suffix out of each role name
and keying on the parsed string verbatim. A query for a wildcard EC such as
``"1.1.1.-"`` therefore only ever matches roles whose name carries that exact
wildcard form -- it can never match ``"1.1.1.1"`` or any other concrete EC
sharing the prefix, because no prefix expansion happens anywhere in this
module.

Configuration
-------------
The caller supplies the path to ModelSEED's ``Annotations/Roles.tsv`` (or a
directory containing it) via the constructor. The file is read read-only;
nothing here writes to it. Loading is lazy (deferred to first call) and the
parsed index is cached for the lifetime of the instance.
"""

from __future__ import annotations

import csv
import os
import re
from typing import Dict, List, Optional

__all__ = ["EcRoleResolver"]

#: Matches the ModelSEED role-name EC suffix, e.g. "(EC 3.1.6.12)" or
#: "(EC 1.1.1.-)". The first digit is restricted to the seven official EC
#: classes; the remaining three fields are each either a run of digits or a
#: literal "-" wildcard. Anchored to the "(EC ...)" parenthetical form
#: specifically so an unrelated trailing parenthetical -- e.g. "(putative)" or
#: "(NAD(P)+)" -- is never mistaken for an EC number.
_EC_IN_ROLE_RE = re.compile(r"\(EC\s+([1-7](?:\.(?:\d+|-)){3})\)")

#: Matches a bare EC number a caller passes to ``roles_for_ec``, with an
#: optional "EC" prefix (e.g. "EC 3.1.6.12", "ec3.1.6.12", "3.1.6.12").
_BARE_EC_RE = re.compile(r"^(?:EC\s*)?([1-7](?:\.(?:\d+|-)){3})$", re.IGNORECASE)


def _normalize_query_ec(ec: str) -> Optional[str]:
    """Strip an optional "EC" prefix/whitespace from a caller-supplied EC string.

    Returns the bare ``x.x.x.x`` string (wildcards preserved verbatim), or
    ``None`` if ``ec`` is not a well-formed EC number. A malformed query
    simply cannot match anything in the index, so callers get ``[]`` rather
    than an exception.
    """
    match = _BARE_EC_RE.match(ec.strip())
    return match.group(1) if match else None


class EcRoleResolver:
    """Resolves EC numbers to ModelSEED role names via ``Annotations/Roles.tsv``.

    Args:
        roles_tsv_path: Path to ModelSEED's ``Annotations/Roles.tsv`` file
            itself (tab-separated, columns ``id, name, source, features,
            aliases``). Read-only; never written to.

    Example::

        resolver = EcRoleResolver("/path/to/ModelSEEDDatabase/Annotations/Roles.tsv")
        resolver.roles_for_ec("3.1.6.12")
        # ["Arylsulfatase B precursor (EC 3.1.6.12)"]
    """

    def __init__(self, roles_tsv_path: str) -> None:
        self._roles_tsv_path = roles_tsv_path
        self._index: Optional[Dict[str, List[str]]] = None

    # -- lazy, cached index ---------------------------------------------

    @property
    def _ec_index(self) -> Dict[str, List[str]]:
        if self._index is None:
            self._index = self._build_index()
        return self._index

    def _build_index(self) -> Dict[str, List[str]]:
        """Parse ``Roles.tsv`` and build the EC -> [role name, ...] index.

        A role name may embed more than one EC (e.g. a bifunctional enzyme's
        name carrying two "(EC ...)" parentheticals) -- each embedded EC gets
        an entry for that role. Order within each EC's list follows the order
        roles appear in the source TSV, which is deterministic for a fixed
        input file.
        """
        index: Dict[str, List[str]] = {}
        with open(self._roles_tsv_path, newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            for row in reader:
                name = (row.get("name") or "").strip()
                if not name:
                    continue
                # A single role name can embed the same EC twice only by
                # coincidence of formatting; dedupe within a row so one role
                # is never listed twice under the same EC.
                seen_in_row = set()
                for ec_match in _EC_IN_ROLE_RE.finditer(name):
                    ec = ec_match.group(1)
                    if ec in seen_in_row:
                        continue
                    seen_in_row.add(ec)
                    index.setdefault(ec, []).append(name)
        return index

    # -- public API --------------------------------------------------------

    def roles_for_ec(self, ec: str) -> List[str]:
        """Return all ModelSEED role names whose name embeds ``ec``.

        Args:
            ec: An EC number, e.g. ``"3.1.6.12"`` or a wildcard form such as
                ``"1.1.1.-"``. An optional ``"EC "`` prefix is tolerated.
                Wildcard fields are matched literally -- ``"1.1.1.-"`` matches
                only roles carrying that exact wildcard string, never
                concrete ECs sharing the ``1.1.1`` prefix.

        Returns:
            All matching role names, in TSV order. An empty list if ``ec``
            does not match any role (including a malformed ``ec`` string) --
            this method never raises for an unmatched EC.
        """
        normalized = _normalize_query_ec(ec)
        if normalized is None:
            return []
        return list(self._ec_index.get(normalized, []))
