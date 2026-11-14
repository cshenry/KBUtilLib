#!/usr/bin/env python3
"""Compare a candidate council job against NAMED baseline go_mf@4 jobs.

    python scripts/council/alcf_bench_report.py \
        --candidate-store sqlite:////path/candidate.sqlite --candidate-job JOB \
        --baseline-store  sqlite:////path/live.sqlite --baseline-jobs JOB1,JOB2 \
        --worksheet disagreements.md

THE PRIMARY OUTPUT IS THE DISAGREEMENT WORKSHEET, for a domain expert to
adjudicate. Agreement with the baseline is a DIAGNOSTIC, NOT THE SCORE: the
baseline is a frontier bench, not ground truth (on the first three contested
items of the 500-item Bench A run the open-model bench was right twice), so
scoring by agreement would penalise a more accurate council. 100% agreement
would mean the candidate reproduced the baseline's mistakes too. The agreement
figures say how much adjudication is owed. This script prints NO pass/fail
verdict.

Only explicitly named baseline jobs are eligible, and a baseline row is
eligible only when it is go_mf@4 with an ok verdict and EQUAL item_id,
entity_hash and data_hash to the candidate item. A duplicate eligible match,
or any missing or mismatched input, REFUSES (exit 2, listing the ids). No
winner is ever chosen implicitly (no latest-wins, first-wins or retry-child
preference).

All computation lives in importable functions over plain dicts; only
``load_job`` and ``main`` touch a store.

EXIT CODES. 0 report produced; 2 refused (bad or ambiguous input).
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:  # run from a checkout without install
    sys.path.insert(0, str(REPO_ROOT / "src"))

BASELINE_QUERY = ("go_mf", "4")

#: Historical population figures: CONTEXT ONLY, never the comparison.
POPULATION_CONTEXT = {
    "source": "live store, all 48,437 go_mf@4 items, status ok",
    "decline": 48.9,
    "exactly_1": 45.3,
    "exactly_2": 5.1,
    "confidence": {"high": 82.4, "medium": 15.5, "low": 2.1},
    "decided_by": {"unanimous": 71.8, "meta": 28.2},
}

CONFIDENCE_KEYS = ("high", "medium", "low", "unknown")
DECIDED_BY_KEYS = ("unanimous", "meta", "deterministic_empty", "unknown")
SIZE_KEYS = ("decline", "exactly_1", "exactly_2", "3_or_more")

GROUPS = (
    ("candidate-declined-baseline-picked", "candidate declined / baseline picked"),
    ("candidate-picked-baseline-declined", "candidate picked / baseline declined"),
    ("both-picked-differ", "both picked and differ"),
)

WARNING_SURVIVORSHIP = (
    "WARNING (survivorship): a per-seat ballot error rate read from verdicts "
    "that ended ok is survivorship-filtered. Never quote it as an error rate."
)
WARNING_TWO_VARIABLES = (
    "WARNING (two variables): an ALCF bench differs from the baseline in model "
    "family AND in requesting response_format, so agreement measures whether "
    "the councils agree, not what caused a disagreement."
)
WARNING_RESOLUTION = (
    "WARNING (resolution): the 95% interval applies to ONE proportion under a "
    "simple sampling assumption (Wilson, matched n). It does NOT apply to "
    "exact-match agreement or to a paired decline difference."
)
WARNING_DIRECTION = (
    "NOTE (direction): each delta below is stated with its direction only. A "
    "lower or higher decline rate is a difference to be adjudicated, not a "
    "finding about which council is better."
)
NO_ATTEMPT_CAVEAT = (
    "Halted, parked and in-flight requests can lack attempt rows (_park writes "
    "none), so the inventory below is of RECORDED COMPLETED NON-REUSE attempts "
    "only; a call with zero rows is 'no recorded attempt', not 'never attempted'."
)

NO_VERDICT_STATEMENT = (
    "This report prints no pass/fail verdict. Agreement with the baseline is a "
    "diagnostic, not the score: the baseline is not ground truth, and the "
    "worksheet of disagreements is what a domain expert adjudicates."
)


class Refused(Exception):
    """The inputs are missing, mismatched or ambiguous; ``ids`` lists them."""

    def __init__(self, message: str, ids: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.ids = list(ids)


# ---------------------------------------------------------------------------
# verdict helpers
# ---------------------------------------------------------------------------


def selected_set(verdict: Optional[Mapping[str, Any]]) -> frozenset:
    return frozenset((verdict or {}).get("selected_ids") or [])


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    """Jaccard similarity with Jaccard(empty, empty) = 1."""
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def _pct(num: float, den: float) -> Optional[float]:
    return None if not den else 100.0 * num / den


def _fmt_pct(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value:.1f}%"


def _verdict_ok(item: Mapping[str, Any]) -> bool:
    verdict = item.get("verdict") or {}
    return item.get("status") == "ok" and verdict.get("status", "ok") == "ok"


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------


def match_items(
    candidate_items: Sequence[Mapping[str, Any]],
    baseline_jobs: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Pair candidate items with baseline rows; REFUSE rather than choose.

    ``baseline_jobs`` are ``{"job_id", "query_name", "query_version",
    "items": [...]}`` dicts, one per explicitly named job.

    Returns ``{"pairs": {item_id: (cand_item, base_item)}, "unmatched":
    {item_id: reason}}``. Raises :class:`Refused` on a baseline job that is
    not go_mf@4, a duplicate eligible match, or an item id present in the
    baseline whose entity_hash/data_hash differ from the candidate's.
    """
    wrong = [
        j["job_id"] for j in baseline_jobs
        if (j.get("query_name"), str(j.get("query_version"))) != BASELINE_QUERY
    ]
    if wrong:
        raise Refused("baseline job(s) are not go_mf@4", wrong)

    by_id: Dict[str, List[Tuple[str, Mapping[str, Any]]]] = defaultdict(list)
    for job in baseline_jobs:
        for row in job.get("items", []):
            by_id[row["item_id"]].append((job["job_id"], row))

    pairs: Dict[str, Tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
    unmatched: Dict[str, str] = {}
    duplicates: List[str] = []
    mismatched: List[str] = []
    for cand in candidate_items:
        item_id = cand["item_id"]
        rows = by_id.get(item_id, [])
        same_hash = [
            (jid, r) for jid, r in rows
            if r.get("entity_hash") == cand.get("entity_hash")
            and r.get("data_hash") == cand.get("data_hash")
        ]
        if rows and not same_hash:
            mismatched.append(item_id)
            continue
        eligible = [(jid, r) for jid, r in same_hash if _verdict_ok(r)]
        if len(eligible) > 1:
            duplicates.append(item_id)
        elif len(eligible) == 1:
            pairs[item_id] = (cand, eligible[0][1])
        elif same_hash:
            unmatched[item_id] = "baseline row has no ok verdict"
        else:
            unmatched[item_id] = "no baseline row in the named jobs"
    if duplicates:
        raise Refused(
            "duplicate eligible baseline match (refusing to pick a winner)", duplicates
        )
    if mismatched:
        raise Refused(
            "baseline row exists but entity_hash/data_hash differ from the candidate",
            mismatched,
        )
    return {"pairs": pairs, "unmatched": unmatched}


# ---------------------------------------------------------------------------
# coverage / agreement / profile
# ---------------------------------------------------------------------------


def coverage(
    candidate_items: Sequence[Mapping[str, Any]], matched: Mapping[str, Any]
) -> Dict[str, int]:
    """Separate counts; they are never merged into one number."""
    statuses = Counter(i.get("status") for i in candidate_items)
    failed = statuses.get("error", 0)
    ok = statuses.get("ok", 0)
    cancelled = statuses.get("cancelled", 0)
    return {
        "submitted": len(candidate_items),
        "successful": ok,
        "failed": failed,
        "cancelled": cancelled,
        "pending": len(candidate_items) - ok - failed - cancelled,
        "matched": len(matched),
    }


def agreement(pairs: Mapping[str, Tuple[Mapping[str, Any], Mapping[str, Any]]]) -> Dict[str, Any]:
    """Admit only candidate-ok items that have a matched ok baseline verdict."""
    n = exact = both_declined = 0
    total_j = 0.0
    for cand, base in pairs.values():
        if not (_verdict_ok(cand) and _verdict_ok(base)):
            continue
        a, b = selected_set(cand["verdict"]), selected_set(base["verdict"])
        n += 1
        total_j += jaccard(a, b)
        if a == b:
            exact += 1
            if not a:
                both_declined += 1
    excl_n = n - both_declined
    excl_exact = exact - both_declined
    return {
        "n": n,
        "exact": exact,
        "exact_rate": _pct(exact, n),
        "mean_jaccard": (total_j / n) if n else None,
        "both_declined": both_declined,
        "exact_excluding_both_declined": excl_exact,
        "n_excluding_both_declined": excl_n,
        "exact_rate_excluding_both_declined": _pct(excl_exact, excl_n),
    }


def _size_bin(verdict: Mapping[str, Any]) -> str:
    n = len(selected_set(verdict))
    return "decline" if n == 0 else "exactly_1" if n == 1 else "exactly_2" if n == 2 else "3_or_more"


def _bucket(value: Any, allowed: Sequence[str]) -> str:
    return value if value in allowed and value != "unknown" else "unknown"


def _count(values: Iterable[str], keys: Sequence[str]) -> Dict[str, int]:
    counts = Counter(values)
    return {k: counts.get(k, 0) for k in keys}


def _distribution(verdicts: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, int]]:
    return {
        "size": _count((_size_bin(v) for v in verdicts), SIZE_KEYS),
        "confidence": _count(
            (_bucket(v.get("final_confidence"), CONFIDENCE_KEYS) for v in verdicts), CONFIDENCE_KEYS
        ),
        "decided_by": _count(
            (_bucket(v.get("decided_by"), DECIDED_BY_KEYS) for v in verdicts), DECIDED_BY_KEYS
        ),
    }


