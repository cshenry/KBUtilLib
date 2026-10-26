#!/usr/bin/env python3
"""t11 -- the ONE COMMAND that runs the COUNCIL-RECORD-LAKEHOUSE-V1 acceptance rehearsal.

    python scripts/council/acceptance_rehearsal.py \
        --snapshot /scratch/chenry/tmp/council-snapshot-20261023.sqlite \
        --work-root /scratch/chenry/t11-rehearsal \
        --report agent-io/reports/2026-10-26-council-lakehouse-t11-rehearsal.txt

SPENDS ZERO TOKENS AND NEEDS NO LIVE COUNCIL RUN. It drives the shipped rail
against the real 2026-10-23 production snapshot and asserts what landed BY
DIRECT QUERY. Every numbered step reports ASSERTED (with the measured value),
FAILED (with evidence) or NOT ASSERTED (with the reason) -- see
:mod:`kbutillib.domains.kbase.berdl.council_rehearsal`, which holds the
logic; this file is argument handling and output.

EVERYTHING IT WRITES IS DISPOSABLE AND UNDER ``--work-root``, which must be
on scratch. The snapshot is READ-ONLY input and is never written. No
production clearinghouse row is written and the live in-pod load is reported
as ``awaiting operator acceptance``, never attempted.

EXIT CODES. ``0`` only when no step FAILED. ``1`` when a step failed. ``2``
when the rehearsal refused its target. NOT ASSERTED steps do NOT fail the
run: a leg whose input does not exist is a reported gap, and conflating it
with a broken one is what this whole report shape exists to prevent. Use
``--strict`` to make a NOT ASSERTED step exit non-zero too.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:  # run from a checkout without install
    sys.path.insert(0, str(REPO_ROOT / "src"))

from kbutillib.domains.kbase.berdl.council_rehearsal import (  # noqa: E402
    DEFAULT_PROVENANCE,
    DEFAULT_VERDICT_EXPORT,
    FAILED,
    NOT_ASSERTED,
    REHEARSAL_STORE_ID,
    RehearsalRefused,
    render_report,
    run_rehearsal,
)

#: The read-only decompressed copy tranche 1 left behind. Its uncompressed
#: sha256 is 37535e28382fd3323af9eab2fa0a6f4612d0852856c650cf6cf42740e9bcef32;
#: if it is gone, re-decompress from
#: ~/Dropbox/Jobs/function-to-go/_store-backups/council-snapshot-20261023.sqlite.xz.
DEFAULT_SNAPSHOT = "/scratch/chenry/tmp/council-snapshot-20261023.sqlite"
EXPECTED_SNAPSHOT_SHA256 = (
    "37535e28382fd3323af9eab2fa0a6f4612d0852856c650cf6cf42740e9bcef32"
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--snapshot",
        default=DEFAULT_SNAPSHOT,
        help="the council store snapshot to rehearse against (READ-ONLY)",
    )
    parser.add_argument(
        "--work-root",
        required=True,
        help="disposable scratch directory for everything this run writes",
    )
    parser.add_argument("--store-id", default=REHEARSAL_STORE_ID)
    parser.add_argument("--provenance", default=DEFAULT_PROVENANCE)
    parser.add_argument("--verdict-export", default=DEFAULT_VERDICT_EXPORT)
    parser.add_argument(
        "--only",
        default="",
        help="comma-separated step numbers to run (default: all of 0-9)",
    )
    parser.add_argument("--report", default="", help="write the text report here")
    parser.add_argument("--json", default="", help="write the report JSON here")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero on a NOT ASSERTED step as well as a FAILED one",
    )
    parser.add_argument(
        "--skip-digest-check",
        action="store_true",
        help="do not verify the snapshot's sha256 before using it",
    )
    return parser.parse_args(argv)


def verify_snapshot(path: Path) -> str:
    """Return *path*'s sha256, and say so when it is not the expected one.

    The digest is VERIFIED rather than assumed because /scratch on this host
    is ephemeral and not backed up: a snapshot that was re-decompressed from
    a different archive, or truncated, would archive 203,821 rows keyed on a
    digest that does not describe them.
    """
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 22), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    snapshot = Path(args.snapshot).expanduser()
    if not snapshot.is_file():
        print(
            f"no snapshot at {snapshot}. Re-decompress it:\n"
            "  xz -dk -c ~/Dropbox/Jobs/function-to-go/_store-backups/"
            f"council-snapshot-20261023.sqlite.xz > {snapshot}",
            file=sys.stderr,
        )
        return 2

    if not args.skip_digest_check:
        measured = verify_snapshot(snapshot)
        print(f"snapshot sha256: {measured}", file=sys.stderr)
        if measured != EXPECTED_SNAPSHOT_SHA256:
            print(
                f"REFUSING: {snapshot} hashes to {measured} but the "
                f"2026-10-23 snapshot is {EXPECTED_SNAPSHOT_SHA256}. These "
                "are not the same bytes. Re-decompress from the .xz in "
                "Dropbox, or pass --skip-digest-check if you deliberately "
                "mean to rehearse against a different store.",
                file=sys.stderr,
            )
            return 2

    try:
        report = run_rehearsal(
            snapshot=snapshot,
            work_root=args.work_root,
            store_id=args.store_id,
            provenance=args.provenance,
            verdict_export=args.verdict_export,
            only=[part for part in args.only.split(",") if part.strip()] or None,
        )
    except RehearsalRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    # THE JSON IS PERSISTED FIRST, BEFORE RENDERING. A rehearsal run is
    # minutes of work against a 896 MB snapshot, and the first real run of
    # this script lost a COMPLETE one to a TypeError inside the renderer. The
    # measurement is the valuable artifact; the text is a view of it.
    if args.json:
        target = Path(args.json).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        print(f"report JSON written to {target}", file=sys.stderr)

    text = render_report(report)
    print(text)
    if args.report:
        target = Path(args.report).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
        print(f"\nreport written to {target}", file=sys.stderr)

    summary = report["summary"]
    if summary[FAILED]:
        return 1
    if args.strict and summary[NOT_ASSERTED]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
