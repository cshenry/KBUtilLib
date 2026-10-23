"""The standing pipeline's status verb: coverage by LIFECYCLE and by PARTITION.

:func:`council_status` is the verb behind ``kbu ontomap council-status``.  It
answers two questions that a single coverage number cannot, and it is separate
from the five stages because it is the only verb that reads ACROSS runs.

**WHY ONE NUMBER IS THE WRONG ANSWER.**  A corpus under a versioned transform
is ALWAYS partly stale, and the two ways it is partly stale are unrelated:

1. **BY LIFECYCLE.**  Work can be planned, quoted, approved, running or
   published without having LANDED.  Those middle-late states are the ones
   that go missing: money has been SPENT and no row is in the clearinghouse,
   so the work is invisible both to a "what is un-run" count (the plan took
   it out of the un-run set) and to a "what is loaded" count (nothing landed).
   That is exactly what a crashed run leaves behind, and a status verb that
   reported only the two ends would describe a 9-hour 199M-token run as
   nothing at all.
2. **BY** ``parameter_set_hash``.  A bench, query-version or ``mapping_hash``
   bump re-partitions the whole corpus, and re-running all 47,341 functions
   on every bump is unaffordable.  So partial-version coverage is TOLERATED
   -- and therefore it must be REPORTED.  "Is this dataset consistent?" is
   unanswerable otherwise: 19,268 current rows and 28,073 rows at an older
   parameter set reads as 47,341 "covered" functions under any single count.

**THE SEVEN STATES ARE MADE A PARTITION HERE.**  The PRD names ``un-run``,
``planned-not-quoted``, ``quoted-not-approved``, ``approved-not-run``,
``running``, ``exported-not-loaded`` and ``loaded``.  Taken literally those
seven leave a hole -- a run whose chunks and retry pass have SETTLED but whose
verdicts have not been published yet is in none of them -- and a lifecycle
report whose buckets do not add up to the corpus is not a report.  So
:data:`LIFECYCLE_STATES` folds "ran, not published" into ``running``:
``running`` means **RUN.json exists and PUBLISH.json does not**, i.e. started
and not yet handed off.  The per-run rows carry ``complete``, ``halt`` and
``published`` so an operator still sees which of the three it is; what is NOT
available is a bucket that quietly holds nothing.

**THE STATE IS READ OFF THE FILES, NOT OFF A STATUS FIELD.**  Each stage
already writes exactly one artifact into the synced run directory --
``PLAN.json``, ``QUOTE.json``, ``APPROVED.json``, ``RUN.json``,
``PUBLISH.json``, and the consumer's ``LOADED.json`` receipt -- so the
lifecycle is a function of which of those six exist.  A separate
``STATUS.json`` would be a seventh file that could disagree with the six, and
the one an operator trusted would be whichever this verb happened to read.

**``loaded`` COMES FROM THE CLEARINGHOUSE AND NOTHING ELSE.**  A receipt says
a load claimed to land; the current-state table says what is there.  Those
differ in precisely the case worth catching -- the Iceberg commit and the
``LOADED.json`` write are two unrelated writes with no transaction between
them, so a crash in the gap leaves the rows landed and the receipt missing.
This verb reports such a run as ``exported-not-loaded`` by its FILES while
also reporting the rows its parameter set already has in the clearinghouse,
and says that the fix is to re-export the receipt (``verify --receipt``) and
not to reload.  See :func:`_receipt_note`.

**IT MAKES A SECOND CONCURRENT RUN VISIBLE**, because that is a failure the
chunking cannot survive.  One chunk in flight is load-bearing (KBUtilLib issue
84: claim cost 217 ms at 19k pending rows against 10.4 ms at 2k, a 10.8x
throughput collapse), and the daemon drains every claimable job -- so two run
directories both in stage 4 interleave their chunks through the one daemon and
rebuild the pending set the chunking exists to avoid.  Neither run can see the
other; this verb can.

THIS MODULE IMPORTS NO NUMPY, for the same reason :mod:`.council_run`,
:mod:`.council_publish`, :mod:`.unrun` and :mod:`.verdicts` do not: status
ranks nothing.  The arithmetic (:func:`lifecycle_coverage`,
:func:`parameter_set_coverage`, :func:`concurrent_runs`) is PURE, and the two
clearinghouse reads are thin calls around it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

__all__ = [
    "LIFECYCLE_STATES",
    "IN_FLIGHT_STATES",
    "StatusRefused",
    "concurrent_runs",
    "council_status",
    "discover_runs",
    "lifecycle_coverage",
    "parameter_set_coverage",
    "run_lifecycle",
    "stage_artifacts",
]

#: The corpus coverage buckets, in pipeline order, and a PARTITION of the
#: corpus: every function is in exactly one.  See the module docstring for why
#: ``running`` absorbs "ran but not published" -- without that fold these
#: seven are not a partition and the counts do not add up.
LIFECYCLE_STATES: Tuple[str, ...] = (
    "un-run",
    "planned-not-quoted",
    "quoted-not-approved",
    "approved-not-run",
    "running",
    "exported-not-loaded",
    "loaded",
)

#: The five states that hold work a run directory has CLAIMED and that has
#: not landed.  ``running`` and ``exported-not-loaded`` are the two that have
#: been PAID FOR, which is why they are reported separately rather than summed
#: into one "in progress" number.
IN_FLIGHT_STATES: Tuple[str, ...] = (
    "planned-not-quoted",
    "quoted-not-approved",
    "approved-not-run",
    "running",
    "exported-not-loaded",
)

#: The stage that writes each artifact, for the refusal messages.  An operator
#: reading "no QUOTE.json" needs the verb that writes one, not the filename.
_WRITTEN_BY: Dict[str, str] = {
    "plan": "kbu ontomap council-plan",
    "quote": "kbu ontomap council-quote",
    "approval": "kbu ontomap council-approve",
    "run": "kbu ontomap council-run",
    "publish": "kbu ontomap council-publish",
    "receipt": "kbu clearinghouse verify --receipt (on kbhub, in-pod)",
}


class StatusRefused(Exception):
    """Status cannot be reported, and :attr:`check` says why.

    Carries the failed check as a field as well as in the message, exactly as
    :class:`~kbutillib.domains.genome.ontology_mapping.council_run.RunRefused`
    and
    :class:`~kbutillib.domains.ai.council.approval.ApprovalRefused` do, so a
    caller can branch on WHICH check failed without parsing English.
    """

    def __init__(self, check: str, detail: str) -> None:
        self.check = check
        self.detail = detail
        super().__init__(f"council_status check {check!r} failed: {detail}")


def _now_iso() -> str:
    """The current UTC time as an ISO-8601 string with a ``Z`` suffix."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@lru_cache(maxsize=1)