def wilson_interval(successes: int, n: int, z: float = 1.96) -> Optional[Tuple[float, float]]:
    """95% Wilson interval for ONE proportion at the actual n; None at n=0."""
    if n <= 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (100.0 * max(0.0, centre - half), 100.0 * min(1.0, centre + half))


def profile(
    pairs: Mapping[str, Tuple[Mapping[str, Any], Mapping[str, Any]]],
    candidate_proposers: Optional[int],
    baseline_proposers: Optional[int],
) -> Dict[str, Any]:
    """Candidate vs matched baseline on ONE denominator: matched, both-ok items."""
    admitted = [(c, b) for c, b in pairs.values() if _verdict_ok(c) and _verdict_ok(b)]
    n = len(admitted)
    cand = _distribution([c["verdict"] for c, _ in admitted])
    base = _distribution([b["verdict"] for _, b in admitted])

    def table(section: str) -> Dict[str, Dict[str, Optional[float]]]:
        out = {}
        for key in cand[section]:
            cp, bp = _pct(cand[section][key], n), _pct(base[section][key], n)
            out[key] = {
                "candidate_n": cand[section][key], "baseline_n": base[section][key],
                "candidate_pct": cp, "baseline_pct": bp,
                "delta_pts": None if cp is None or bp is None else cp - bp,
            }
        return out

    suppressed = None
    if candidate_proposers is None or baseline_proposers is None:
        suppressed = "proposer count unknown for one of the jobs"
    elif candidate_proposers != baseline_proposers:
        suppressed = (
            f"candidate has {candidate_proposers} proposer(s) and the baseline "
            f"{baseline_proposers}; unanimity and meta-escalation rates depend "
            "on how many voters there are, so the decided_by difference is not "
            "like-for-like"
        )
    cand_declines = cand["size"]["decline"]
    base_declines = base["size"]["decline"]
    return {
        "n": n,
        "size": table("size"),
        "confidence": table("confidence"),
        "decided_by": table("decided_by"),
        "decided_by_deltas_suppressed": suppressed,
        "decline_interval_candidate": wilson_interval(cand_declines, n),
        "decline_interval_baseline": wilson_interval(base_declines, n),
    }


