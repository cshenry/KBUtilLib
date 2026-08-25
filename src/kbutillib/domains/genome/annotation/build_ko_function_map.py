#!/usr/bin/env python3
"""Build the KOFAMSCAN reaction-bridge table (``ko_function_map.tsv``).

Background
----------
KOFAMSCAN's primary product is a bare KO id, but a bare KO id joins
``mapping_KOFAMSCAN.tsv`` at 0% -- the join key there,
``f_fh == sha256(f_description)``, is computed over a
``"symbol; definition"``-shaped string that a KO id alone cannot
reproduce. This script builds the table that makes the join possible:

    f_fh  ==  sha256( SYMBOL.lower() + "; " + norm(definition) )

where ``definition`` comes from a KOfamScan ``ko_list`` file and
``SYMBOL`` comes from a KEGG ``ko`` flat-file release, and ``norm``
strips a trailing ``[EC:...]`` block, lowercases, and strips
whitespace. See PRD ``kbdl-local-bakta-kofamscan-v1`` D6/D6.1 for the
full derivation and the measured 97.0% coverage this composition
achieves (KBDLJobRunningPrototype
``agent-io/prds/kbdl-local-bakta-kofamscan-v1/fullprompt.md``).

DO NOT COMMIT THE OUTPUT
-------------------------
``ko_function_map.tsv`` is derived from bulk KEGG ``ko``/``ko_list``
content. KEGG's redistribution terms for that content are materially
more restrictive than the licences of any tool in this repo, so the
generated table must never be committed -- it is built **once per
deployment, on the compute host**, from a KEGG release the host
already holds, and lives outside git under the deployment's data root
(``kofamscan.ko_function_map`` config key). This script is the
committed artifact; its output is not. ``ko_function_map.tsv`` (and any
``ko_function_map*.tsv``) is listed in this repo's ``.gitignore`` for
exactly this reason -- a future contributor should not "helpfully"
commit a run of this script.

The release-schema trap (D6.2) -- why this script cannot be a naive parser
---------------------------------------------------------------------------
The KEGG ``ko`` flat-file schema is release-dependent, and a parser that
assumes the wrong layout parses *cleanly* and matches *nothing*:

- Release 109.1 stores the gene symbol in a ``SYMBOL`` field and the
  description in ``NAME``.
- Release 90.1 stores the symbol in ``NAME`` and the description in
  ``DEFINITION``, and uses CRLF line endings.

Only the symbol matters here (the definition always comes from
``ko_list``, never from the ``ko`` flat file), but a parser hard-coded
to look for the symbol in the wrong field would return an
all-empty-symbol table -- 0% coverage, silently. This script therefore
**detects** which field carries the symbol for the release it is
handed, and **fails loudly, naming the detected layout**, if the
detected field yields no non-empty symbols across the whole release.
A silently empty table produces a bridge that joins nothing.

Usage
-----
    python3 -m kbutillib.domains.genome.annotation.build_ko_function_map \\
        --ko-file /path/to/kegg/ko \\
        --ko-list /path/to/kofam/profiles/2025-11-03.txt \\
        --kegg-release 109.1 \\
        --ko-list-version 2025-11-03 \\
        --output /path/to/deployment/data/ko_function_map.tsv

Runtime consumption is a pure dict lookup (``_bridge_ko`` in
``KofamscanUtils``, sibling module) over the table this script emits --
no KEGG parsing, no network, at annotation time.
"""

from __future__ import annotations

import argparse
import datetime
import logging
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# KEGG `ko` flat-file parsing
# ---------------------------------------------------------------------------

# A trailing bracketed cross-reference block, e.g. "[EC:1.1.1.3]" or
# "[EC:1.1.1.3] [RN:R00464]". Only the EC form is stripped per D6 -- the
# bridge's `norm()` is defined narrowly against what was measured, not
# generalised to every bracket KEGG ever emits.
_TRAILING_EC_RE = re.compile(r"\s*\[EC:[^\]]*\]\s*$")

# KEGG flat-file field lines start at column 0 with an upper-case field tag
# (e.g. "ENTRY", "SYMBOL", "NAME", "DEFINITION") followed by whitespace and
# the value; continuation lines for the same field are blank-tag (indented,
# no tag token). This matches a field-start line generically enough to
# survive the 90.1 vs. 109.1 schema difference, since we key off tag name,
# not position.
_FIELD_START_RE = re.compile(r"^([A-Z][A-Z0-9_]*)\s+(.*)$")


@dataclass
class KoEntry:
    """One parsed KEGG `ko` flat-file record, fields relevant to the bridge only."""

    ko_id: str
    symbol_field: str | None  # raw SYMBOL field text, if present
    name_field: str | None  # raw NAME field text, if present