def stage_artifacts() -> Dict[str, str]:
    """``{stage_key: filename}`` for the six artifacts the lifecycle reads.

    Every name is IMPORTED from the stage that writes it rather than restated:
    a status verb with its own copy of ``PUBLISH.json`` would report "not
    published" about a published run the day either side renamed the file.

    LAZY, CACHED, AND NOT A MODULE-LEVEL CONSTANT. Resolving these at import
    time pulls ``council.quote`` -> ``sqlalchemy`` and ``council_publish`` ->
    the berdl stack into every importer of this module, including
    ``kbu --help``. A function behind :func:`~functools.lru_cache` resolves
    the six names exactly once, on first use, and leaves the import graph
    where the rest of this package keeps it.
    """
    from kbutillib.domains.ai.council.approval import (
        APPROVAL_FILENAME,
        RUN_FILENAME,
    )
    from kbutillib.domains.ai.council.quote import PLAN_FILENAME, QUOTE_FILENAME

    from .council_publish import PUBLISH_FILENAME, ci_receipt_filename

    return {
        "plan": PLAN_FILENAME,
        "quote": QUOTE_FILENAME,
        "approval": APPROVAL_FILENAME,
        "run": RUN_FILENAME,
        "publish": PUBLISH_FILENAME,
        "receipt": ci_receipt_filename(),
    }


def _read_artifact(path: Path) -> Optional[Tuple[Dict[str, Any], bool]]:
    """One artifact as ``(payload, readable)``, or ``None`` when it is absent.

    THREE OUTCOMES AND NOT TWO, because a corrupt artifact is NOT an absent
    one and must not read as a stage that never ran. ``None`` is absent;
    ``({...}, True)`` parsed; ``({}, False)`` is present and unparseable --
    its presence counts for the lifecycle state (a stage that wrote a file did
    run) while every field reads as missing, and :func:`run_lifecycle` reports
    it in ``unreadable``. Distinguishing the last two by "did it parse to an
    empty dict" would flag a legitimately empty artifact as corrupt.

    A half-written artifact is a real state: each stage writes atomically, but
    the run directory is Dropbox-synced and a partial sync is not the writer's
    doing.
    """
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}, False
    return (dict(raw), True) if isinstance(raw, Mapping) else ({}, False)