# ---------------------------------------------------------------------------
# worksheet
# ---------------------------------------------------------------------------


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _annotation(item: Mapping[str, Any]) -> str:
    evidence = item.get("evidence") or {}
    text = evidence.get("text") if isinstance(evidence, Mapping) else None
    return text if text else item["item_id"]


def disagreements(
    pairs: Mapping[str, Tuple[Mapping[str, Any], Mapping[str, Any]]]
) -> Dict[str, List[Dict[str, Any]]]:
    """Group disagreeing (candidate ok, baseline ok) items into the three groups."""
    out: Dict[str, List[Dict[str, Any]]] = {g: [] for g, _ in GROUPS}
    for item_id, (cand, base) in pairs.items():
        if not (_verdict_ok(cand) and _verdict_ok(base)):
            continue
        a, b = selected_set(cand["verdict"]), selected_set(base["verdict"])
        if a == b:
            continue
        group = (
            "candidate-declined-baseline-picked" if not a
            else "candidate-picked-baseline-declined" if not b
            else "both-picked-differ"
        )
        out[group].append({
            "item_id": item_id,
            "annotation": _annotation(cand),
            "baseline": sorted(b),
            "candidate": sorted(a),
        })
    for rows in out.values():
        rows.sort(key=lambda r: r["item_id"])
    return out


