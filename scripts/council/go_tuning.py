#!/usr/bin/env python3
"""Extract go_mf conflicts, sample a tuning manifest, adjudicate it in tranches.

    python scripts/council/go_tuning.py extract \
        --candidate-store sqlite:////path/bench_a.sqlite --candidate-jobs J1,J2 \
        --baseline-store  sqlite:////path/live.sqlite \
        --out conflicts.jsonl

    python scripts/council/go_tuning.py sample \
        --conflicts conflicts.jsonl --seed 20261117 --out manifest.json

    python scripts/council/go_tuning.py adjudicate \
        --manifest manifest.json --matched conflicts.jsonl --pools pilot \
        --store sqlite:////path/council.sqlite --profile go_adjudicate \
        --run-dir /path/to/run

``extract`` joins the candidate (ALCF bench) jobs against the ``go_mf@4``
baseline on ``(entity_hash, data_hash)`` and classifies each matched key; with
no ``--baseline-jobs`` it uses EVERY ``go_mf@4`` job in the baseline store,
which is the normal case -- the production baseline for one entity is spread
over cancelled originals, chunks and retries, and naming them by hand is how a
chunk gets left out.  ``sample`` draws the pools from what ``extract`` wrote.

THE TWO STEPS ARE SEPARATE FILES ON PURPOSE.  The manifest is the artefact a
tuning run is judged against, and it must be reproducible from a FROZEN
conflict set rather than from whatever the stores hold on the day it is
re-run: a baseline job finishing between two runs would otherwise silently
change the sample a seed produces.  Extract once, commit the JSONL, sample
from it as often as you like.

``adjudicate`` builds the blinded ``go_mf_adjudicate@1`` items the manifest
calls for and submits them one tranche per council job, resumably: re-running
the same command after an interruption adopts the jobs it already created
instead of paying for them twice (see
``kbutillib.domains.ai.council.go_tuning.adjudicate``).  It is MEANT to be
re-run -- a tranche after the first will not start until the previous
tranche's spot-check marks have been imported into the run's ledger, so the
normal shape of a tuning pass is one invocation per tranche with a human step
in between.

THE HUMAN STEP IS ``pilot-table`` -> mark in Excel -> ``calibrate``::

    python scripts/council/go_tuning.py pilot-table \
        --manifest manifest.json --matched conflicts.jsonl \
        --store sqlite:////path/council.sqlite --run-dir /path/to/run \
        --xlsx pilot.xlsx --md pilot.md

    #  ... Chris marks the 'Chris mark' column R/W/? in pilot.xlsx ...

    python scripts/council/go_tuning.py calibrate \
        --manifest manifest.json --matched conflicts.jsonl \
        --store sqlite:////path/council.sqlite --run-dir /path/to/run \
        --xlsx pilot.xlsx --out calibration.md --record

``spot-check`` writes the same table over ONE tranche's persisted spot-check
ids, and ``read-marks --record`` imports its marks into the ledger, which is
what the next tranche's gate reads.  ``read-marks`` without ``--record`` is
the dry run: it reports what the sheet says and writes the gold as JSON.

Later tasks add subcommands here, so each one is a subparser with its own
output; nothing is shared but the store-opening helper.

EXIT CODES. 0 done; 2 refused (bad, ambiguous or too-small input, or a
disturbed marked workbook); 3 unfinished -- the adjudication run stopped at a
gate with work still planned (the token ceiling or the spot-check gate), or
``read-marks`` found rows still carrying no mark.  Not a failure, but not
finished either, and a caller that treated it as success would report a
partial corpus as complete.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:  # run from a checkout without install
    sys.path.insert(0, str(REPO_ROOT / "src"))

from kbutillib.domains.ai.council.go_tuning import calibrate as C  # noqa: E402
from kbutillib.domains.ai.council.go_tuning import table as T  # noqa: E402
from kbutillib.domains.ai.council.go_tuning.adjudicate import (  # noqa: E402
    DEFAULT_TOKEN_CEILING,
    DEFAULT_TRANCHE_SIZE,
    build_items,
    read_results,
    run_tranches,
)
from kbutillib.domains.ai.council.go_tuning.conflicts import (  # noqa: E402
    CLASSES,
    UNSTABLE_CLASSES,
    MatchedItem,
    Refused,
    match,
)
from kbutillib.domains.ai.council.go_tuning.ledger import Ledger  # noqa: E402
from kbutillib.domains.ai.council.go_tuning.sample import (  # noqa: E402
    Plan,
    build_manifest,
)


def _jobs(value: Optional[str]) -> Optional[List[str]]:
    """``None`` means "discover them"; an empty list is an error, not a wildcard."""
    if value is None:
        return None
    jobs = [j.strip() for j in value.split(",") if j.strip()]
    if not jobs:
        raise Refused("a --*-jobs option was given but named no job")
    return jobs


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------


def cmd_extract(args: argparse.Namespace) -> int:
    candidate_jobs = _jobs(args.candidate_jobs)
    if candidate_jobs is None:
        raise Refused("--candidate-jobs is required (candidate jobs are never discovered)")
    result = match(
        args.candidate_store,
        candidate_jobs,
        args.baseline_store,
        _jobs(args.baseline_jobs),
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        for item in result.items:
            handle.write(json.dumps(item.as_dict(), sort_keys=True) + "\n")

    print(f"matched {len(result.items)} key(s) from {len(result.baseline_job_ids)} baseline job(s)")
    print("\nclasses:")
    for cls in CLASSES:
        print(f"  {cls:<16} {result.counts.get(cls, 0)}")
    print(f"\nconflicts (disjoint+partial): {len(result.conflicts)}")
    print("\nexcluded, not matched:")
    for label in UNSTABLE_CLASSES:
        print(f"  {label:<16} {result.counts.get(label, 0)}")
    print(f"  {'candidate_only':<16} {len(result.candidate_only)}")
    print(f"  {'baseline_only':<16} {result.baseline_only}")
    if result.excluded:
        print("\nrows turned away by status:")
        for label, n in result.excluded.items():
            print(f"  {label:<24} {n}")
    print(f"\nwrote {out}")
    return 0


# ---------------------------------------------------------------------------
# sample
# ---------------------------------------------------------------------------


def _read_conflicts(path: Path) -> List[MatchedItem]:
    """Rebuild :class:`MatchedItem` rows from what ``extract`` wrote."""
    items: List[MatchedItem] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row: Dict[str, Any] = json.loads(line)
        except json.JSONDecodeError as exc:
            raise Refused(f"{path}:{line_no} is not JSON: {exc}") from exc
        items.append(
            MatchedItem(
                entity_hash=row["entity_hash"],
                data_hash=row["data_hash"],
                item_id=row["item_id"],
                candidate_job_id=row["candidate_job_id"],
                baseline_item_id=row["baseline_item_id"],
                baseline_job_id=row["baseline_job_id"],
                candidate_selected=frozenset(row["candidate_selected"]),
                baseline_selected=frozenset(row["baseline_selected"]),
                candidate_answers=row["candidate_answers"],
                baseline_answers=row["baseline_answers"],
                source=row["source"],
                evidence_text=row.get("evidence_text", ""),
                menu=tuple(row.get("menu") or ()),
                kind=row["class"],
                item_sub_kind=row.get("sub_kind"),
                candidate_decided_by=row.get("candidate_decided_by"),
                baseline_decided_by=row.get("baseline_decided_by"),
                candidate_decline_reason=row.get("candidate_decline_reason"),
                baseline_decline_reason=row.get("baseline_decline_reason"),
            )
        )
    return items


def cmd_sample(args: argparse.Namespace) -> int:
    items = _read_conflicts(Path(args.conflicts))
    plan = Plan(
        pilot_size=args.pilot_size,
        swap_slice=args.swap_slice,
        regression_size=args.regression_size,
        decline_guard_size=args.decline_guard_size,
    )
    manifest = build_manifest(items, args.seed, plan)
    written = manifest.write(args.out)
    print(f"manifest {manifest.manifest_id} (seed {args.seed})")
    print(f"  {'pool':<14} {'rows':>6} {'entries':>8}  (entries > rows where a row is swapped)")
    for name, entries in manifest.report["entries_per_pool"].items():
        rows = manifest.report["pools"][name]["rows"]
        print(f"  {name:<14} {rows:>6} {entries:>8}")
    print(f"\nwrote {written}")
    return 0


# ---------------------------------------------------------------------------
# adjudicate
# ---------------------------------------------------------------------------


def _pools(value: Optional[str]) -> Optional[List[str]]:
    """``None`` means every pool the manifest holds; an empty list is an error."""
    if value is None:
        return None
    pools = [p.strip() for p in value.split(",") if p.strip()]
    if not pools:
        raise Refused("--pools was given but named no pool")
    return pools


def cmd_adjudicate(args: argparse.Namespace) -> int:
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    matched = _read_conflicts(Path(args.matched))
    items = build_items(manifest, matched, _pools(args.pools))
    ledger = Ledger(args.run_dir)
    report = run_tranches(
        args.store,
        args.profile,
        items,
        tranche_size=args.tranche_size,
        token_ceiling=args.token_ceiling,
        ledger=ledger,
        manifest_id=str(manifest.get("manifest_id") or ""),
        work=not args.no_work,
        retry_failed=args.retry_failed,
    )

    print(f"manifest {report['manifest_id'] or '(unidentified)'} -> {report['query']}")
    print(f"  invocation  {report['invocation_id']}")
    print(f"  ledger      {ledger.path}")
    print(f"  items       {len(items)} in {report['planned_tranches']} tranche(s)")
    print(f"  roster      {report['profile']} ({report['roster_fingerprint'][:12]}...)")
    print(f"\n  {'#':>3} {'outcome':<24} {'job':<34} {'argo':>9} {'cli':>9}")
    for entry in report["tranches"]:
        usage = entry.get("usage") or {}
        print(
            f"  {entry['index']:>3} {entry['outcome']:<24} "
            f"{str(entry['job_id'] or '-'):<34} "
            f"{usage.get('argo_tokens', 0):>9} {usage.get('unmetered_tokens', 0):>9}"
        )
        if entry.get("detail"):
            print(f"      {entry['detail']}")
    usage = report["usage"]
    print(
        f"\nARGO-billed tokens this invocation: {usage['argo_tokens']} "
        f"(ceiling {report['token_ceiling']}"
        + (", OVERSHOT" if report.get("overshoot") else "")
        + ")"
    )
    print(
        f"CLI (subscription) tokens, reported and NOT metered: "
        f"{usage['unmetered_tokens']}"
    )
    if report["stopped"] is not None:
        stop = report["stopped"]
        print(
            f"\nSTOPPED at tranche {stop['tranche']}: {stop['reason']}\n"
            f"  {stop['detail']}",
            file=sys.stderr,
        )
        return 3
    return 0


# ---------------------------------------------------------------------------
# pilot-table / spot-check / read-marks / calibrate
# ---------------------------------------------------------------------------


def _manifest_of(path: str) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _tranche_records(run_dir: Optional[str], manifest_id: str) -> List[Dict[str, Any]]:
    """The run's ``tranche`` records for *manifest_id*, oldest first."""
    if run_dir is None:
        return []
    return [
        r
        for r in Ledger(run_dir).records("tranche")
        if str(r.get("manifest_id") or "") == str(manifest_id)
    ]


