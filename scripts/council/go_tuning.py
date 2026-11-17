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

THEN THE TUNING LOOP ITSELF: ``fresh-run`` -> ``score`` -> ``audit``::

    #  one isolated store per run, because the engine's reuse key ignores
    #  the job id and a second run in one store reports the first's answers
    python scripts/council/go_tuning.py fresh-run \
        --spec-store sqlite:////path/loop.sqlite --query go_mf --version 4 \
        --manifest manifest.json --matched conflicts.jsonl \
        --profile go_mf --run-dir /scratch/you/run --label iter1-baseline

    python scripts/council/go_tuning.py score \
        --candidate-store sqlite:////scratch/you/run/stores/iter1-cand.sqlite \
        --candidate-job J_CAND \
        --baseline-store sqlite:////scratch/you/run/stores/iter1-baseline.sqlite \
        --baseline-job J_BASE \
        --gold gold.json --manifest manifest.json --matched conflicts.jsonl \
        --noise-floor 0.03 --run-dir /scratch/you/run

    #  ONCE, for the one candidate proposed for promotion
    python scripts/council/go_tuning.py audit \
        --candidate-store ... --candidate-job ... \
        --baseline-store ... --baseline-job ... \
        --gold pilot-gold.json --manifest manifest.json --run-dir /scratch/you/run

``--run-dir`` must be on LOCAL DISK.  The council store refuses a path with a
Dropbox component and a path on a network filesystem, and ``fresh-run``
reports that refusal rather than quietly putting the store somewhere else --
on h100 that means ``/scratch``, not ``$HOME`` and not the Dropbox tree.