def render_worksheet(groups: Mapping[str, List[Mapping[str, Any]]], n_compared: int) -> str:
    total = sum(len(v) for v in groups.values())
    lines = [
        "# Candidate vs baseline go_mf@4 -- disagreements, for adjudication",
        "",
        f"{n_compared} comparable items; {total} differ and are listed below.",
        "",
        "**The baseline column is NOT ground truth.** It is a frontier bench; on the "
        "first contested items of the 500-item Bench A run the open-model bench was "
        "right more often than not. These items are listed for adjudication, not "
        "scored as candidate errors.",
        "",
        "Mark each row in the leading column: R (candidate right), W (candidate "
        "wrong), or ? (unclear / both defensible).",
        "",
    ]
    for key, _title in GROUPS:
        rows = groups.get(key, [])
        lines += [
            f"## {key}  ({len(rows)} items)",
            "",
            "| | annotation string | baseline picked | candidate picked |",
            "|---|---|---|---|",
        ]
        for r in rows:
            base = ", ".join(r["baseline"]) or "(declined)"
            cand = ", ".join(r["candidate"]) or "(declined)"
            lines.append(f"|  | `{_cell(r['annotation'])}` | {base} | {cand} |")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# failure inventory (candidate job only)
# ---------------------------------------------------------------------------


def failure_inventory(
    calls: Sequence[Mapping[str, Any]], attempts: Sequence[Mapping[str, Any]]
) -> Dict[str, Any]:
    """RECORDED COMPLETED NON-REUSE attempt rows, by outcome_kind per seat.

    'ok' is success, every other recorded non-reuse outcome is failure,
    'reused' is excluded from numerator AND denominator and counted apart.
    Only the candidate job's own calls are read; retry parents/children are
    not traversed.
    """
    seat_of = {c["call_id"]: c.get("seat_name") for c in calls}
    seats = sorted({s for s in seat_of.values() if s is not None})
    per_seat: Dict[str, Dict[str, Any]] = {
        s: {"kinds": Counter(), "ok": 0, "failure": 0, "reused": 0, "calls": 0,
            "no_recorded_attempt": 0}
        for s in seats
    }
    with_rows: Set[str] = set()
    for a in attempts:
        cid = a["call_id"]
        if cid not in seat_of:
            continue
        with_rows.add(cid)
        if a.get("finished_at") is None or not a.get("outcome_kind"):
            continue  # not a completed attempt
        slot = per_seat[seat_of[cid]]
        kind = a["outcome_kind"]
        if kind == "reused":
            slot["reused"] += 1
            continue
        slot["kinds"][kind] += 1
        slot["ok" if kind == "ok" else "failure"] += 1
    for c in calls:
        slot = per_seat[c["seat_name"]]
        slot["calls"] += 1
        if c["call_id"] not in with_rows:
            slot["no_recorded_attempt"] += 1
    for slot in per_seat.values():
        total = slot["ok"] + slot["failure"]
        slot["attempts"] = total
        slot["failure_rate"] = _pct(slot["failure"], total)

    errors = Counter()
    for c in calls:
        err = c.get("error")
        if isinstance(err, Mapping) and err.get("kind"):
            errors[err["kind"]] += 1
        elif err and c.get("status") != "ok":
            errors["unrecorded_kind"] += 1

    items_with_calls = {c["item_id"] for c in calls}
    meta_items = {c["item_id"] for c in calls if c.get("role") == "meta"}
    return {
        "per_seat": per_seat,
        "call_final_errors": dict(errors),
        "meta_invoked_items": len(meta_items),
        "items_with_calls": len(items_with_calls),
        "meta_invocation_rate": _pct(len(meta_items), len(items_with_calls)),
    }


# ---------------------------------------------------------------------------
# text report
# ---------------------------------------------------------------------------


def _delta(d: Optional[float]) -> str:
    if d is None:
        return "N/A"
    word = "higher" if d > 0 else "lower" if d < 0 else "equal"
    return f"{d:+.1f} pts ({word} in candidate)"