def _job_ids(args: argparse.Namespace, manifest_id: str) -> List[str]:
    """The adjudication jobs to read: ``--jobs``, else the run's ledger.

    Taken from the ledger rather than from the store so the table covers
    exactly the jobs THIS run submitted: a store can hold another run's
    adjudication of the same manifest, and pooling the two would table one
    item twice.
    """
    named = _jobs(args.jobs)
    if named:
        return named
    jobs: List[str] = []
    for record in _tranche_records(args.run_dir, manifest_id):
        job_id = str(record.get("job_id") or "")
        if job_id and job_id not in jobs:
            jobs.append(job_id)
    if not jobs:
        raise Refused(
            "no adjudication jobs to read: pass --jobs, or --run-dir pointing at a "
            f"ledger that recorded a tranche for manifest {manifest_id!r}"
        )
    return jobs


def _bindings(args: argparse.Namespace, manifest_id: str) -> tuple:
    """``(adjudicate_spec_hash, roster_fingerprint)`` for the record (C13).

    Read off the newest tranche record unless given explicitly: the marks
    calibrate the judge AS IT RAN, and a hash typed by hand is a hash that
    can be typed wrong.
    """
    spec = str(args.spec_hash or "")
    roster = str(args.roster_fingerprint or "")
    if not spec or not roster:
        for record in reversed(_tranche_records(args.run_dir, manifest_id)):
            spec = spec or str(record.get("adjudicate_spec_hash") or "")
            roster = roster or str(record.get("roster_fingerprint") or "")
            if spec and roster:
                break
    if not spec or not roster:
        raise Refused(
            "a calibration record must be bound to the adjudicate spec hash and the "
            "resolved roster; pass --run-dir so they can be read off the tranche "
            "records, or give --spec-hash and --roster-fingerprint"
        )
    return spec, roster