def _iter_ko_entries(ko_file: Path) -> Iterator[KoEntry]:
    """Parse a KEGG `ko` flat file into one :class:`KoEntry` per record.

    Handles both LF and CRLF line endings (release 90.1 uses CRLF; text
    mode with universal newlines normalises this automatically, but we
    open explicitly with ``newline=None`` to make that reliance visible
    rather than incidental). Records are separated by a bare ``///``
    line, matching the standard KEGG flat-file convention shared with
    ``genes``/``compound``/``reaction`` releases.
    """
    current_fields: dict[str, list[str]] = {}
    current_tag: str | None = None

    def _flush() -> KoEntry | None:
        if "ENTRY" not in current_fields:
            return None
        # ENTRY value looks like "K00001                      KO"; the KO
        # id is the first whitespace-delimited token.
        entry_value = " ".join(current_fields["ENTRY"]).strip()
        ko_id = entry_value.split()[0] if entry_value else ""
        if not ko_id:
            return None
        symbol_field = (
            " ".join(current_fields["SYMBOL"]).strip()
            if "SYMBOL" in current_fields
            else None
        )
        name_field = (
            " ".join(current_fields["NAME"]).strip()
            if "NAME" in current_fields
            else None
        )
        return KoEntry(ko_id=ko_id, symbol_field=symbol_field, name_field=name_field)

    with ko_file.open("r", encoding="utf-8", errors="replace", newline=None) as fh:
        for raw_line in fh:
            line = raw_line.rstrip("\n").rstrip("\r")
            if line.strip() == "///":
                entry = _flush()
                if entry is not None:
                    yield entry
                current_fields = {}
                current_tag = None
                continue
            if not line.strip():
                continue
            match = _FIELD_START_RE.match(line)
            if match:
                current_tag = match.group(1)
                current_fields.setdefault(current_tag, []).append(match.group(2).strip())
            elif current_tag is not None:
                # Continuation line for the currently-open field.
                current_fields[current_tag].append(line.strip())
        # A file without a trailing "///" still has one entry buffered.
        entry = _flush()
        if entry is not None:
            yield entry


def _detect_layout_and_build_symbols(
    entries: list[KoEntry],
) -> tuple[str, dict[str, str]]:
    """Detect which field carries the symbol, and build ``ko_id -> symbol``.

    Detection rule: if any parsed entry carries a non-empty ``SYMBOL``
    field, the release is the newer (109.1-style) layout and the symbol
    is read from ``SYMBOL``. Otherwise the release is the older
    (90.1-style) layout, where the symbol lives in ``NAME`` instead.
    This is a release-wide decision (KEGG does not mix layouts within one
    flat file), not a per-entry one.

    Returns ``(layout_name, {ko_id: raw_symbol_text})``. Entries that
    have no value in the detected field are simply absent from the
    returned map -- they are genuinely symbol-less KOs, not a parse
    failure, and will fall through to "id alone, no bridge" at
    annotation time (D5/D6.3).
    """
    has_symbol_field = any(e.symbol_field for e in entries)
    if has_symbol_field:
        layout = "SYMBOL-field (KEGG >=109.1-style)"
        symbols = {e.ko_id: e.symbol_field for e in entries if e.symbol_field}
    else:
        layout = "NAME-field (KEGG <=90.1-style)"
        symbols = {e.ko_id: e.name_field for e in entries if e.name_field}
    return layout, symbols


# ---------------------------------------------------------------------------
# ko_list parsing
# ---------------------------------------------------------------------------


def _iter_ko_list_definitions(ko_list_file: Path) -> Iterator[tuple[str, str]]:
    """Yield ``(ko_id, definition)`` pairs from a KOfamScan ``ko_list`` TSV.

    ``ko_list`` is a header + TSV body. The KO id column is the first
    column regardless of its exact header spelling (``knum``, ``#knum``,
    ``ko``, ...); the definition column is located by name (case
    insensitive substring match on ``"defini"``), falling back to the
    last column if no such header is found, since every observed
    ``ko_list`` variant carries the definition last.
    """
    with ko_list_file.open("r", encoding="utf-8", errors="replace", newline=None) as fh:
        header_line = fh.readline()
        if not header_line:
            return
        header = header_line.rstrip("\n").rstrip("\r").lstrip("#").split("\t")
        definition_idx = None
        for idx, col in enumerate(header):
            if "defini" in col.strip().lower():
                definition_idx = idx
                break
        if definition_idx is None:
            definition_idx = len(header) - 1

        for raw_line in fh:
            line = raw_line.rstrip("\n").rstrip("\r")
            if not line.strip():
                continue
            cols = line.split("\t")
            if len(cols) <= definition_idx:
                continue
            ko_id = cols[0].strip().lstrip("#")
            definition = cols[definition_idx].strip()
            if not ko_id:
                continue
            yield ko_id, definition


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def norm(definition: str) -> str:
    """Normalise a ``ko_list`` definition the way ``f_description`` expects.

    Strips a single trailing ``[EC:...]`` block, lowercases, and strips
    surrounding whitespace. This is deliberately narrow -- only the
    transformation measured (D6) to reproduce ``f_fh`` when composed with
    the KEGG symbol, not a general-purpose text cleaner.
    """
    stripped = _TRAILING_EC_RE.sub("", definition)
    return stripped.strip().lower()