def run_lifecycle(run_dir: Union[str, Path]) -> Dict[str, Any]:
    """Classify ONE run directory, from the artifacts present in it.

    Pure apart from the six ``stat``/``read`` calls on the run directory
    itself; it reaches no clearinghouse and no council store.

    Args:
        run_dir: A run directory.  One without a ``PLAN.json`` is reported
            with ``state=None`` rather than refused: a status sweep over a
            runs root will meet directories that are not runs, and failing the
            whole sweep over one of them would be absurd.

    Returns:
        ``run_id``, ``run_dir``, ``state`` (one of
        :data:`LIFECYCLE_STATES` minus ``un-run``, or ``None``), ``functions``
        (how many clearinghouse functions the plan claimed -- its
        ``counts.unrun``, which is ``submit`` fanned back out plus
        ``abstain``), ``items`` (the paid answers: ``counts.submit``),
        ``query``, ``source``, ``parameter_set_hash``, ``plan_hash``,
        ``planned_at``, ``complete``, ``halt``, ``published``, ``receipt_ok``
        and ``unreadable`` (the artifact keys that are present but would not
        parse).
    """
    directory = Path(run_dir).expanduser()
    found = {
        key: _read_artifact(directory / name)
        for key, name in stage_artifacts().items()
    }
    present = {key for key, value in found.items() if value is not None}
    unreadable = sorted(
        key for key, value in found.items() if value is not None and not value[1]
    )

    plan = (found.get("plan") or ({}, True))[0]
    run = (found.get("run") or ({}, True))[0]
    receipt = (found.get("receipt") or ({}, True))[0]
    counts = dict(plan.get("counts") or {})

    if "plan" not in present:
        state: Optional[str] = None
    elif "receipt" in present:
        state = "loaded"
    elif "publish" in present:
        state = "exported-not-loaded"
    elif "run" in present:
        # RUN.json present and PUBLISH.json absent.  Deliberately one bucket
        # for "draining", "halted" and "settled but unpublished": see the
        # module docstring on why the seven states need this fold to be a
        # partition.  Which of the three it is rides on `complete`/`halt`.
        state = "running"
    elif "approval" in present:
        state = "approved-not-run"
    elif "quote" in present:
        state = "quoted-not-approved"
    else:
        state = "planned-not-quoted"

    return {
        "run_id": str(plan.get("run_id") or directory.name),
        "run_dir": str(directory),
        "state": state,
        # The FUNCTION count, not the item count.  One paid answer can serve
        # several functions (a definition text shared by three entity hashes
        # is one submit row and three result rows), so an item count would
        # under-report coverage of the corpus by exactly the fan-out.
        "functions": int(counts.get("unrun") or 0),
        "items": int(counts.get("submit") or 0),
        "query": str(plan.get("query") or "") or None,
        "source": str(plan.get("source") or "") or None,
        "parameter_set_hash": str(plan.get("parameter_set_hash") or "") or None,
        "plan_hash": str(plan.get("plan_hash") or "") or None,
        "planned_at": str(plan.get("generated_at") or "") or None,
        "artifacts": sorted(present),
        "unreadable": unreadable,
        # ``None`` means UNKNOWN, and an UNREADABLE ``RUN.json`` is unknown
        # rather than incomplete: ``bool({}.get("complete"))`` would be
        # ``False``, which reads as the definite claim "this run did not
        # finish" about a run whose record merely would not parse.
        "complete": (
            bool(run.get("complete"))
            if "run" in present and "run" not in unreadable
            else None
        ),
        "halt": (
            run.get("halt")
            if "run" in present and "run" not in unreadable
            else None
        ),
        "published": "publish" in present,
        "receipt_ok": bool(receipt.get("ok")) if "receipt" in present else None,
    }