def cmd_pilot_table(args: argparse.Namespace) -> int:
    manifest = _manifest_of(args.manifest)
    matched = _read_conflicts(Path(args.matched))
    results = read_results(
        args.store, _job_ids(args, str(manifest.get("manifest_id") or "")), manifest
    )
    out = T.write_pilot(results, manifest, matched, args.xlsx, args.md)
    print(f"manifest {out['manifest_id'] or '(unidentified)'}: {out['rows']} pilot row(s)")
    print(f"  answer sha256 {out['answer_sha256'][:16]}...")
    print(f"  wrote {out['xlsx']}")
    print(f"  wrote {out['md']}")
    print(
        f"\nMark the '{T.MARK_COLUMN}' column R/W/? in the .xlsx, then read it back "
        "with `read-marks` or `calibrate`."
    )
    return 0


def cmd_spot_check(args: argparse.Namespace) -> int:
    manifest = _manifest_of(args.manifest)
    manifest_id = str(manifest.get("manifest_id") or "")
    records = [r for r in _tranche_records(args.run_dir, manifest_id) if r.get("spot_check_ids")]
    if args.tranche is not None:
        records = [r for r in records if int(r.get("tranche_index", -1)) == args.tranche]
    if not records:
        raise Refused(
            "the ledger records no spot-check ids for "
            + (f"tranche {args.tranche} of " if args.tranche is not None else "")
            + f"manifest {manifest_id!r}"
        )
    record = records[-1]
    item_ids = [str(i) for i in record.get("spot_check_ids") or ()]
    matched = _read_conflicts(Path(args.matched))
    results = read_results(args.store, _job_ids(args, manifest_id), manifest)
    out = T.write_spot_check(results, manifest, matched, item_ids, args.xlsx, args.md)
    print(
        f"tranche {record.get('tranche_index')} of manifest "
        f"{manifest_id or '(unidentified)'}: {out['rows']} spot-check row(s)"
    )
    print(f"  token {str(record.get('submission_token') or '')[:16]}...")
    print(f"  wrote {out['xlsx']}")
    print(f"  wrote {out['md']}")
    return 0


