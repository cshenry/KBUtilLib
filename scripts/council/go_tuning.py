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

Later tasks add subcommands here, so each one is a subparser with its own
output; nothing is shared but the store-opening helper.

EXIT CODES. 0 done; 2 refused (bad, ambiguous or too-small input); 3 the
adjudication run stopped at a gate with work still planned (the token ceiling
or the spot-check gate) -- not a failure, but not finished either, and a
caller that treated it as success would report a partial corpus as complete.
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

from kbutillib.domains.ai.council.go_tuning.adjudicate import (  # noqa: E402
    DEFAULT_TOKEN_CEILING,
    DEFAULT_TRANCHE_SIZE,
    build_items,
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
# cli
# ---------------------------------------------------------------------------


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

    return parser.parse_args(argv)


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