``score`` reads ``heldout`` plus the two guard slices and REFUSES the pilot;
``audit`` reads the pilot and nothing else.  Every ``score``/``audit`` run is
appended to the ledger, and ``score`` evaluates the stop rule afterwards and
records the stop when it fires.

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
from kbutillib.domains.ai.council.go_tuning.run import (  # noqa: E402
    fresh_run,
    go_mf_tasks,
)
from kbutillib.domains.ai.council.go_tuning.sample import (  # noqa: E402
    Plan,
    build_manifest,
)
from kbutillib.domains.ai.council.go_tuning.score import (  # noqa: E402
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
    SCORE_POOLS,
    RunRef,
    audit,
    measure_noise,
    score,
    stop_rule,
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
# fresh-run / score / audit
# ---------------------------------------------------------------------------


def _gold_from(paths: Sequence[str]) -> Dict[str, Any]:
    """Merge one or more gold files into ``item_id -> record``, later wins.

    Each file is what ``read-marks --out`` wrote (a ``Gold.as_dict()``), or a
    bare ``item_id ->`` mapping.  THE MERGE ORDER IS THE CALLER'S POLICY and
    it is the one the PRD states: Chris's gold where he marked, the
    adjudicated answer elsewhere -- so pass the adjudicated file first and
    Chris's second, and the per-record ``provenance`` the reader kept says
    which one each set came from.
    """
    merged: Dict[str, Any] = {}
    for path in paths:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        records = payload.get("records") if isinstance(payload, dict) else None
        if records is None and isinstance(payload, dict):
            merged.update({str(k): v for k, v in payload.items()})
            continue
        for record in records or ():
            merged[str(record["item_id"])] = record
    if not merged:
        raise Refused("the gold file(s) named carry no gold records")
    return merged


def _agreed_from(path: Optional[str]) -> Optional[Dict[str, Any]]:
    """``item_id -> the agreed id set`` for the regression slice.

    Read off the extract: a ``regression`` row is an ``agree_assigned`` key,
    so both sides' selected sets are the same set and either one is "the
    agreed set".  Supplies the BASELINE's own regression drift only; the
    candidate's gated flip rate is against the same iteration's fresh
    baseline (C15) and needs none of this.
    """
    if path is None:
        return None
    out: Dict[str, Any] = {}
    for row in _read_conflicts(Path(path)):
        if row.kind == "agree_assigned":
            out[row.item_id] = sorted(row.baseline_selected)
    return out


def _run_ref(spec: str) -> RunRef:
    """``<store-url>#<job-id>`` -- one run, for the options that take a pair.

    ``#`` and not ``:``: a sqlite store URL is full of colons.
    """
    url, sep, job = str(spec).rpartition("#")
    if not sep or not url.strip() or not job.strip():
        raise Refused(
            f"{spec!r} does not name a run; use '<store-url>#<job-id>', e.g. "
            "'sqlite:////scratch/you/run/stores/iter0-a.sqlite#JOBID'"
        )
    return RunRef(url.strip(), job.strip())


def _band_of(args: argparse.Namespace) -> str:
    """``--band``, else the newest calibration band in the run's ledger.

    Read rather than typed wherever possible: the band gates whether
    judge-derived gold may be scored against at all, and a band typed by
    hand is a band that can be typed wrong in the permissive direction.
    """
    if args.band:
        return str(args.band)
    for record in reversed(Ledger(args.run_dir).records("calibration")):
        band = str(record.get("band") or "")
        if band:
            return band
    raise Refused(
        "no calibration record in the run's ledger carries a band; run "
        "`calibrate --record` (or `read-marks --record`) first, or pass --band"
    )


def _noise_floor(args: argparse.Namespace, manifest: Dict[str, Any]) -> Optional[float]:
    """``--noise-floor``, else measured from the two iteration-0 baselines."""
    runs = [spec for spec in (args.noise_run or ())]
    if args.noise_floor is not None and runs:
        raise Refused("pass either --noise-floor or two --noise-run, not both")
    if args.noise_floor is not None:
        return float(args.noise_floor)
    if not runs:
        return None
    if len(runs) != 2:
        raise Refused(
            f"a noise floor is measured between TWO iteration-0 baselines; "
            f"{len(runs)} --noise-run were given"
        )
    report = measure_noise(_run_ref(runs[0]), _run_ref(runs[1]), manifest)
    print(
        f"noise floor {report['noise_floor']:.4f} "
        f"({report['flipped']}/{report['n']} guard key(s) flipped between the two "
        f"{report['query']} baselines)"
    )
    for name, slice_ in report["per_slice"].items():
        print(f"  {name:<14} {slice_['flipped']:>5}/{slice_['n']:<5}")
    return float(report["noise_floor"])


def cmd_fresh_run(args: argparse.Namespace) -> int:
    manifest = _manifest_of(args.manifest)
    matched = _read_conflicts(Path(args.matched))
    items = go_mf_tasks(manifest, matched, _pools(args.pools))
    report = fresh_run(
        args.spec_store,
        args.query,
        args.version,
        items,
        args.profile,
        args.run_dir,
        ledger=Ledger(args.run_dir),
        label=args.label,
        work=not args.no_work,
    )
    print(f"{report['query']} run {report['label']}")
    print(f"  store   {report['store']}")
    print(f"  job     {report['job_id']}")
    print(f"  items   {report['n_items']}")
    if report["fresh"] is None:
        print("  fresh   not checked (--no-work: nothing has been worked yet)")
        return 3
    print(f"  fresh   yes ({report['reused']} reused call(s))")
    return 0


def _score_options(args: argparse.Namespace, manifest: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "gold": _gold_from(args.gold),
        "manifest": manifest,
        "noise_floor": _noise_floor(args, manifest),
        "ledger": Ledger(args.run_dir),
        "seed": args.seed,
        "resamples": args.resamples,
    }


def _print_score(result: Any) -> None:
    payload = result.as_dict()
    primary = payload["primary"]
    boot = payload["bootstrap"]
    print(
        f"{payload['candidate']['query']} vs {payload['baseline']['query']} "
        f"on pool {payload['pool']} (band {payload['band'] or '-'})"
    )
    print(f"  scored      {primary['n']} item(s), subset {payload['scored']['digest'][:12]}...")
    print(f"  primary     candidate {_pct(primary['candidate'])}  baseline {_pct(primary['baseline'])}")
    print(
        f"  gain        {_pct(primary['gain'])}   bootstrap {int(100 * boot['level'])}% "
        f"[{_pct(boot['lower'])}, {_pct(boot['upper'])}] "
        f"({boot['resamples']} resamples of {boot['groups']} entity group(s), seed {boot['seed']})"
    )
    print(f"  noise floor {_pct(payload['noise_floor'])}")
    decline, regression = payload["decline"], payload["regression"]
    print(
        f"  decline     candidate {_pct(decline['candidate'])}  baseline "
        f"{_pct(decline['baseline'])}  (n={decline['n']})"
    )
    print(
        f"  regression  candidate {_pct(regression['candidate'])} vs this iteration's "
        f"fresh baseline  (n={regression['n']})"
    )
    conflict = payload["secondary"]["conflict_rate"]
    print(f"  conflicts   {_pct(conflict['overall'])} of {conflict['n']} required item(s)")
    for side in ("candidate", "baseline"):
        tokens = payload["secondary"]["tokens"][side]
        print(
            f"  tokens      {side:<9} argo {tokens.get('argo_tokens', '-')}  "
            f"cli {tokens.get('unmetered_tokens', '-')}"
        )
    gain = payload["gain"]
    print(f"\n  GAIN: {'yes' if gain['is_gain'] else 'no'}")
    for name, passed in sorted((gain.get("checks") or {}).items()):
        print(f"    {'pass' if passed else 'FAIL':<5} {name}")
    for reason in gain.get("reasons") or ():
        print(f"    - {reason}")
    incumbent = payload["incumbent"]
    print(
        f"\n  incumbent {incumbent.get('label')} at {_pct(incumbent.get('primary'))}: "
        f"{'REPLACED' if incumbent.get('replaced') else 'kept'}"
    )
    print(f"    {incumbent.get('detail')}")
    heldout = payload["heldout"]
    print(
        f"\n  off-menu  {heldout['off_menu_gold_rows']} of "
        f"{heldout['remaining_failures']} remaining failure(s) = "
        f"{_pct(heldout['off_menu_share'])} (ceiling {_pct(heldout['ceiling'])})"
    )


def _pct(value: Any) -> str:
    return "-" if value is None else f"{100.0 * float(value):.1f}%"


def _write_json(path: Optional[str], payload: Any) -> None:
    if not path:
        return
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")


def cmd_score(args: argparse.Namespace) -> int:
    manifest = _manifest_of(args.manifest)
    options = _score_options(args, manifest)
    incumbent = _run_ref(args.incumbent) if args.incumbent else None
    result = score(
        args.candidate_store,
        args.candidate_job,
        args.baseline_store,
        args.baseline_job,
        options["gold"],
        manifest,
        options["noise_floor"],
        band=_band_of(args),
        pools=_pools(args.pools) or list(SCORE_POOLS),
        agreed=_agreed_from(args.matched),
        incumbent=incumbent,
        incumbent_label=args.incumbent_label,
        ledger=options["ledger"],
        seed=options["seed"],
        resamples=options["resamples"],
    )
    _print_score(result)
    _write_json(args.out, result.as_dict())

    decision = stop_rule(options["ledger"])
    if decision["stop"]:
        print(
            f"\nSTOP ({decision['reason']}): {decision['detail']}\n"
            f"  {decision['candidates']} candidate(s), "
            f"{decision['consecutive_without_improvement']} without improvement, "
            f"incumbent {decision['incumbent']}",
            file=sys.stderr,
        )
    else:
        print(
            f"\ncontinue: {decision['candidates']} candidate(s), "
            f"{decision['consecutive_without_improvement']} without improvement, "
            f"incumbent {decision['incumbent']}"
        )
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    manifest = _manifest_of(args.manifest)
    options = _score_options(args, manifest)
    result = audit(
        RunRef(args.candidate_store, args.candidate_job),
        RunRef(args.baseline_store, args.baseline_job),
        options["gold"],
        manifest,
        noise_floor=options["noise_floor"],
        band=args.band or "",
        ledger=options["ledger"],
        seed=options["seed"],
        resamples=options["resamples"],
    )
    _print_score(result)
    _write_json(args.out, result.as_dict())
    print(
        "\nThe pilot is a LOCKED test set. This audit is the one read of it for "
        "this candidate; do not tune against what it says."
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

    fresh = subparsers.add_parser(
        "fresh-run",
        help="run a query over the manifest in an ISOLATED store, and prove it was fresh",
    )
    fresh.add_argument(
        "--spec-store",
        required=True,
        help="the loop store holding the spec to run; it is COPIED into the new store",
    )
    fresh.add_argument("--query", required=True, help="query type name, e.g. go_mf")
    fresh.add_argument("--version", required=True, help="its version, e.g. 4 for the baseline")
    fresh.add_argument("--manifest", required=True, help="the manifest sample wrote")
    fresh.add_argument("--matched", required=True, help="the JSONL extract the manifest came from")
    fresh.add_argument(
        "--pools",
        default=None,
        help="comma-separated pool names; omit for every pool, which is what a "
        "scored iteration needs (one job over one frozen item set)",
    )
    fresh.add_argument("--profile", default=None, help="seating profile (the operator's)")
    fresh.add_argument(
        "--run-dir",
        required=True,
        help="the run directory, ON LOCAL DISK. The store is created at "
        "<run-dir>/stores/<label>.sqlite and is never relocated for you",
    )
    fresh.add_argument(
        "--label",
        default=None,
        help="names the run and its store file (e.g. iter1-baseline); a random "
        "one is used when omitted, which is unique but unrepeatable",
    )
    fresh.add_argument(
        "--no-work",
        action="store_true",
        help="submit only; nothing is worked, so freshness is NOT checked (exit 3)",
    )
    fresh.set_defaults(func=cmd_fresh_run)

    for name, handler, help_text in (
        (
            "score",
            cmd_score,
            "score a candidate against a fresh go_mf@4 on the HELDOUT pool",
        ),
        (
            "audit",
            cmd_audit,
            "the one read of the LOCKED pilot, for the candidate proposed for promotion",
        ),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("--candidate-store", required=True)
        sub.add_argument("--candidate-job", required=True)
        sub.add_argument("--baseline-store", required=True, help="the FRESH go_mf@4 run's store")
        sub.add_argument("--baseline-job", required=True)
        sub.add_argument(
            "--gold",
            action="append",
            required=True,
            metavar="JSON",
            help="gold written by `read-marks --out`; repeatable and LATER WINS, so "
            "pass the adjudicated file first and Chris's second",
        )
        sub.add_argument("--manifest", required=True, help="the manifest sample wrote")
        sub.add_argument(
            "--run-dir", required=True, help="the run's ledger directory; the score is recorded"
        )
        sub.add_argument(
            "--noise-floor",
            type=float,
            default=None,
            help="the floor measure_noise reported; mutually exclusive with --noise-run",
        )
        sub.add_argument(
            "--noise-run",
            action="append",
            default=None,
            metavar="STORE#JOB",
            help="give TWICE to measure the floor here from the two iteration-0 "
            "baselines, e.g. 'sqlite:////scratch/run/stores/iter0-a.sqlite#JOB'",
        )
        sub.add_argument(
            "--band",
            default=None,
            help="the calibration band; read off the run's newest calibration record "
            "when omitted. `not_trusted` refuses judge-derived gold",
        )
        sub.add_argument("--out", default=None, help="write the score as JSON here")
        sub.add_argument("--seed", default=BOOTSTRAP_SEED, help="paired-bootstrap seed")
        sub.add_argument("--resamples", type=int, default=BOOTSTRAP_RESAMPLES)
        if name == "score":
            sub.add_argument(
                "--pools",
                default=None,
                help=f"comma-separated pools; defaults to {', '.join(SCORE_POOLS)}. "
                "'pilot' is REFUSED -- it is a locked test set",
            )
            sub.add_argument(
                "--matched",
                default=None,
                help="the JSONL extract; supplies the agreed sets, which is the only "
                "way the fresh baseline's OWN regression drift can be reported",
            )
            sub.add_argument(
                "--incumbent",
                default=None,
                metavar="STORE#JOB",
                help="the incumbent's run when it is no longer go_mf@4; re-measured "
                "over this score's own labelled subset (C19)",
            )
            sub.add_argument(
                "--incumbent-label",
                default=None,
                help="how to name the incumbent in the record; its query type by default",
            )
        sub.set_defaults(func=handler)

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