def discover_runs(runs_root: Union[str, Path]) -> List[Dict[str, Any]]:
    """Every run directory under *runs_root*, classified, newest plan first.

    A directory with no ``PLAN.json`` is dropped: it is not a run, and a runs
    root on a synced surface accumulates scratch, ``.partial`` leftovers and
    the odd editor directory.  Sorted by the plan's own ``generated_at`` and
    then by ``run_id``, so the order is the order the runs were planned in
    rather than whatever the filesystem returned.
    """
    root = Path(runs_root).expanduser()
    if not root.is_dir():
        raise StatusRefused(
            "runs_root",
            f"{root} is not a directory, so there are no run directories to "
            "classify. Pass the directory that HOLDS the per-run "
            "directories, not a run directory itself.",
        )
    rows = [
        row
        for child in sorted(root.iterdir())
        if child.is_dir()
        for row in (run_lifecycle(child),)
        if row["state"] is not None
    ]
    return sorted(
        rows, key=lambda row: (row.get("planned_at") or "", row["run_id"])
    )


def concurrent_runs(runs: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """The runs that are in STAGE 4 at the same time, or ``[]``.

    Pure.  "In stage 4" is ``state == "running"`` AND not complete: a settled
    run that has merely not been published yet is claiming no daemon capacity,
    so reporting it as a concurrent run would cry wolf on every operator who
    had not got round to ``council-publish`` yet.

    A list and not a boolean, because the answer an operator needs is WHICH
    directories -- the remedy is to stop one of them (write ``STOP`` into its
    directory), and that needs a path.
    """
    live = [
        dict(row)
        for row in runs
        if row.get("state") == "running" and not row.get("complete")
    ]
    return live if len(live) > 1 else []


def lifecycle_coverage(
    *,
    corpus: Optional[int],
    loaded: Optional[int],
    runs: Iterable[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Corpus coverage by lifecycle state.  Pure.

    ``loaded`` is the clearinghouse's own count at the CURRENT parameter set
    and the five in-flight buckets are what the run directories claim; so
    ``un-run`` is what is left::

        un-run = corpus - loaded - sum(in-flight claims)

    THE IN-FLIGHT CLAIMS ARE AS-OF PLAN TIME and can therefore OVERLAP
    ``loaded``: a published run whose rows landed and whose receipt write
    was lost is counted in ``exported-not-loaded`` by its files AND in
    ``loaded`` by the table.  That is a real condition worth seeing, not an
    arithmetic bug, so the overlap is reported (``overclaimed``) and
    ``un-run`` is clamped at zero and declared a FLOOR rather than being
    silently reconciled away.

    Args:
        corpus: Functions in the clearinghouse universe.  ``None`` when the
            read was degraded; every derived figure then reads ``None``.
        loaded: Functions with a current-state result row at the current
            parameter set.  ``None`` when no current parameter set resolved
            (see :func:`council_status`), which makes ``un-run`` unknowable
            too -- and ``None`` is the honest answer, not 0.
        runs: :func:`run_lifecycle` rows.

    Returns:
        ``states`` (one entry per :data:`LIFECYCLE_STATES`, each with
        ``state``, ``functions``, ``runs`` and ``share``), ``corpus``,
        ``paid_for_not_landed``, ``overclaimed`` and ``un_run_is_a_floor``.
    """
    rows = [dict(row) for row in runs]
    by_state: Dict[str, List[Dict[str, Any]]] = {
        state: [row for row in rows if row.get("state") == state]
        for state in LIFECYCLE_STATES
    }
    claimed = {
        state: sum(int(row.get("functions") or 0) for row in by_state[state])
        for state in IN_FLIGHT_STATES
    }
    in_flight = sum(claimed.values())

    unknown = corpus is None or loaded is None
    overclaimed = (
        None if unknown else max(0, int(loaded or 0) + in_flight - int(corpus or 0))
    )
    un_run = (
        None
        if unknown
        else max(0, int(corpus or 0) - int(loaded or 0) - in_flight)
    )

    counts: Dict[str, Optional[int]] = {"un-run": un_run, "loaded": loaded}
    counts.update(claimed)

    def _share(value: Optional[int]) -> Optional[float]:
        if value is None or not corpus:
            return None
        return round(value / int(corpus), 6)

    return {
        "corpus": corpus,
        "states": [
            {
                "state": state,
                "functions": counts.get(state),
                # ``un-run`` and ``loaded`` are properties of the CORPUS, not
                # of any run directory, so they carry no run count. Reporting
                # 0 there would read as "no runs have loaded anything".
                "runs": (
                    None
                    if state in ("un-run", "loaded")
                    else len(by_state[state])
                ),
                "share": _share(counts.get(state)),
            }
            for state in LIFECYCLE_STATES
        ],
        # THE NUMBER THIS VERB EXISTS FOR. Work that has been paid for and
        # has not landed: tokens spent, nothing in the clearinghouse. It is
        # invisible in both an un-run count and a loaded count.
        "paid_for_not_landed": claimed["running"] + claimed["exported-not-loaded"],
        "overclaimed": overclaimed,
        "un_run_is_a_floor": bool(overclaimed),
    }


def parameter_set_coverage(
    done: Iterable[Mapping[str, Any]],
    *,
    corpus: Optional[int],
    current: Optional[str],
) -> Dict[str, Any]:
    """Corpus coverage by ``parameter_set_hash``.  Pure.

    PARTIAL-VERSION COVERAGE IS TOLERATED AND THEREFORE REPORTED.  Re-running
    47,341 functions on every bench, query-version or ``mapping_hash`` bump is
    unaffordable, so a corpus legitimately carries several parameter sets at
    once -- and the only way "is this dataset consistent?" has an answer is if
    the split is published rather than summed.

    Args:
        done: Current-state ``function_result`` rows for this source, over
            EVERY parameter set (no hash filter).  ``parameter_set_hash`` is
            part of the slot key
            (:data:`~kbutillib.domains.kbase.berdl.clearinghouse_derivation.SLOT_KEY_COLUMNS`),
            so current state holds one row per
            ``(entity_hash, parameter_set_hash)`` and the split is a group-by
            rather than a second read per hash.
        corpus: Functions in the universe, for the shares.  ``None`` leaves
            every share ``None``.
        current: This run's ``parameter_set_hash``, or ``None`` when none
            resolved.

    Returns:
        ``parameter_sets`` (one row per hash, the current one first and the
        rest by descending function count), ``current``,
        ``covered_only_by_an_older_parameter_set``, ``covered_at_all`` and
        ``uncovered``.

    Raises:
        ValueError: A row carries no ``entity_hash`` or no
            ``parameter_set_hash``.  Either makes the row uncountable, and
            dropping it would under-report coverage with nothing to explain
            the gap.
    """
    by_hash: Dict[str, set] = {}
    all_entities: set = set()
    for row in done or ():
        if not isinstance(row, Mapping):
            raise ValueError(
                f"a current-state row is a {type(row).__name__}; expected a "
                "result row mapping with entity_hash and parameter_set_hash"
            )
        entity = row.get("entity_hash")
        parameter_set = row.get("parameter_set_hash")
        if entity is None or parameter_set is None:
            raise ValueError(
                "a current-state row carries no "
                + ("entity_hash" if entity is None else "parameter_set_hash")
                + "; coverage by parameter set cannot be counted without "
                f"both. Row keys: {sorted(row)}"
            )
        by_hash.setdefault(str(parameter_set), set()).add(str(entity))
        all_entities.add(str(entity))

    def _share(value: int) -> Optional[float]:
        return None if not corpus else round(value / int(corpus), 6)

    rows = [
        {
            "parameter_set_hash": digest,
            "current": digest == current,
            "functions": len(entities),
            "share": _share(len(entities)),
        }
        for digest, entities in by_hash.items()
    ]
    # The current set first -- it is the one an operator is asking about --
    # then the rest biggest-first, with the hash as a deterministic tie-break.
    rows.sort(key=lambda row: (not row["current"], -row["functions"], row["parameter_set_hash"]))
    if current and current not in by_hash:
        rows.insert(
            0,
            {
                "parameter_set_hash": current,
                "current": True,
                "functions": 0,
                "share": _share(0),
            },
        )

    at_current = by_hash.get(current or "", set())
    stale_only = all_entities - at_current
    return {
        "current": current,
        "parameter_sets": rows,
        "covered_at_all": len(all_entities),
        # THE PARTIAL-VERSION NUMBER. Functions the clearinghouse has an
        # answer for, but not at THIS parameter set: stage 1 will re-select
        # every one of them, and they are not a gap in the dataset.
        "covered_only_by_an_older_parameter_set": (
            None if current is None else len(stale_only)
        ),
        "uncovered": (
            None if corpus is None else max(0, int(corpus) - len(all_entities))
        ),
    }


# ---------------------------------------------------------------------------
# The verb
# ---------------------------------------------------------------------------


def council_status(
    runs_root: Optional[Union[str, Path]] = None,
    *,
    run_dirs: Optional[Sequence[Union[str, Path]]] = None,
    query: Optional[str] = None,
    parameter_set_hash: Optional[str] = None,
    clearinghouse: Any = None,
) -> Dict[str, Any]:
    """Report corpus coverage by lifecycle state and by ``parameter_set_hash``.

    SPENDS NO LLM TOKENS, writes nothing, and runs anywhere.  The two
    clearinghouse reads work from h100 off-pod over the BERDL delta API
    (measured 2026-10-22); only ``load`` and ``verify`` are in-pod, so this
    verb is deliberately NOT gated on the locus.

    THE TWO READS, and why they are the two:

    1. :meth:`ClearinghouseCapability.stats` for the corpus size -- a
       ``COUNT(*)``, not the 47,341-row join
       :meth:`~ClearinghouseCapability.universe` does.  Status is the verb an
       operator runs repeatedly while a 9-hour run drains, so it must be
       cheap.  The cost is that ``function_entity`` and ``function_content``
       can disagree -- the universe is their INNER JOIN, so it is at most the
       smaller -- and when they do, the smaller is used and a warning says so.
       Stage 1 is the verb that does the exact join.
    2. :meth:`ClearinghouseCapability.current_state` for this source over
       EVERY parameter set (no hash filter), which is what makes the
       partition split a group-by rather than one read per hash.

    Args:
        runs_root: The directory HOLDING the per-run directories.  Every run
            under it is classified.
        run_dirs: Specific run directories, instead of (or as well as) a root.
        query: ``"<name>@<version>"``, which fixes the ``source``
            (``council/<query>``) the current-state read filters on.  Default:
            the query the newest discovered plan records, else
            :data:`~.ontology_mapping_utils.DEFAULT_COUNCIL_QUERY`.
        parameter_set_hash: The CURRENT parameter set.  Default: the one the
            newest discovered plan records.  Without either, the lifecycle
            report's ``un-run`` and ``loaded`` read ``None`` with a warning
            and the partition report still answers in full -- deriving the
            hash live would need the retrieval backend's ``mapping_hash`` and
            the live bench, and a status verb must not fail on a host that
            has no numpy.
        clearinghouse: The read surface, injectable for tests.  Default: a
            lazily-constructed :class:`ClearinghouseCapability`.

    Returns:
        The status record: ``schema_version``, ``generated_at``, ``query``,
        ``source``, ``parameter_set_hash``, ``corpus``, ``lifecycle``,
        ``by_parameter_set``, ``runs``, ``concurrent_runs`` and ``warnings``.

    Raises:
        StatusRefused: Neither *runs_root* nor *run_dirs* was given, or
            *runs_root* is not a directory.
    """
    from .ontology_mapping_utils import COUNCIL_SOURCE_PREFIX, DEFAULT_COUNCIL_QUERY

    if runs_root is None and not run_dirs:
        raise StatusRefused(
            "target",
            "no runs root and no run directory. Status is a report ACROSS "
            "runs -- a second concurrent run is one of the things it exists "
            "to surface -- so it needs the directory that holds them (or an "
            "explicit list of them).",
        )

    runs: List[Dict[str, Any]] = []
    seen: set = set()
    for row in list(discover_runs(runs_root) if runs_root is not None else []) + [
        run_lifecycle(path) for path in run_dirs or ()
    ]:
        if row["run_dir"] in seen:
            continue
        seen.add(row["run_dir"])
        runs.append(row)
    runs.sort(key=lambda row: (row.get("planned_at") or "", row["run_id"]))

    warnings: List[str] = []
    # The NEWEST plan is the one that speaks for "current": a status sweep
    # over a runs root holds every historical run, and the oldest plan's
    # parameter set is precisely the stale one the partition half reports.
    newest = next(
        (row for row in reversed(runs) if row.get("parameter_set_hash")), None
    )
    resolved_query = query or (newest or {}).get("query") or DEFAULT_COUNCIL_QUERY
    source = f"{COUNCIL_SOURCE_PREFIX}{resolved_query}"
    current = parameter_set_hash or (newest or {}).get("parameter_set_hash")
    if current is None:
        warnings.append(
            "no current parameter_set_hash: none was passed and no "
            "discovered plan records one, so 'un-run' and 'loaded' cannot be "
            "counted and read null. The coverage BY PARAMETER SET below is "
            "still complete. Pass the hash, or point this at a runs root "
            "holding a PLAN.json."
        )

    disagreeing = sorted(
        {
            str(row["parameter_set_hash"])
            for row in runs
            if row.get("parameter_set_hash")
            and row.get("state") in IN_FLIGHT_STATES
            and row["parameter_set_hash"] != current
        }
    )
    if disagreeing:
        warnings.append(
            f"{len(disagreeing)} in-flight run(s) are at a DIFFERENT "
            "parameter set from the current one ("
            + ", ".join(digest[:12] + "..." for digest in disagreeing)
            + f" against {str(current)[:12]}...). Their work will land at "
            "their own hash, so it will not count towards current coverage "
            "and the next plan at the current hash will re-select every "
            "function they are answering."
        )

    cap = clearinghouse if clearinghouse is not None else _clearinghouse()
    corpus, corpus_detail, corpus_warnings = _corpus(cap)
    warnings.extend(corpus_warnings)

    done: List[Mapping[str, Any]] = []
    try:
        done = list(cap.current_state("function", sources=[source]))
    except Exception as exc:  # noqa: BLE001 - a degraded read is a warning
        warnings.append(
            f"the current-state read for source {source!r} failed "
            f"({type(exc).__name__}: {exc}), so coverage by parameter set and "
            "'loaded' are UNKNOWN rather than zero. The lifecycle states "
            "below that come from the run directories are unaffected."
        )
        partition = parameter_set_coverage((), corpus=corpus, current=current)
        partition["covered_at_all"] = None
        partition["covered_only_by_an_older_parameter_set"] = None
        partition["uncovered"] = None
        loaded: Optional[int] = None
    else:
        partition = parameter_set_coverage(done, corpus=corpus, current=current)
        loaded = (
            None
            if current is None
            else next(
                (
                    int(row["functions"])
                    for row in partition["parameter_sets"]
                    if row["current"]
                ),
                0,
            )
        )

    lifecycle = lifecycle_coverage(corpus=corpus, loaded=loaded, runs=runs)
    at_current = {
        row["parameter_set_hash"]: int(row["functions"])
        for row in partition["parameter_sets"]
    }
    for row in runs:
        row["rows_at_parameter_set"] = at_current.get(
            str(row.get("parameter_set_hash") or ""), 0
        )
        note = _receipt_note(row)
        if note:
            row["note"] = note
            warnings.append(note)

    for row in runs:
        if row.get("unreadable"):
            warnings.append(
                f"run {row['run_id']!r}: "
                + ", ".join(
                    f"{stage_artifacts()[key]} is present but will not parse "
                    f"(written by `{_WRITTEN_BY[key]}`)"
                    for key in row["unreadable"]
                )
                + ". Its presence still counts for the lifecycle state, "
                "because a stage that wrote a file did run; its contents do "
                "not, so this run's figures are incomplete."
            )
        if row.get("halt"):
            halt = row["halt"] if isinstance(row["halt"], Mapping) else {}
            warnings.append(
                f"run {row['run_id']!r} is HALTED "
                f"({halt.get('reason') or 'reason not recorded'}): "
                f"{halt.get('detail') or 'no detail recorded'} It is counted "
                "in 'running' -- the tokens it spent are spent and nothing "
                "has landed. Clear the halt and resume it, or publish what "
                "it got."
            )

    concurrent = concurrent_runs(runs)
    if concurrent:
        warnings.append(
            f"{len(concurrent)} runs are in STAGE 4 AT THE SAME TIME ("
            + ", ".join(f"{row['run_id']} at {row['run_dir']}" for row in concurrent)
            + "). ONE CHUNK IN FLIGHT IS LOAD-BEARING: the daemon drains "
            "every claimable job, so two runs interleave their chunks "
            "through it and rebuild the pending set the chunking exists to "
            "avoid (KBUtilLib issue 84 -- claim cost 217 ms at 19k pending "
            "rows against 10.4 ms at 2k, a 10.8x throughput collapse). Stop "
            "one of them: write a STOP file into its run directory."
        )
    if lifecycle["overclaimed"]:
        warnings.append(
            f"the run directories claim {lifecycle['overclaimed']} function(s) "
            "more than the corpus has room for alongside what is loaded, so "
            "'un-run' is a FLOOR rather than an exact count. The usual cause "
            "is a published run whose rows have landed while its receipt has "
            "not been written -- see the per-run notes."
        )

    return {
        "schema_version": 1,
        "generated_at": _now_iso(),
        "query": resolved_query,
        "source": source,
        "parameter_set_hash": current,
        "corpus": corpus_detail,
        "lifecycle": lifecycle,
        "by_parameter_set": partition,
        "runs": runs,
        "concurrent_runs": concurrent,
        "warnings": warnings,
    }


def _receipt_note(row: Mapping[str, Any]) -> Optional[str]:
    """The note for a published run whose rows look like they already landed.

    THE ONE CROSS-CHECK THIS VERB MAKES, and the reason ``loaded`` is read
    from the table rather than from receipts.  The Iceberg commit and the
    ``LOADED.json`` write are two unrelated writes with no transaction between
    them, so a crash in the gap leaves the rows LANDED and the receipt MISSING
    -- and a producer that read "no receipt" as "not loaded" would ask for a
    reload of rows that are already there.  The receipt is derived entirely
    from the ledger, the inventory, the snapshot history and ``PUBLISH.json``,
    so it is RE-EXPORTABLE: the fix is ``verify --receipt``, not a reload.
    """
    if row.get("state") != "exported-not-loaded":
        return None
    landed = int(row.get("rows_at_parameter_set") or 0)
    if not landed:
        return None
    return (
        f"run {row['run_id']!r} is published with NO receipt, but the "
        f"clearinghouse already holds {landed} function(s) at its parameter "
        f"set {str(row.get('parameter_set_hash'))[:12]}.... That is the "
        "crash-after-commit shape: the Iceberg commit and the "
        f"{stage_artifacts()['receipt']} write are two writes with no "
        "transaction between them. The receipt is derived from the ledger, "
        "the inventory and PUBLISH.json, so RE-EXPORT it -- "
        f"`{_WRITTEN_BY['receipt']}` -- rather than reloading rows that are "
        "already there."
    )


def _corpus(cap: Any) -> Tuple[Optional[int], Dict[str, Any], List[str]]:
    """The corpus size from ``stats()``, with the entity/content disagreement.

    Returns ``(corpus, detail, warnings)``.  ``corpus`` is the SMALLER of the
    two tables' counts, because the universe is their inner join and so can be
    no bigger than either; the detail carries both so an operator can see the
    gap, and a disagreement is a warning rather than a refusal (a content gap
    is a real, reportable state of the lake and not a reason to refuse to say
    anything about the lifecycle).
    """
    try:
        report = cap.stats()
    except Exception as exc:  # noqa: BLE001 - a degraded read is a warning
        return (
            None,
            {"entity_rows": None, "content_rows": None, "functions": None},
            [
                f"the clearinghouse stats read failed ({type(exc).__name__}: "
                f"{exc}), so the corpus size is UNKNOWN and every share and "
                "the 'un-run' count read null. The run directories' own "
                "lifecycle states are unaffected."
            ],
        )
    counts = {
        str(table.get("name")): table.get("row_count")
        for table in (report or {}).get("tables") or ()
    }
    entity = counts.get("function_entity")
    content = counts.get("function_content")
    warnings = [str(line) for line in (report or {}).get("warnings") or ()]
    if not isinstance(entity, int) or not isinstance(content, int):
        return (
            None,
            {"entity_rows": entity, "content_rows": content, "functions": None},
            warnings
            + [
                "the clearinghouse stats read reported no row count for "
                "function_entity or function_content, so the corpus size is "
                "UNKNOWN and reads null rather than zero."
            ],
        )
    if entity != content:
        warnings.append(
            f"function_entity holds {entity:,} row(s) and function_content "
            f"{content:,}: the universe is their INNER JOIN, so it is at most "
            f"{min(entity, content):,} and that is what the shares below are "
            "against. The exact join is stage 1's "
            "(`kbu ontomap council-plan`); this verb reads COUNT(*) because "
            "it is run repeatedly while a 9-hour run drains."
        )
    return (
        min(entity, content),
        {
            "entity_rows": entity,
            "content_rows": content,
            "functions": min(entity, content),
            "basis": "stats() COUNT(*), not the universe() join",
        },
        warnings,
    )


def _clearinghouse() -> Any:
    """The clearinghouse READ surface, lazily.

    The same factory
    :meth:`~.ontology_mapping_utils.OntologyMappingUtils._clearinghouse` uses,
    spelled here so status needs no ``OntologyMappingUtils`` instance -- and
    therefore no numpy/scikit-learn/joblib -- to answer.  NOT gated on the
    locus: the reads work off-pod over the BERDL delta API.
    """
    from kbutillib.domains.kbase.berdl.clearinghouse_capability import (
        ClearinghouseCapability,
    )

    return ClearinghouseCapability()