def render_summary(
    cov: Mapping[str, int],
    agr: Mapping[str, Any],
    prof: Mapping[str, Any],
    groups: Mapping[str, Sequence[Any]],
    inventory: Mapping[str, Any],
    halts: Optional[Mapping[str, Any]] = None,
    worksheet_path: Optional[str] = None,
) -> str:
    out: List[str] = []
    out.append("== DISAGREEMENT WORKSHEET (primary output) ==")
    if worksheet_path:
        out.append(f"written to {worksheet_path}")
    for key, title in GROUPS:
        out.append(f"  {title}: {len(groups.get(key, []))}")
    out.append("  adjudicate these; the baseline is not ground truth.")
    out.append(NO_VERDICT_STATEMENT)
    out.append("")
    out.append("== COVERAGE (separate counts) ==")
    for k in ("submitted", "successful", "failed", "pending", "matched"):
        out.append(f"  {k}: {cov[k]}")
    if cov.get("cancelled"):
        out.append(f"  cancelled: {cov['cancelled']}")
    out.append("")
    out.append("== AGREEMENT (context, diagnostic only -- not the score) ==")
    out.append(WARNING_TWO_VARIABLES)
    out.append(f"  admitted (candidate ok AND matched ok baseline): {agr['n']}")
    out.append(f"  exact match on selected_ids (as a set): {agr['exact']} ({_fmt_pct(agr['exact_rate'])})")
    mj = agr["mean_jaccard"]
    out.append(f"  mean Jaccard (empty vs empty = 1): {'N/A' if mj is None else f'{mj:.3f}'}")
    out.append(
        f"  exact match excluding both-declined: {agr['exact_excluding_both_declined']}"
        f" of {agr['n_excluding_both_declined']} "
        f"({_fmt_pct(agr['exact_rate_excluding_both_declined'])})"
    )
    out.append("  A failed or missing candidate verdict is not an empty set and is not admitted.")
    out.append("")
    out.append(f"== PROFILE (candidate vs matched baseline; ONE denominator: n={prof['n']} matched items) ==")
    out.append(WARNING_DIRECTION)
    for section, label in (("size", "selected terms"), ("confidence", "confidence")):
        out.append(f"  {label}:")
        for key, r in prof[section].items():
            out.append(
                f"    {key}: candidate {_fmt_pct(r['candidate_pct'])} vs baseline "
                f"{_fmt_pct(r['baseline_pct'])}; delta {_delta(r['delta_pts'])}"
            )
    out.append("  decided_by:")
    sup = prof["decided_by_deltas_suppressed"]
    for key, r in prof["decided_by"].items():
        base = f"candidate {_fmt_pct(r['candidate_pct'])} vs baseline {_fmt_pct(r['baseline_pct'])}"
        out.append(f"    {key}: {base}" + ("" if sup else f"; delta {_delta(r['delta_pts'])}"))
    if sup:
        out.append(f"    decided_by deltas SUPPRESSED: {sup}.")
    out.append(WARNING_RESOLUTION)
    for who, key in (("candidate", "decline_interval_candidate"), ("baseline", "decline_interval_baseline")):
        iv = prof[key]
        text = "N/A (n=0)" if iv is None else f"{iv[0]:.1f}% to {iv[1]:.1f}%"
        out.append(f"  95% interval on the {who} decline proportion at n={prof['n']}: {text} (one proportion)")
    out.append("")
    pop = POPULATION_CONTEXT
    out.append(f"== HISTORICAL POPULATION (context only, NOT the comparison; {pop['source']}) ==")
    out.append(f"  decline {pop['decline']}%, exactly-1 {pop['exactly_1']}%, 2 terms {pop['exactly_2']}%")
    out.append("  confidence high/medium/low {high}/{medium}/{low}".format(**pop["confidence"]))
    out.append("  decided_by unanimous/meta {unanimous}/{meta}".format(**pop["decided_by"]))
    out.append("")
    out.append("== FAILURE INVENTORY (candidate job only) ==")
    out.append(WARNING_SURVIVORSHIP)
    out.append(NO_ATTEMPT_CAVEAT)
    out.append("  'reused' attempts are excluded from numerator and denominator and shown apart.")
    for seat, s in inventory["per_seat"].items():
        rate = "N/A" if s["attempts"] == 0 else f"{s['failure']}/{s['attempts']} = {_fmt_pct(s['failure_rate'])}"
        kinds = ", ".join(f"{k}={v}" for k, v in sorted(s["kinds"].items())) or "none"
        out.append(f"  seat {seat}: recorded non-reuse attempts {s['attempts']} "
                   f"(ok {s['ok']}, failure {s['failure']}); failure rate {rate}; by outcome_kind: {kinds}")
        out.append(f"    reused (excluded): {s['reused']}; calls {s['calls']}; "
                   f"calls with no recorded attempt: {s['no_recorded_attempt']}")
    mr = inventory["meta_invocation_rate"]
    out.append(
        f"  meta invocations: {inventory['meta_invoked_items']} of "
        f"{inventory['items_with_calls']} items with calls = {_fmt_pct(mr)} (a rate)"
    )
    errs = inventory["call_final_errors"]
    out.append("  calls' own final errors (unquantified diagnostic, not a rate): "
               + (", ".join(f"{k}={v}" for k, v in sorted(errs.items())) or "none recorded"))
    halts = halts or {}
    out.append("  halt reasons (unquantified diagnostic): "
               + ("; ".join(f"{e}: {h}" for e, h in sorted(halts.items())) or "none recorded"))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# store I/O
