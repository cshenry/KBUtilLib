"""OntologyDictionary -- accession -> human-readable description lookups.

Serves ``describe(namespace, accession) -> str | None`` for four
controlled vocabularies used elsewhere in this package's annotators:
``KO`` (KEGG Orthology), ``EC`` (ExPASy enzyme.dat), ``GO`` (Gene
Ontology), and ``COG`` (NCBI Clusters of Orthologous Groups).

This module never parses a raw ontology release itself. It reads
**staged, pre-generated two-column TSVs** (``accession<TAB>description``,
one namespace per file) produced by this package's sibling generator
scripts (``build_ko_description_map.py``, ``build_ec_description_map.py``,
``build_go_description_map.py``, ``build_cog_description_map.py``), each
of which does the release parsing, release-layout detection (KO), and
zero-entry loud-failure guarding for its own source format. Splitting the
concerns this way keeps ``OntologyDictionary`` itself a small, dependency-free
runtime lookup -- no KEGG/OBO/ExPASy/NCBI parsing at annotation time,
mirroring ``KofamscanUtils._bridge_ko``'s "pure dict lookup over a
pre-built table" design (see ``kofamscan_utils.py`` module docstring).

Two independent failure modes, deliberately handled differently
--------------------------------------------------------------------
1. **Missing dictionary file** (unset config / constructor path, or a
   path that does not resolve to a readable file) is *not* an error:
   ``describe()`` returns ``None`` for every accession in that namespace,
   a warning is logged once (per namespace, at first lookup) naming the
   expected path, and the namespace is recorded in
   :attr:`OntologyDictionary.degraded_namespaces` so a caller can report
   the degradation -- the same shape as ``kofamscan_utils``'s
   ``ko_function_map: absent`` marker.
2. **A staged file that exists but parses to zero entries** is a hard
   failure: :meth:`OntologyDictionary.describe` (via the private loader)
   raises ``RuntimeError`` naming the namespace, the file path, and the
   detected layout. A silently empty dictionary is indistinguishable from
   "this accession genuinely has no description" and would corrupt every
   downstream ``describe()`` call for that namespace with false
   negatives; a missing *file*, by contrast, is a legible, reportable
   degradation with a well-defined path to fix (stage the file).

Loading is lazy (nothing is read from disk until the first
``describe()`` call for a given namespace) and the parsed table is
cached for the lifetime of the ``OntologyDictionary`` instance -- one
parse per namespace per process, not per call.

Configuration
-------------
Each namespace's staged TSV path may be supplied either as a constructor
keyword argument (``ko_path``, ``ec_path``, ``go_path``, ``cog_path`` --
useful for tests and one-off scripts) or via config keys read through
``SharedEnvUtils.get_config_value``:

- ``ontology_dictionary.ko_path``
- ``ontology_dictionary.ec_path``
- ``ontology_dictionary.go_path``
- ``ontology_dictionary.cog_path``

A constructor argument, when given, takes precedence over the matching
config key.

Note on ``ko_path``
--------------------
The staged KO table here is ``ko_description_map.tsv`` (built by
``build_ko_description_map.py``), a *plain* ``ko_id -> description``
table. It is a **different artifact** from ``ko_function_map.tsv``
(built by ``build_ko_function_map.py``), which holds a composed bridge
string (``SYMBOL.lower() + "; " + norm(definition)``) that
``mapping_KOFAMSCAN.tsv`` joins on downstream. ``OntologyDictionary``
never reads ``ko_function_map.tsv`` and never writes it; do not point
``ontology_dictionary.ko_path`` at it.

Example::

    od = OntologyDictionary(ko_path="/data/ko_description_map.tsv")
    od.describe("KO", "K00001")   # -> "alcohol dehydrogenase" (or similar)
    od.describe("KO", "K99999")   # -> None (accession absent from table)
    od.describe("EC", "1.1.1.1")  # -> None (ontology_dictionary.ec_path unset)
    "EC" in od.degraded_namespaces  # -> True
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kbutillib.core.shared_env_utils import SharedEnvUtils

#: The four supported controlled-vocabulary namespaces.
NAMESPACES: tuple[str, ...] = ("KO", "EC", "GO", "COG")

#: Config key read (via ``get_config_value``) for each namespace's staged
#: two-column dictionary path, when no matching constructor kwarg is given.
_CONFIG_KEYS: dict[str, str] = {
    "KO": "ontology_dictionary.ko_path",
    "EC": "ontology_dictionary.ec_path",
    "GO": "ontology_dictionary.go_path",
    "COG": "ontology_dictionary.cog_path",
}


def _parse_two_column_tsv(
    text: str, *, namespace: str, source_path: str
) -> dict[str, str]:
    """Parse a staged ``accession<TAB>description`` TSV into a dict.

    Blank lines and ``#``-prefixed lines (provenance/header comments,
    matching the header format the ``build_*_description_map.py``
    generators write) are skipped. A data line that does not split into
    exactly two non-empty tab-separated fields is silently skipped --
    matching ``kofamscan_utils._parse_ko_function_map_text``'s tolerance
    for the occasional malformed row -- but a *file* that yields zero
    usable entries is not tolerated (see below).

    Raises:
        RuntimeError: If zero ``{accession: description}`` entries parse
            from *text*, naming *namespace*, *source_path*, and the
            detected layout ("two-column TSV, accession<TAB>description").
            Never returns an empty dict: a silently empty dictionary
            yields descriptions that describe nothing and is
            indistinguishable from "this accession has no description".
    """
    table: dict[str, str] = {}
    data_lines_seen = 0
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        data_lines_seen += 1
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        accession, description = parts[0].strip(), parts[1].strip()
        if accession and description:
            table[accession] = description

    if not table:
        raise RuntimeError(
            f"{namespace} ontology dictionary at {source_path!r} parsed to "
            "zero entries using the expected two-column TSV layout "
            "(accession<TAB>description, '#'-prefixed header/provenance "
            f"lines skipped); saw {data_lines_seen} non-comment line(s). "
            "A silently empty dictionary is indistinguishable from every "
            "accession genuinely lacking a description, so this raises "
            "instead. Confirm this file was generated by this namespace's "
            "build_*_description_map.py script and was not truncated, "
            "corrupted, or overwritten with an unrelated file."
        )
    return table


class OntologyDictionary(SharedEnvUtils):
    """Accession -> human-readable description lookup over staged ontology dictionaries.

    Exactly one public method, :meth:`describe`. Source parsing, KEGG
    release-layout detection, loading, and caching are all handled by the
    sibling ``build_*_description_map.py`` generator scripts (at staging
    time) and this class's private loader (at first-use time) -- see the
    module docstring for the missing-file-vs-zero-entry distinction.
    """

    def __init__(
        self,
        ko_path: str | None = None,
        ec_path: str | None = None,
        go_path: str | None = None,
        cog_path: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize an OntologyDictionary.

        Args:
            ko_path: Path to the staged ``ko_description_map.tsv``
                (built by ``build_ko_description_map.py``). Overrides the
                ``ontology_dictionary.ko_path`` config key when given.
            ec_path: Path to the staged ``ec_description_map.tsv``
                (built by ``build_ec_description_map.py``). Overrides
                ``ontology_dictionary.ec_path`` when given.
            go_path: Path to the staged ``go_description_map.tsv``
                (built by ``build_go_description_map.py``). Overrides
                ``ontology_dictionary.go_path`` when given.
            cog_path: Path to the staged ``cog_description_map.tsv``
                (built by ``build_cog_description_map.py``). Overrides
                ``ontology_dictionary.cog_path`` when given.
            **kwargs: Forwarded to ``SharedEnvUtils.__init__``.
        """
        super().__init__(**kwargs)
        explicit: dict[str, str | None] = {
            "KO": ko_path,
            "EC": ec_path,
            "GO": go_path,
            "COG": cog_path,
        }
        self._paths: dict[str, str] = {}
        for namespace, config_key in _CONFIG_KEYS.items():
            override = explicit[namespace]
            if override is not None:
                self._paths[namespace] = override
            else:
                self._paths[namespace] = (
                    self.get_config_value(config_key, default="") or ""
                )

        #: Per-namespace cache: unset (namespace absent from dict) until
        #: first ``describe()`` call for that namespace; then either a
        #: parsed ``{accession: description}`` table or ``None`` (missing
        #: file). Populated lazily, read many times -- the "loading is
        #: lazy and cached per process" requirement.
        self._tables: dict[str, dict[str, str] | None] = {}

        #: Namespaces whose staged dictionary file was unset/unreadable at
        #: last load attempt -- the "missing dictionary file" degradation.
        #: Mirrors ``KofamscanUtils``'s ``ko_function_map: absent`` marker
        #: (see ``kofamscan_utils.py``) so a caller can record it the same
        #: way. Populated lazily, alongside ``_tables``.
        self.degraded_namespaces: set[str] = set()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def describe(self, namespace: str, accession: str) -> str | None:
        """Return a human-readable description for *accession*, or ``None``.

        Args:
            namespace: One of ``"KO"``, ``"EC"``, ``"GO"``, ``"COG"``.
            accession: The accession to look up within *namespace*
                (e.g. ``"K00001"``, ``"1.1.1.1"``, ``"GO:0000001"``,
                ``"COG0001"``).

        Returns:
            The description string, or ``None`` when *accession* is
            absent from the namespace's dictionary, or when the
            namespace's staged dictionary file itself is missing/unset
            (see :attr:`degraded_namespaces` to distinguish the latter).

        Raises:
            ValueError: If *namespace* is not one of the four supported
                namespaces.
            RuntimeError: If the namespace's staged dictionary file exists
                but parses to zero entries (see module docstring) -- a
                distinct failure mode from "file missing".
        """
        if namespace not in NAMESPACES:
            raise ValueError(
                f"Unknown ontology namespace {namespace!r}; expected one of "
                f"{NAMESPACES}."
            )
        table = self._table_for(namespace)
        if table is None:
            return None
        return table.get(accession)

    # ------------------------------------------------------------------
    # Private: lazy loading + caching
    # ------------------------------------------------------------------

    def _table_for(self, namespace: str) -> dict[str, str] | None:
        """Return the cached table for *namespace*, loading it on first use."""
        if namespace in self._tables:
            return self._tables[namespace]
        table = self._load(namespace)
        self._tables[namespace] = table
        return table

    def _load(self, namespace: str) -> dict[str, str] | None:
        """Load and parse the staged dictionary file for *namespace*.

        Returns ``None`` (and records the degradation) when the path is
        unset or unreadable. Raises ``RuntimeError`` (via
        :func:`_parse_two_column_tsv`) when the file is readable but
        parses to zero entries -- see module docstring.
        """
        path = self._paths.get(namespace, "")
        if not path:
            self.degraded_namespaces.add(namespace)
            self.log_warning(
                f"ontology_dictionary path for namespace {namespace!r} is "
                "unset (no constructor override and no "
                f"{_CONFIG_KEYS[namespace]!r} config key); "
                f"describe({namespace!r}, ...) will return None for every "
                "accession until this is staged."
            )
            return None
        try:
            text = Path(path).expanduser().read_text(encoding="utf-8")
        except OSError:
            self.degraded_namespaces.add(namespace)
            self.log_warning(
                f"{namespace} ontology dictionary file not found at "
                f"{path!r}; describe({namespace!r}, ...) will return None "
                "for every accession until this file is staged (see the "
                "matching build_*_description_map.py generator script)."
            )
            return None
        return _parse_two_column_tsv(text, namespace=namespace, source_path=path)
