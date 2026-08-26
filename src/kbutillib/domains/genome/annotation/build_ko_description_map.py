#!/usr/bin/env python3
"""Build a plain KO -> human-readable description dictionary (``ko_description_map.tsv``).

This is a *separate* artifact from ``ko_function_map.tsv`` (built by the
sibling ``build_ko_function_map.py``). ``ko_function_map.tsv`` holds a
composed bridge string (``SYMBOL.lower() + "; " + norm(definition)``) whose
exact text joins ``mapping_KOFAMSCAN.tsv`` downstream -- it must never be
regenerated in a different shape. This script instead emits a plain
``ko_id -> description`` two-column table for :class:`OntologyDictionary`
(sibling module ``ontology_dictionary.py``) to serve from
``OntologyDictionary.describe("KO", ko_id)``.

DO NOT COMMIT THE OUTPUT
-------------------------
Like ``ko_function_map.tsv``, this table is derived from a bulk KEGG ``ko``
flat-file release. KEGG's redistribution terms for that content are more
restrictive than the licences of any tool in this repo, so the generated
table must never be committed -- it is built once per deployment, on the
compute host, from a KEGG release the host already holds, and lives outside
git under the deployment's data root. This script is the committed
artifact; its output (``ko_description_map*.tsv``) is gitignored for
exactly this reason -- a future contributor should not "helpfully" commit a
run of this script.

The release-schema trap, reused rather than reimplemented
-----------------------------------------------------------
The KEGG ``ko`` flat-file schema is release-dependent (see
``build_ko_function_map.py`` module docstring for the full derivation):
release 109.1 stores the gene symbol in ``SYMBOL`` and the description in
``NAME``; release 90.1 stores the symbol in ``NAME`` and the description in
``DEFINITION`` (with CRLF line endings). This script reuses
``build_ko_function_map._iter_ko_entries`` and
``build_ko_function_map._detect_layout_and_build_symbols`` -- the same
release-layout detection already implemented and guarded there -- rather
than writing a second parser, and picks the *description* field
(``NAME`` vs ``DEFINITION``) off the same layout decision.

Usage
-----
    python3 -m kbutillib.domains.genome.annotation.build_ko_description_map \\
        --ko-file /path/to/kegg/ko \\
        --kegg-release 109.1 \\
        --output /path/to/deployment/data/ko_description_map.tsv
"""

from __future__ import annotations

import argparse
import datetime
import logging
import sys
from pathlib import Path

from .build_ko_function_map import (
    KoEntry,
    _detect_layout_and_build_symbols,
    _iter_ko_entries,
)

logger = logging.getLogger(__name__)


def build_description_map(ko_file: Path) -> tuple[str, dict[str, str]]:
    """Parse a KEGG ``ko`` flat file into ``(detected_layout, {ko_id: description})``.

    Reuses :func:`build_ko_function_map._iter_ko_entries` and
    :func:`build_ko_function_map._detect_layout_and_build_symbols` for
    parsing and release-layout detection, then reads the description off
    whichever field the detected layout says carries it:

    - ``SYMBOL``-field layout (KEGG >=109.1-style): description is the
      ``NAME`` field.
    - ``NAME``-field layout (KEGG <=90.1-style): description is the
      ``DEFINITION`` field.

    Raises:
        RuntimeError: If the flat file yields zero parsed ``ENTRY``
            records, or if the detected layout yields zero non-empty
            descriptions across the whole release -- the release-schema
            trap (see module docstring): a parser that reads the wrong
            field for a given release parses cleanly and matches nothing,
            so an all-empty result is treated as a failure, named with the
            detected layout, rather than returned as an empty dictionary.
    """
    entries: list[KoEntry] = list(_iter_ko_entries(ko_file))
    if not entries:
        raise RuntimeError(
            f"KEGG ko flat file at {ko_file} yielded zero parsed ENTRY records. "
            "Confirm this is a KEGG `ko` release flat file (not `ko_list`, "
            "not gzipped, not truncated)."
        )

    layout, _symbols = _detect_layout_and_build_symbols(entries)
    if layout.startswith("SYMBOL-field"):
        descriptions = {e.ko_id: e.name_field for e in entries if e.name_field}
    else:
        descriptions = {
            e.ko_id: e.definition_field for e in entries if e.definition_field
        }

    if not descriptions:
        raise RuntimeError(
            f"KEGG ko release at {ko_file} produced zero non-empty descriptions "
            f"after detecting layout={layout!r} across {len(entries)} parsed "
            "entries. This is the release-schema trap: KEGG 109.1 stores the "
            "description in NAME (symbol in SYMBOL); release 90.1 stores the "
            "description in DEFINITION (symbol in NAME), with CRLF line "
            "endings. A parser assuming the wrong layout parses cleanly and "
            f"matches nothing. Inspect the raw file at {ko_file} to confirm "
            "which field actually carries the description for this release."
        )

    return layout, descriptions


def write_table(
    output_path: Path,
    descriptions: dict[str, str],
    *,
    kegg_release: str,
    layout: str,
    ko_file: Path,
) -> None:
    """Write the two-column ``ko_id\\tdescription`` table with a provenance header."""
    generated = datetime.datetime.now(tz=datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fh:
        fh.write(
            "# ko_description_map.tsv -- generated by "
            "kbutillib.domains.genome.annotation.build_ko_description_map\n"
        )
        fh.write(f"# kegg_ko_release: {kegg_release}\n")
        fh.write(f"# detected_ko_layout: {layout}\n")
        fh.write(f"# source_ko_file: {ko_file}\n")
        fh.write(f"# generated_utc: {generated}\n")
        fh.write(f"# row_count: {len(descriptions)}\n")
        fh.write("# KEGG-derived content -- do not commit this file.\n")
        fh.write("# ko_id\tdescription\n")
        for ko_id in sorted(descriptions):
            fh.write(f"{ko_id}\t{descriptions[ko_id]}\n")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build ko_description_map.tsv, a plain KO -> description table, "
            "from a KEGG ko flat-file release, for OntologyDictionary. Run "
            "this once per deployment on the compute host; never commit its "
            "output (see module docstring)."
        )
    )
    parser.add_argument(
        "--ko-file",
        type=Path,
        required=True,
        help="Path to a KEGG `ko` flat-file release (uncompressed).",
    )
    parser.add_argument(
        "--kegg-release",
        required=True,
        help=(
            "KEGG ko release identifier to record in the output header "
            "(e.g. '109.1'). Not reliably auto-detectable from file "
            "content, so it must be supplied explicitly."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output path for ko_description_map.tsv (outside git; deployment data root).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    layout, descriptions = build_description_map(args.ko_file)
    logger.info(
        "Detected KEGG ko layout=%r; parsed %d ko_description_map rows.",
        layout,
        len(descriptions),
    )

    write_table(
        args.output,
        descriptions,
        kegg_release=args.kegg_release,
        layout=layout,
        ko_file=args.ko_file,
    )
    logger.info("Wrote %s (%d rows).", args.output, len(descriptions))
    return 0


if __name__ == "__main__":
    sys.exit(main())