def compose(symbol: str, definition: str) -> str:
    """``SYMBOL.lower() + "; " + norm(definition)`` -- the D6 bridge string."""
    return f"{symbol.strip().lower()}; {norm(definition)}"


# ---------------------------------------------------------------------------
# Build + write
# ---------------------------------------------------------------------------


def build_map(
    ko_file: Path,
    ko_list_file: Path,
) -> tuple[str, dict[str, str], int]:
    """Parse both inputs and return ``(detected_layout, {ko_id: composed}, ko_list_rows)``.

    Raises :class:`RuntimeError` if the detected KEGG ``ko`` layout
    yields zero non-empty symbols across the whole release -- the D6.2
    release-schema trap, caught rather than silently producing an
    all-empty-symbol (and therefore all-unbridgeable) table.
    """
    entries = list(_iter_ko_entries(ko_file))
    if not entries:
        raise RuntimeError(
            f"KEGG ko flat file at {ko_file} yielded zero parsed ENTRY records. "
            "Confirm this is a KEGG `ko` release flat file (not `ko_list`, "
            "not gzipped, not truncated)."
        )

    layout, symbols = _detect_layout_and_build_symbols(entries)
    if not symbols:
        raise RuntimeError(
            f"KEGG ko release at {ko_file} produced zero non-empty symbols "
            f"after detecting layout={layout!r} across {len(entries)} parsed "
            "entries. This is the release-schema trap: KEGG 109.1 stores the "
            "gene symbol in a SYMBOL field and the description in NAME; "
            "release 90.1 stores the symbol in NAME and the description in "
            "DEFINITION (with CRLF line endings). A parser assuming the "
            "wrong layout parses cleanly and matches nothing. Inspect the "
            f"raw file at {ko_file} to confirm which field actually carries "
            "the gene symbol for this release, and if neither SYMBOL nor "
            "NAME field is populated at all, this release's schema differs "
            "from both known layouts and this script needs a third case."
        )

    composed: dict[str, str] = {}
    ko_list_rows = 0
    for ko_id, definition in _iter_ko_list_definitions(ko_list_file):
        ko_list_rows += 1
        symbol = symbols.get(ko_id)
        if symbol is None:
            # No KEGG symbol for this KO -- it cannot bridge; leave it out
            # of the table rather than composing a symbol-less (and
            # therefore never-matching) row.
            continue
        composed[ko_id] = compose(symbol, definition)

    return layout, composed, ko_list_rows


def write_table(
    output_path: Path,
    composed: dict[str, str],
    *,
    kegg_release: str,
    ko_list_version: str,
    layout: str,
    ko_file: Path,
    ko_list_file: Path,
) -> None:
    """Write ``ko_function_map.tsv`` with a header comment recording provenance."""
    generated = datetime.datetime.now(tz=datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fh:
        fh.write(
            "# ko_function_map.tsv -- generated by "
            "kbutillib.domains.genome.annotation.build_ko_function_map\n"
        )
        fh.write(f"# kegg_ko_release: {kegg_release}\n")
        fh.write(f"# ko_list_version: {ko_list_version}\n")
        fh.write(f"# detected_ko_layout: {layout}\n")
        fh.write(f"# source_ko_file: {ko_file}\n")
        fh.write(f"# source_ko_list_file: {ko_list_file}\n")
        fh.write(f"# generated_utc: {generated}\n")
        fh.write(f"# row_count: {len(composed)}\n")
        fh.write("# KEGG-derived content -- do not commit this file.\n")
        fh.write("ko_id\tcomposed_string\n")
        for ko_id in sorted(composed):
            fh.write(f"{ko_id}\t{composed[ko_id]}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build ko_function_map.tsv, the KOFAMSCAN reaction-bridge table, "
            "from a KEGG ko flat-file release and a KOfamScan ko_list. Run "
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
        "--ko-list",
        type=Path,
        required=True,
        help="Path to the matching KOfamScan ko_list TSV (e.g. profiles/<name>.txt).",
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
        "--ko-list-version",
        required=True,
        help=(
            "ko_list / profile-set version identifier to record in the "
            "output header (e.g. '2025-11-03'), matching the "
            "kofam_profile_set name this table is built to accompany."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output path for ko_function_map.tsv (outside git; deployment data root).",
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

    layout, composed, ko_list_rows = build_map(args.ko_file, args.ko_list)
    logger.info(
        "Detected KEGG ko layout=%r; composed %d ko_function_map rows "
        "(of %d ko_list rows read).",
        layout,
        len(composed),
        ko_list_rows,
    )

    write_table(
        args.output,
        composed,
        kegg_release=args.kegg_release,
        ko_list_version=args.ko_list_version,
        layout=layout,
        ko_file=args.ko_file,
        ko_list_file=args.ko_list,
    )
    logger.info("Wrote %s (%d rows).", args.output, len(composed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