# ---------------------------------------------------------------------------


def load_job(store_url: str, job_id: str, *, with_calls: bool = False) -> Dict[str, Any]:
    """Read one job's row, items (and optionally calls, attempts, halts)."""
    import sqlalchemy as sa

    from kbutillib.domains.ai.council.store import schema as sch
    from kbutillib.domains.ai.council.store.sqlite import SqliteStore

    store = SqliteStore(store_url, create_parents=False)
    try:
        job = store.get_job(job_id)
        if job is None:
            raise Refused(f"job not found in {store_url}", [job_id])
        data: Dict[str, Any] = {
            "job_id": job_id,
            "query_name": job["query_name"],
            "query_version": job["query_version"],
            "items": store.get_items(job_id),
        }
        calls = store.get_calls(job_id)
        data["n_proposers"] = len({c["seat_name"] for c in calls if c["role"] == "proposer"}) or None
        if with_calls:
            data["calls"] = calls
            ids = [c["call_id"] for c in calls]
            attempts: List[Dict[str, Any]] = []
            with store.engine.connect() as conn:
                for i in range(0, len(ids), 500):
                    rows = conn.execute(
                        sa.select(sch.attempts).where(sch.attempts.c.call_id.in_(ids[i:i + 500]))
                    )
                    attempts += [dict(r._mapping) for r in rows]
            data["attempts"] = attempts
            halts = {}
            for ep in sorted({c["endpoint"] for c in calls}):
                st = store.get_endpoint_state(ep)
                if st.get("halt_kind"):
                    halts[ep] = f"{st['halt_kind']}: {st.get('halt_message')}"
            data["halts"] = halts
        return data
    finally:
        store.close()


def build_report(
    candidate: Mapping[str, Any], baselines: Sequence[Mapping[str, Any]]
) -> Dict[str, Any]:
    """All computation, from plain dicts. Raises :class:`Refused`."""
    matching = match_items(candidate["items"], baselines)
    pairs = matching["pairs"]
    groups = disagreements(pairs)
    props = {b.get("n_proposers") for b in baselines}
    baseline_proposers = props.pop() if len(props) == 1 else None
    return {
        "coverage": coverage(candidate["items"], pairs),
        "agreement": agreement(pairs),
        "profile": profile(pairs, candidate.get("n_proposers"), baseline_proposers),
        "groups": groups,
        "inventory": failure_inventory(candidate.get("calls", []), candidate.get("attempts", [])),
        "halts": candidate.get("halts", {}),
        "unmatched": matching["unmatched"],
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--candidate-store", required=True)
    p.add_argument("--candidate-job", required=True)
    p.add_argument("--baseline-store", required=True)
    p.add_argument("--baseline-jobs", required=True, help="comma-separated explicit go_mf@4 job ids")
    p.add_argument("--worksheet", required=True, help="markdown file to write the worksheet to")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    job_ids = [j.strip() for j in args.baseline_jobs.split(",") if j.strip()]
    try:
        if not job_ids:
            raise Refused("--baseline-jobs named no job")
        candidate = load_job(args.candidate_store, args.candidate_job, with_calls=True)
        baselines = [load_job(args.baseline_store, j) for j in job_ids]
        report = build_report(candidate, baselines)
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        if exc.ids:
            print("affected ids: " + ", ".join(exc.ids), file=sys.stderr)
        return 2
    sheet = render_worksheet(report["groups"], report["agreement"]["n"])
    Path(args.worksheet).write_text(sheet + "\n", encoding="utf-8")
    print(render_summary(report["coverage"], report["agreement"], report["profile"],
                         report["groups"], report["inventory"], report["halts"], args.worksheet))
    if report["unmatched"]:
        print(f"\n{len(report['unmatched'])} candidate item(s) had no eligible baseline row "
              "and are excluded from agreement and profile.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
