#!/usr/bin/env python3
"""Build a small KOfamScan "guard" profile set for the fast real-dependency guard.

Background
----------
``exec_annotation`` sweeps all ~27,883 KOfam HMM profiles regardless of
query size, so a guard run against the full production profile set costs
2-4 minutes and will in practice be skipped (PRD ``kbdl-local-bakta-
kofamscan-v1``, Testing / "Real-dependency guards"). This script builds a
small subset -- roughly 50 HMMs chosen to actually hit E. coli -- laid out
as a ``<name>/`` directory of ``.hmm`` files plus a sibling ``<name>.txt``
``ko_list`` slice, which is the **same pairing** ``validate_kofam_profiles``
enforces in production (a profile directory and its matching ``ko_list``
must travel together, D8). A guard built on this layout therefore also
exercises the pairing rule and ``kofam_profile_set`` selection, not only
the HMM search itself.

DO NOT COMMIT THE OUTPUT
-------------------------
Both the copied ``.hmm`` files and the sliced ``ko_list`` are KEGG-derived
content (subsets of the production KOfam profile set and its ``ko_list``),
so the same redistribution-terms reasoning as ``build_ko_function_map.py``
applies: the generated guard set must never be committed. It is built
**on the host**, from the production profile set already registered
there, and lives outside git (under the deployment data root, or wherever
``ops/DEPLOY.md`` names for guard discovery). This script is the committed
artifact; its output is not. A future contributor should not "helpfully"
commit a run of this script -- see ``.gitignore``.

Selection strategy, and an explicit limitation
-----------------------------------------------
"Chosen to actually hit E. coli" is an empirical claim that can only be
verified by actually running KOfamScan against an E. coli genome -- which
this script, authored on primary-laptop with no access to poplar's
profile set or database, cannot do. Rather than hard-coding a fixed list
of KO ids from memory (fabricating a precision this script cannot verify),
:data:`GUARD_GENE_PATTERNS` is a list of case-insensitive text fragments
matched against each row's ``definition`` column in a **real, on-host**
``ko_list`` at generation time. Every pattern targets a gene family that
is single-copy, essential, and near-universal across bacteria --
ribosomal proteins, RNA/DNA polymerase subunits, DNA gyrase, translation
elongation factors, chaperonins, and central carbon metabolism enzymes --
so the *claim* "this gene family is present in E. coli" is safe on
biological grounds even though this script cannot independently confirm
that KOfamScan actually calls a hit for each one at the configured
threshold. The generated set should be spot-checked against a real
KOfamScan run on the host before being relied on as a guard; that
verification is out of scope for this task (see the work-record).

Because selection is pattern-matched against whatever ``ko_list`` is
handed to it, this script is self-correcting across KEGG releases: it
never selects a nonexistent KO id, and it reports which patterns matched
nothing so an operator can see the gap rather than getting a silently
undersized guard set.

Usage
-----
    python3 -m kbutillib.domains.genome.annotation.build_guard_kofam_profiles \\
        --profiles-dir /path/to/kofam-root/profiles/2025-11-03 \\
        --ko-list /path/to/kofam-root/profiles/2025-11-03.txt \\
        --output-dir /path/to/deployment/data/kofam-guard \\
        --name guard

produces ``<output-dir>/guard/`` (the HMM subset) and
``<output-dir>/guard.txt`` (the matching ``ko_list`` slice), ready to
register as a ``KOFAM_PROFILES`` reference and select via
``kofam_profile_set="guard"``.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Guard gene selection patterns
#
# Ordered, case-insensitive substring/regex fragments matched against each
# ko_list row's `definition` column. Each entry targets a single-copy,
# essential, near-universal bacterial gene family, so match order roughly
# tracks "most confidently present in any bacterium first". There are more
# patterns here than the ~50 target count so that wording drift between
# KEGG releases (or a pattern simply not matching this release's exact
# phrasing) does not undersize the guard set.
# ---------------------------------------------------------------------------

GUARD_GENE_PATTERNS: list[str] = [
    # Large ribosomal subunit proteins (L1-L36) -- single-copy, essential,
    # present in every free-living bacterium.
    *[f"ribosomal protein l{n}" for n in (
        1, 2, 3, 4, 5, 6, 9, 10, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22,
        23, 24, 25, 27, 28, 29, 30, 32, 33, 34, 35, 36,
    )],
    # Small ribosomal subunit proteins (S1-S21) -- same rationale.
    *[f"ribosomal protein s{n}" for n in (
        1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19,
        20, 21,
    )],
    # RNA polymerase core subunits and primary sigma factor.
    "dna-directed rna polymerase subunit alpha",
    "dna-directed rna polymerase subunit beta",
    "dna-directed rna polymerase subunit beta'",
    "rna polymerase sigma factor",
    # DNA replication / topology.
    "dna gyrase subunit a",
    "dna gyrase subunit b",
    "dna polymerase i",
    "dna polymerase iii subunit alpha",
    "dna topoisomerase i",
    # Translation machinery beyond the ribosome itself.
    "elongation factor tu",
    "elongation factor g",
    "elongation factor ts",
    "translation initiation factor if-1",
    "translation initiation factor if-2",
    "translation initiation factor if-3",
    # Chaperones / stress-response housekeeping.
    "chaperonin groel",
    "chaperonin groes",
    "molecular chaperone dnak",
    "molecular chaperone dnaj",
    "recombination protein reca",
    # ATP synthase subunits.
    "f-type h+-transporting atpase subunit alpha",
    "f-type h+-transporting atpase subunit beta",
    "f-type h+-transporting atpase subunit gamma",
    # Central carbon metabolism (glycolysis / TCA), single-copy in E. coli.
    "glyceraldehyde 3-phosphate dehydrogenase",
    "enolase",
    "pyruvate kinase",
    "citrate synthase",
    "isocitrate dehydrogenase",
    "malate dehydrogenase",
    "6-phosphofructokinase",
    "fructose-bisphosphate aldolase",
    "triosephosphate isomerase",
    "phosphoglycerate kinase",
    # Aminoacyl-tRNA synthetases -- one per amino acid, essential, single copy.
    "alanyl-trna synthetase",
    "aspartyl-trna synthetase",
    "leucyl-trna synthetase",
    "isoleucyl-trna synthetase",
    "valyl-trna synthetase",
    "seryl-trna synthetase",
    "glycyl-trna synthetase",
]

DEFAULT_TARGET_COUNT = 50


def _load_ko_list_rows(ko_list_file: Path) -> tuple[str, list[tuple[str, str, str]]]:
    """Return ``(header_line, [(ko_id, definition, raw_line), ...])``.

    ``definition`` is located by header name (case-insensitive substring
    match on ``"defini"``), falling back to the last column, matching
    ``build_ko_function_map.py``'s convention for the same file format.
    """
    with ko_list_file.open("r", encoding="utf-8", errors="replace", newline=None) as fh:
        header_line = fh.readline().rstrip("\n").rstrip("\r")
        header_cols = header_line.lstrip("#").split("\t")
        definition_idx = None
        for idx, col in enumerate(header_cols):
            if "defini" in col.strip().lower():
                definition_idx = idx
                break
        if definition_idx is None:
            definition_idx = len(header_cols) - 1

        rows: list[tuple[str, str, str]] = []
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
            rows.append((ko_id, definition, line))
    return header_line, rows


def select_guard_kos(
    ko_list_rows: list[tuple[str, str, str]],
    patterns: list[str],
    target_count: int,
) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Pick up to ``target_count`` rows, one per matching pattern in order.

    Returns ``(selected_rows, unmatched_patterns)``. Selection is
    deterministic: for each pattern, the lowest ``ko_id`` (lexicographic,
    which for KEGG's ``K#####`` ids is also numeric order) among matching,
    not-yet-selected rows is taken.
    """
    selected: list[tuple[str, str, str]] = []
    selected_ids: set[str] = set()
    unmatched: list[str] = []

    for pattern in patterns:
        if len(selected) >= target_count:
            break
        needle = pattern.lower()
        candidates = sorted(
            (row for row in ko_list_rows if needle in row[1].lower() and row[0] not in selected_ids),
            key=lambda row: row[0],
        )
        if not candidates:
            unmatched.append(pattern)
            continue
        chosen = candidates[0]
        selected.append(chosen)
        selected_ids.add(chosen[0])

    return selected, unmatched