def _print_gold(gold: Any) -> None:
    counts = gold.counts
    print(f"manifest {gold.manifest_id or '(unidentified)'} ({gold.scope})")
    print(
        f"  marks       R {counts['R']}  W {counts['W']}  ? {counts['?']}  "
        f"blank {counts['blank']}  of {counts['rows']} row(s)"
    )
    print(f"  gold sets   {counts['gold']} ({counts['excluded']} row(s) supply none)")
    reasons: Dict[str, int] = {}
    for reason in gold.excluded.values():
        reasons[reason] = reasons.get(reason, 0) + 1
    for reason, n in sorted(reasons.items()):
        print(f"    {reason:<20} {n}")


def cmd_read_marks(args: argparse.Namespace) -> int:
    manifest = _manifest_of(args.manifest)
    gold = T.read_marks(args.xlsx, manifest)
    _print_gold(gold)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(gold.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"\nwrote {out}")
    if args.record:
        spec, roster = _bindings(args, gold.manifest_id)
        written = C.record_marks(
            Ledger(args.run_dir),
            gold,
            adjudicate_spec_hash=spec,
            roster_fingerprint=roster,
        )
        print(
            f"recorded calibration seq {written['seq']} band {written['band']} "
            f"({len(written['marks'])} mark(s)) in {Path(args.run_dir) / 'ledger.jsonl'}"
        )
    if gold.unreviewed:
        print(
            f"\n{len(gold.unreviewed)} row(s) are still unmarked; calibrate will "
            "refuse until every row carries R, W or ?",
            file=sys.stderr,
        )
        return 3
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    manifest = _manifest_of(args.manifest)
    manifest_id = str(manifest.get("manifest_id") or "")
    matched = _read_conflicts(Path(args.matched))
    results = read_results(args.store, _job_ids(args, manifest_id), manifest)
    gold = T.read_marks(args.xlsx, manifest)
    spec, roster = _bindings(args, manifest_id)
    calibration = C.report(
        results,
        gold,
        manifest,
        adjudicate_spec_hash=spec,
        roster_fingerprint=roster,
        population=C.population_shares(matched),
    )
    markdown = calibration.to_markdown()
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(markdown, encoding="utf-8")
        print(f"wrote {out}\n")
    print(markdown)
    if args.record:
        written = C.record(Ledger(args.run_dir), calibration)
        print(
            f"recorded calibration seq {written['seq']} band {written['band']} in "
            f"{Path(args.run_dir) / 'ledger.jsonl'}"
        )
    return 0


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------


