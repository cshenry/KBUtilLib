#!/usr/bin/env python3
"""Build a GO -> description dictionary (``go_description_map.tsv``) from ``go-basic.obo``.

``go-basic.obo`` is OBO stanza format: a header block, then a sequence of
``[Term]`` (and other) stanzas, each a run of ``tag: value`` lines up to
the next blank line or ``[...]`` header. Only ``[Term]`` stanzas are read
here; within each, ``id:`` gives the GO accession and ``name:`` gives its
description. Obsolete terms (``is_obsolete: true``) are skipped -- a stale
placeholder description is worse than ``describe()`` returning ``None`` for
an accession this release no longer defines.

DO NOT COMMIT THE OUTPUT
-------------------------
``go_description_map.tsv`` is derived from the Gene Ontology's bulk
``go-basic.obo`` release -- third-party bulk ontology data, not this
repo's own content. The generated table must never be committed; it is
built once per deployment, on the compute host, from a release the host
already holds, and lives outside git under the deployment's data root.
``go_description_map*.tsv`` is listed in this repo's ``.gitignore`` for
exactly this reason -- a future contributor should not "helpfully" commit a
run of this script.

Usage
-----
    python3 -m kbutillib.domains.genome.annotation.build_go_description_map \\
        --go-obo /path/to/go-basic.obo \\
        --release-version 2025-11-03 \\
        --output /path/to/deployment/data/go_description_map.tsv
"""

from __future__ import annotations

import argparse
import datetime
import logging
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class GoEntry:
    """One parsed ``[Term]`` stanza from a ``go-basic.obo`` file."""

    go_id: str
    name: str | None
    is_obsolete: bool


def _iter_go_entries(go_obo_file: Path) -> Iterator[GoEntry]:
    """Parse ``[Term]`` stanzas out of a ``go-basic.obo`` file.

    Stanzas start with a bracketed header line (``[Term]``, ``[Typedef]``,
    ...) and run until the next bracketed header or end of file; a blank
    line does not end a stanza in real OBO releases (only the next
    header does), so this splits strictly on header lines rather than on
    blank lines. Only ``[Term]`` stanzas are yielded; ``[Typedef]`` and
    any other stanza kind is skipped.
    """
    in_term = False
    go_id: str | None = None
    name: str | None = None
    is_obsolete = False

    def _flush() -> GoEntry | None:
        if go_id is None:
            return None
        return GoEntry(go_id=go_id, name=name, is_obsolete=is_obsolete)

    with go_obo_file.open("r", encoding="utf-8", errors="replace", newline=None) as fh:
        for raw_line in fh:
            line = raw_line.rstrip("\n").rstrip("\r")
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                if in_term:
                    entry = _flush()
                    if entry is not None:
                        yield entry
                go_id = None
                name = None
                is_obsolete = False
                in_term = stripped == "[Term]"
                continue
            if not in_term or not stripped:
                continue
            tag, sep, value = stripped.partition(":")
            if not sep:
                continue
            tag = tag.strip()
            value = value.strip()
            if tag == "id":
                go_id = value
            elif tag == "name":
                name = value
            elif tag == "is_obsolete":
                is_obsolete = value.lower() == "true"
    if in_term:
        entry = _flush()
        if entry is not None:
            yield entry


def build_description_map(go_obo_file: Path) -> dict[str, str]:
    """Parse ``go-basic.obo`` into ``{go_id: name}``, skipping obsolete terms.

    Raises:
        RuntimeError: If the file yields zero parsed ``[Term]`` stanzas
            with an ``id:`` line, or if every non-obsolete stanza has no
            ``name:`` line (both named as "OBO stanza format ([Term]
            blocks with id:/name: lines)" so a caller can tell this apart
            from a missing-file degradation).
    """
    entries = list(_iter_go_entries(go_obo_file))
    if not entries:
        raise RuntimeError(
            f"GO OBO file at {go_obo_file} yielded zero parsed [Term] stanzas "
            "with an 'id:' line, using the expected OBO stanza format "
            "([Term] blocks with id:/name: lines). Confirm this is a "
            "go-basic.obo release, not a different OBO export."
        )

    descriptions = {
        e.go_id: e.name for e in entries if e.name and not e.is_obsolete
    }
    if not descriptions:
        raise RuntimeError(
            f"GO OBO file at {go_obo_file} produced zero non-empty, "
            f"non-obsolete descriptions across {len(entries)} parsed [Term] "
            "stanzas using the expected OBO stanza format ([Term] blocks "
            "with id:/name: lines). A parser reading the wrong tag, or a "
            "release with name: lines in an unexpected shape, parses "
            f"cleanly and matches nothing. Inspect the raw file at "
            f"{go_obo_file} to confirm name: lines are present."
        )
    return descriptions


def write_table(
    output_path: Path,
    descriptions: dict[str, str],
    *,
    release_version: str,
    go_obo_file: Path,
) -> None:
    """Write the two-column ``go_id\\tdescription`` table with a provenance header."""
    generated = datetime.datetime.now(tz=datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fh:
        fh.write(
            "# go_description_map.tsv -- generated by "
            "kbutillib.domains.genome.annotation.build_go_description_map\n"
        )
        fh.write(f"# go_obo_release: {release_version}\n")
        fh.write(f"# source_go_obo_file: {go_obo_file}\n")
        fh.write(f"# generated_utc: {generated}\n")
        fh.write(f"# row_count: {len(descriptions)}\n")
        fh.write("# Gene Ontology-derived content -- do not commit this file.\n")
        fh.write("# go_id\tdescription\n")
        for go_id in sorted(descriptions):
            fh.write(f"{go_id}\t{descriptions[go_id]}\n")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build go_description_map.tsv, a GO -> description table, from "
            "a go-basic.obo release, for OntologyDictionary. Run this once "
            "per deployment on the compute host; never commit its output "
            "(see module docstring)."
        )
    )
    parser.add_argument(
        "--go-obo",
        type=Path,
        required=True,
        help="Path to a go-basic.obo release file (uncompressed).",
    )
    parser.add_argument(
        "--release-version",
        required=True,
        help=(
            "Release/date identifier to record in the output header (e.g. "
            "'2025-11-03'). Not reliably auto-detectable from file content, "
            "so it must be supplied explicitly."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output path for go_description_map.tsv (outside git; deployment data root).",
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

    descriptions = build_description_map(args.go_obo)
    logger.info("Parsed %d go_description_map rows.", len(descriptions))

    write_table(
        args.output,
        descriptions,
        release_version=args.release_version,
        go_obo_file=args.go_obo,
    )
    logger.info("Wrote %s (%d rows).", args.output, len(descriptions))
    return 0


if __name__ == "__main__":
    sys.exit(main())
