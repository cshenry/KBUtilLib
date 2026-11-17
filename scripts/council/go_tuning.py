#!/usr/bin/env python3
"""Extract go_mf conflicts from two stores, and sample a tuning manifest.

    python scripts/council/go_tuning.py extract \
        --candidate-store sqlite:////path/bench_a.sqlite --candidate-jobs J1,J2 \
        --baseline-store  sqlite:////path/live.sqlite \
        --out conflicts.jsonl

    python scripts/council/go_tuning.py sample \
        --conflicts conflicts.jsonl --seed 20261117 --out manifest.json

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

Later tasks add subcommands here, so each one is a subparser with its own
``--out``; nothing is shared but the store-opening helper.

EXIT CODES. 0 done; 2 refused (bad, ambiguous or too-small input).
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

from kbutillib.domains.ai.council.go_tuning.conflicts import (  # noqa: E402
    CLASSES,
    UNSTABLE_CLASSES,
    MatchedItem,
    Refused,
    match,
)
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