def _add_table_options(parser: Any, *, marks: bool) -> None:
    """Options shared by the four table/calibration subcommands."""
    parser.add_argument("--manifest", required=True, help="the manifest sample wrote")
    if marks:
        parser.add_argument("--xlsx", required=True, help="the marked workbook to read")
    parser.add_argument(
        "--run-dir",
        default=None,
        help="the run's ledger directory; supplies the job ids, the spot-check ids "
        "and the spec/roster bindings",
    )
    parser.add_argument(
        "--jobs",
        default=None,
        help="comma-separated adjudication job ids; omit to read them off --run-dir",
    )
    parser.add_argument("--spec-hash", default=None, help="override the recorded adjudicate spec hash")
    parser.add_argument(
        "--roster-fingerprint", default=None, help="override the recorded roster fingerprint"
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract = subparsers.add_parser("extract", help="match and classify; write JSONL")
    extract.add_argument("--candidate-store", required=True)
    extract.add_argument("--candidate-jobs", required=True, help="comma-separated job ids")
    extract.add_argument("--baseline-store", required=True)
    extract.add_argument(
        "--baseline-jobs",
        default=None,
        help="comma-separated go_mf@4 job ids; omit for every go_mf@4 job in the store",
    )
    extract.add_argument("--out", required=True, help="JSONL file to write")
    extract.set_defaults(func=cmd_extract)

    sample = subparsers.add_parser("sample", help="draw the pools; write the manifest")
    sample.add_argument("--conflicts", required=True, help="the JSONL that extract wrote")
    sample.add_argument("--seed", required=True, help="any stable string; recorded in the manifest")
    sample.add_argument("--out", required=True, help="manifest JSON file to write")
    sample.add_argument("--pilot-size", type=int, default=Plan.pilot_size)
    sample.add_argument("--swap-slice", type=int, default=Plan.swap_slice)
    sample.add_argument("--regression-size", type=int, default=Plan.regression_size)
    sample.add_argument("--decline-guard-size", type=int, default=Plan.decline_guard_size)
    sample.set_defaults(func=cmd_sample)

    adjudicate = subparsers.add_parser(
        "adjudicate", help="build the blinded items and run them in tranches"
    )
    adjudicate.add_argument("--manifest", required=True, help="the manifest sample wrote")
    adjudicate.add_argument(
        "--matched", required=True, help="the JSONL extract the manifest was sampled from"
    )
    adjudicate.add_argument(
        "--pools",
        default=None,
        help="comma-separated pool names; omit for every pool in the manifest",
    )
    adjudicate.add_argument("--store", required=True, help="the council store to submit into")
    adjudicate.add_argument("--profile", default=None, help="seating profile (the operator's)")
    adjudicate.add_argument("--tranche-size", type=int, default=DEFAULT_TRANCHE_SIZE)
    adjudicate.add_argument(
        "--token-ceiling",
        type=int,
        default=DEFAULT_TOKEN_CEILING,
        help="soft ARGO-billed admission threshold for THIS invocation",
    )
    adjudicate.add_argument(
        "--run-dir", required=True, help="where the run's ledger.jsonl lives"
    )
    adjudicate.add_argument(
        "--retry-failed",
        action="store_true",
        help="resubmit the error/cancelled items of a halted tranche (operator's act)",
    )
    adjudicate.add_argument(
        "--no-work",
        action="store_true",
        help="submit only and leave the calls for a daemon; the caller then owns the "
        "rule that tranches must not overlap",
    )
    adjudicate.set_defaults(func=cmd_adjudicate)

    pilot = subparsers.add_parser(
        "pilot-table", help="write the .xlsx a human marks, plus the .md copy"
    )
    _add_table_options(pilot, marks=False)
    pilot.add_argument("--matched", required=True, help="the JSONL extract the manifest came from")
    pilot.add_argument("--store", required=True, help="the council store the adjudications are in")
    pilot.add_argument("--xlsx", required=True, help="the workbook to write")
    pilot.add_argument("--md", required=True, help="the markdown copy to write")
    pilot.set_defaults(func=cmd_pilot_table)

    spot = subparsers.add_parser(
        "spot-check", help="the same table over one tranche's persisted spot-check ids"
    )
    _add_table_options(spot, marks=False)
    spot.add_argument("--matched", required=True, help="the JSONL extract the manifest came from")
    spot.add_argument("--store", required=True, help="the council store the adjudications are in")
    spot.add_argument("--xlsx", required=True, help="the workbook to write")
    spot.add_argument("--md", required=True, help="the markdown copy to write")
    spot.add_argument(
        "--tranche",
        type=int,
        default=None,
        help="tranche index; omit for the newest tranche that recorded spot-check ids",
    )
    spot.set_defaults(func=cmd_spot_check)

    marks = subparsers.add_parser(
        "read-marks", help="read a marked table back as gold, optionally into the ledger"
    )
    _add_table_options(marks, marks=True)
    marks.add_argument("--out", default=None, help="write the gold as JSON here")
    marks.add_argument(
        "--record",
        action="store_true",
        help="append the marks to the run's ledger as a calibration record "
        "(the spot-check import path); needs --run-dir",
    )
    marks.set_defaults(func=cmd_read_marks)

    calibrate = subparsers.add_parser(
        "calibrate", help="agreement, its slices, the swap flip rate and the band"
    )
    _add_table_options(calibrate, marks=True)
    calibrate.add_argument("--matched", required=True, help="the JSONL extract the manifest came from")
    calibrate.add_argument("--store", required=True, help="the council store the adjudications are in")
    calibrate.add_argument("--out", default=None, help="write the markdown report here")
    calibrate.add_argument(
        "--record",
        action="store_true",
        help="append the report to the run's ledger as a calibration record; needs --run-dir",
    )
    calibrate.set_defaults(func=cmd_calibrate)

    args = parser.parse_args(argv)
    if getattr(args, "record", False) and not args.run_dir:
        parser.error("--record needs --run-dir: a calibration record lives in the run's ledger")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        return int(args.func(args))
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        if exc.ids:
            print("affected ids: " + ", ".join(exc.ids), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
