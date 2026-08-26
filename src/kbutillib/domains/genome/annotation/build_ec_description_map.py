#!/usr/bin/env python3
"""Build an EC -> description dictionary (``ec_description_map.tsv``) from ExPASy ``enzyme.dat``.

``enzyme.dat`` is ExPASy's record-block format: each enzyme record is a
sequence of two-letter tagged lines (``ID``, ``DE``, ``AN``, ``CA``, ...)
terminated by a bare ``//`` line. Only ``ID`` (the EC number) and ``DE``
(the recommended description, occasionally wrapped across multiple ``DE``
lines) are needed here.

DO NOT COMMIT THE OUTPUT
-------------------------
``ec_description_map.tsv`` is derived from ExPASy's bulk ``enzyme.dat``
release -- third-party bulk ontology data, not this repo's own content, and
under materially different redistribution terms than this repo's licence.
The generated table must never be committed; it is built once per
deployment, on the compute host, from a release the host already holds,
and lives outside git under the deployment's data root.
``ec_description_map*.tsv`` is listed in this repo's ``.gitignore`` for
exactly this reason -- a future contributor should not "helpfully" commit a
run of this script.

Usage
-----
    python3 -m kbutillib.domains.genome.annotation.build_ec_description_map \\
        --enzyme-dat /path/to/enzyme.dat \\
        --release-version 2025-11 \\
        --output /path/to/deployment/data/ec_description_map.tsv
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
class EcEntry:
    """One parsed ``enzyme.dat`` record."""

    ec_id: str
    description: str | None


def _iter_ec_entries(enzyme_dat_file: Path) -> Iterator[EcEntry]:
    """Parse an ExPASy ``enzyme.dat`` file into one :class:`EcEntry` per record.

    Records are ``ID``/``DE``/... tagged-line blocks terminated by a bare
    ``//`` line. A record's ``DE`` value may wrap across multiple ``DE``
    lines (e.g. a long recommended name split by ExPASy's fixed line
    width); these are joined with a single space. Handles both LF and
    CRLF line endings via universal-newline text mode.
    """
    ec_id: str | None = None
    de_parts: list[str] = []

    def _flush() -> EcEntry | None:
        if ec_id is None:
            return None
        description = " ".join(de_parts).strip().rstrip(".").strip() or None
        return EcEntry(ec_id=ec_id, description=description)

    with enzyme_dat_file.open(
        "r", encoding="utf-8", errors="replace", newline=None
    ) as fh:
        for raw_line in fh:
            line = raw_line.rstrip("\n").rstrip("\r")
            if line.strip() == "//":
                entry = _flush()
                if entry is not None:
                    yield entry
                ec_id = None
                de_parts = []
                continue
            if not line.strip():
                continue
            parts = line.split(None, 1)
            tag = parts[0].strip()
            value = parts[1].strip() if len(parts) > 1 else ""
            if tag == "ID":
                ec_id = value.split()[0] if value else None
            elif tag == "DE":
                de_parts.append(value)
        entry = _flush()
        if entry is not None:
            yield entry


def build_description_map(enzyme_dat_file: Path) -> dict[str, str]:
    """Parse ``enzyme.dat`` into ``{ec_id: description}``.

    Raises:
        RuntimeError: If the file yields zero parsed ``ID`` records, or if
            every parsed record has an empty/absent ``DE`` line (both
            named as "record-block format (ID/DE lines)" so a caller can
            tell this apart from a missing-file degradation).
    """
    entries = list(_iter_ec_entries(enzyme_dat_file))
    if not entries:
        raise RuntimeError(
            f"ExPASy enzyme.dat file at {enzyme_dat_file} yielded zero parsed "
            "ID records using the expected record-block format (ID/DE lines "
            "terminated by '//'). Confirm this is an uncompressed enzyme.dat "
            "release, not a different ExPASy export."
        )

    descriptions = {e.ec_id: e.description for e in entries if e.description}
    if not descriptions:
        raise RuntimeError(
            f"ExPASy enzyme.dat file at {enzyme_dat_file} produced zero "
            f"non-empty descriptions across {len(entries)} parsed ID records "
            "using the expected record-block format (ID/DE lines terminated "
            "by '//'). A parser reading the wrong tag, or a release with DE "
            "lines in an unexpected shape, parses cleanly and matches "
            f"nothing. Inspect the raw file at {enzyme_dat_file} to confirm "
            "DE lines are present and formatted as expected."
        )
    return descriptions


def write_table(
    output_path: Path,
    descriptions: dict[str, str],
    *,
    release_version: str,
    enzyme_dat_file: Path,
) -> None:
    """Write the two-column ``ec_id\\tdescription`` table with a provenance header."""
    generated = datetime.datetime.now(tz=datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fh:
        fh.write(
            "# ec_description_map.tsv -- generated by "
            "kbutillib.domains.genome.annotation.build_ec_description_map\n"
        )
        fh.write(f"# enzyme_dat_release: {release_version}\n")
        fh.write(f"# source_enzyme_dat_file: {enzyme_dat_file}\n")
        fh.write(f"# generated_utc: {generated}\n")
        fh.write(f"# row_count: {len(descriptions)}\n")
        fh.write("# ExPASy-derived content -- do not commit this file.\n")
        fh.write("# ec_id\tdescription\n")
        for ec_id in sorted(descriptions):
            fh.write(f"{ec_id}\t{descriptions[ec_id]}\n")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build ec_description_map.tsv, an EC -> description table, from "
            "an ExPASy enzyme.dat release, for OntologyDictionary. Run this "
            "once per deployment on the compute host; never commit its "
            "output (see module docstring)."
        )
    )
    parser.add_argument(
        "--enzyme-dat",
        type=Path,
        required=True,
        help="Path to an ExPASy enzyme.dat release file (uncompressed).",
    )
    parser.add_argument(
        "--release-version",
        required=True,
        help=(
            "Release/date identifier to record in the output header (e.g. "
            "'2025-11'). Not reliably auto-detectable from file content, so "
            "it must be supplied explicitly."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output path for ec_description_map.tsv (outside git; deployment data root).",
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

    descriptions = build_description_map(args.enzyme_dat)
    logger.info("Parsed %d ec_description_map rows.", len(descriptions))

    write_table(
        args.output,
        descriptions,
        release_version=args.release_version,
        enzyme_dat_file=args.enzyme_dat,
    )
    logger.info("Wrote %s (%d rows).", args.output, len(descriptions))
    return 0


if __name__ == "__main__":
    sys.exit(main())