def build_guard_set(
    profiles_dir: Path,
    ko_list_file: Path,
    output_dir: Path,
    name: str,
    target_count: int,
    patterns: list[str],
) -> tuple[int, int, list[str], list[str]]:
    """Build the guard profile directory + ko_list slice.

    Returns ``(hmm_copied, rows_written, missing_hmm_ko_ids, unmatched_patterns)``.
    Raises :class:`RuntimeError` if zero KOs are selected at all (wrong
    ``--ko-list``/``--profiles-dir`` pairing, or a pattern list that
    matches nothing in this release).
    """
    header_line, ko_list_rows = _load_ko_list_rows(ko_list_file)
    selected, unmatched_patterns = select_guard_kos(ko_list_rows, patterns, target_count)

    if not selected:
        raise RuntimeError(
            f"Selected zero guard KOs from {ko_list_file} against "
            f"{len(patterns)} candidate gene patterns. Confirm --ko-list "
            "points at a real KOfamScan ko_list (not the KEGG ko flat "
            "file, not truncated) -- none of the built-in universal "
            "marker-gene patterns matched any definition text in this "
            "file, which is not expected of a genuine ko_list."
        )

    guard_profiles_dir = output_dir / name
    guard_profiles_dir.mkdir(parents=True, exist_ok=True)

    hmm_copied = 0
    missing_hmm_ko_ids: list[str] = []
    kept_rows: list[tuple[str, str, str]] = []
    for ko_id, _definition, raw_line in selected:
        src_hmm = profiles_dir / f"{ko_id}.hmm"
        if not src_hmm.is_file():
            missing_hmm_ko_ids.append(ko_id)
            continue
        shutil.copy2(src_hmm, guard_profiles_dir / f"{ko_id}.hmm")
        hmm_copied += 1
        kept_rows.append((ko_id, _definition, raw_line))

    if hmm_copied == 0:
        raise RuntimeError(
            f"Matched {len(selected)} guard KOs by definition text, but "
            f"none of them have a .hmm file under {profiles_dir}. Confirm "
            "--profiles-dir points at the production profile directory "
            "(a directory of <KO_ID>.hmm files) matching --ko-list."
        )

    guard_ko_list_file = output_dir / f"{name}.txt"
    with guard_ko_list_file.open("w", encoding="utf-8") as fh:
        fh.write(header_line + "\n")
        for _ko_id, _definition, raw_line in kept_rows:
            fh.write(raw_line + "\n")

    return hmm_copied, len(kept_rows), missing_hmm_ko_ids, unmatched_patterns


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a small guard KOfamScan profile set (<name>/ + <name>.txt) "
            "from the production profile set, for the fast real-dependency "
            "guard. Run this once per deployment on the compute host; never "
            "commit its output (see module docstring)."
        )
    )
    parser.add_argument(
        "--profiles-dir",
        type=Path,
        required=True,
        help="Production KOfam profile directory (a directory of <KO_ID>.hmm files).",
    )
    parser.add_argument(
        "--ko-list",
        type=Path,
        required=True,
        help="Matching production ko_list TSV (e.g. profiles/<name>.txt).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory under which <name>/ and <name>.txt are written.",
    )
    parser.add_argument(
        "--name",
        default="guard",
        help="Guard profile-set name (default: 'guard'); also the kofam_profile_set value.",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=DEFAULT_TARGET_COUNT,
        help=f"Target number of guard KOs (default: {DEFAULT_TARGET_COUNT}).",
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

    hmm_copied, rows_written, missing_hmm_ko_ids, unmatched_patterns = build_guard_set(
        args.profiles_dir,
        args.ko_list,
        args.output_dir,
        args.name,
        args.count,
        GUARD_GENE_PATTERNS,
    )

    logger.info(
        "Wrote %d HMM profile(s) to %s/ and %d ko_list row(s) to %s.txt",
        hmm_copied,
        args.output_dir / args.name,
        rows_written,
        args.output_dir / args.name,
    )
    if missing_hmm_ko_ids:
        logger.warning(
            "%d matched KO id(s) had no .hmm file under %s and were skipped: %s",
            len(missing_hmm_ko_ids),
            args.profiles_dir,
            ", ".join(missing_hmm_ko_ids),
        )
    if unmatched_patterns:
        logger.info(
            "%d gene pattern(s) matched no ko_list row in this release "
            "(informational -- selection stops once --count is reached, "
            "so this is expected if the target count was hit first): %s",
            len(unmatched_patterns),
            ", ".join(unmatched_patterns),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
