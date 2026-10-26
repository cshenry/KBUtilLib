"""The END-TO-END ACCEPTANCE REHEARSAL for COUNCIL-RECORD-LAKEHOUSE-V1.

ONE RUNNABLE TARGET, ZERO TOKENS, NO LIVE COUNCIL RUN. It drives the shipped
rail -- bootstrap, identity assignment, COUNCIL-DELIVERY v1 manifest, the
delivery SCANNER, the archiver's load ledger, disposition seeding, the
publication bundle, the lakehouse publisher, the reuse seed and a second
mutated snapshot -- against the REAL 2026-10-23 production snapshot, and
asserts what landed BY DIRECT QUERY against the sink rather than by any
command's exit code.

WHY A REPORT AND NOT A TEST RUN. The failure this module exists to prevent is
a step that SILENTLY PASSES OVER something it could not check. A pytest run
reports pass/fail per test and says nothing about a check that was skipped
because its input was missing, which is precisely the case that matters here
(the 2026-10-19 campaign's run directory does not exist -- see
:func:`step_0_run_directory`). So every step settles into exactly one of
three states and a step with NO assertions can never read as ASSERTED:

* ``ASSERTED``     -- at least one assertion, every one of them true, each
                      carrying its MEASURED value.
* ``FAILED``       -- an assertion was false, or the step raised. Evidence is
                      carried, never summarised away.
* ``NOT ASSERTED`` -- the step could not run, with the REASON. This is a
                      first-class outcome, not a skip: it is the only honest
                      answer for a leg whose input is gone, and it is the one
                      a reader has to be able to see.

:meth:`Step.settle` is the whole of that rule and is what the unit tests in
``tests/berdl/test_council_rehearsal.py`` pin -- they need no snapshot.

NOTHING HERE TOUCHES PRODUCTION. :func:`require_scratch_target` refuses a
work root that is not under a declared scratch prefix and refuses the
production store path outright, because IDENTITY ASSIGNMENT IS IRREVERSIBLE:
``SqliteStore.assign_store_identity`` is a one-way act whose re-declaration
needs an explicit ``redeclare_store_identity``, so it is run ONLY on a copy
this module made. The real snapshot is READ-ONLY input and is never written,
never committed and never derived-from-in-place.

NO PRODUCTION CLEARINGHOUSE ROW IS WRITTEN, and the LIVE IN-POD LOAD IS NOT
ATTEMPTED AND NOT CLAIMED. KBUtilLib#91 has had the BERDL transport broken on
kbhub since the 2026-10-19 image, kbhub is not a Maestro or AgentForge worker,
and a mocked load is NEVER an acceptance. That leg is reported as
``awaiting operator acceptance`` by :func:`in_pod_leg`.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

__all__ = [
    "ASSERTED",
    "FAILED",
    "NOT_ASSERTED",
    "PRODUCTION_STORE_PATHS",
    "REHEARSAL_STORE_ID",
    "SCRATCH_PREFIXES",
    "Assertion",
    "Step",
    "Rehearsal",
    "RehearsalRefused",
    "require_scratch_target",
    "in_pod_leg",
    "render_report",
    "run_rehearsal",
]


#: The three step states. A step with no assertions may never be ASSERTED.
ASSERTED = "ASSERTED"
FAILED = "FAILED"
NOT_ASSERTED = "NOT ASSERTED"

#: The store id this rehearsal declares. DELIBERATELY NOT ``poplar/council-1``:
#: that id is reserved, its assignment is Chris's decision, and an archived
#: row keyed on it cannot be corrected once a clearinghouse payload carries
#: it. ``rehearsal/`` cannot be mistaken for production by any reader.
REHEARSAL_STORE_ID = "rehearsal/council-20261023"

#: Work roots this module will accept. Everything it writes -- the store copy
#: whose identity it assigns, the delivery drops, the scratch sink, the reuse
#: store -- goes under one of these.
SCRATCH_PREFIXES: tuple[str, ...] = ("/scratch/", "/tmp/")

#: Paths that are NOT a rehearsal target under any circumstances. The first is
#: the live production store; the second is the shared decompressed copy of
#: the read-only snapshot, which other tranches' work depends on byte-for-byte.
PRODUCTION_STORE_PATHS: tuple[str, ...] = (
    "/scratch/chenry/council/council.sqlite",
    "/scratch/chenry/council/council-snapshot-20261023.sqlite",
    "/scratch/chenry/tmp/council-snapshot-20261023.sqlite",
)


class RehearsalRefused(RuntimeError):
    """The rehearsal will not run against the target it was given."""


def require_scratch_target(path: Any, *, what: str) -> Path:
    """Return *path* resolved, or REFUSE because it is not a scratch target.

    Two refusals, and neither is negotiable. A path outside
    :data:`SCRATCH_PREFIXES` is refused because this module assigns a store
    identity, which is irreversible. A path in
    :data:`PRODUCTION_STORE_PATHS` is refused by exact match because those
    three files are, in order, the live store, the snapshot taken from it and
    the shared read-only decompressed copy -- and an identity assigned to any
    of them could not be taken back.

    Raises:
        RehearsalRefused: either refusal.
    """
    target = Path(path).expanduser()
    text = str(target)
    if text in PRODUCTION_STORE_PATHS:
        raise RehearsalRefused(
            f"refusing to use {text!r} as the rehearsal's {what}: it is a "
            "production or shared read-only path. Identity assignment is "
            "one-way (redeclare_store_identity is the only way back) and the "
            "shared snapshot copy is depended on by digest. Copy it first."
        )
    if not any(text.startswith(prefix) for prefix in SCRATCH_PREFIXES):
        raise RehearsalRefused(
            f"refusing {text!r} as the rehearsal's {what}: it is not under "
            f"any of {SCRATCH_PREFIXES!r}. Everything this rehearsal writes "
            "is disposable by construction, and a target outside scratch is "
            "how a rehearsal becomes a production write."
        )
    return target


# --------------------------------------------------------------------------
# The report model
# --------------------------------------------------------------------------


@dataclass
class Assertion:
    """One checked claim, with the value that was MEASURED for it.

    ``expected`` and ``measured`` are both carried even when the assertion
    holds: a report that only showed values for failures could not be read as
    a measurement of the snapshot, which is half of what this rehearsal is
    for.
    """

    claim: str
    expected: Any
    measured: Any
    ok: bool
    evidence: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim,
            "expected": self.expected,
            "measured": self.measured,
            "ok": bool(self.ok),
            "evidence": self.evidence,
        }


@dataclass
class Step:
    """One numbered step of the rehearsal and everything it established."""

    number: str
    name: str
    assertions: list[Assertion] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    reason: str = ""
    state: str = ""
    seconds: float = 0.0

    # -- building -------------------------------------------------------

    def check(
        self,
        claim: str,
        expected: Any,
        measured: Any,
        *,
        ok: Any = None,
        evidence: str = "",
    ) -> Assertion:
        """Record one assertion. Equality is the default comparison."""
        held = (expected == measured) if ok is None else bool(ok)
        assertion = Assertion(
            claim=claim,
            expected=expected,
            measured=measured,
            ok=held,
            evidence=evidence,
        )
        self.assertions.append(assertion)
        return assertion

    def find(self, text: str) -> None:
        """Record a FINDING -- something true of the code or the data that a
        human has to decide about. Never silently absorbed into a fix."""
        self.findings.append(text)

    def note(self, text: str) -> None:
        self.notes.append(text)

    def not_asserted(self, reason: str) -> None:
        """Declare this step unassertable, with the reason. Terminal."""
        self.reason = reason
        self.state = NOT_ASSERTED

    def fail(self, reason: str) -> None:
        self.reason = reason
        self.state = FAILED

    # -- settling -------------------------------------------------------

    def settle(self) -> str:
        """Resolve this step's state. THE RULE, and the whole point.

        An explicitly declared ``NOT ASSERTED`` or ``FAILED`` stands. A step
        with a FALSE assertion is ``FAILED``. A step with NO assertions is
        ``NOT ASSERTED`` -- never ``ASSERTED`` -- because a step that checked
        nothing has established nothing, and reading that as a pass is the
        exact failure this report exists to prevent.
        """
        if self.state in (NOT_ASSERTED, FAILED):
            return self.state
        if any(not assertion.ok for assertion in self.assertions):
            self.state = FAILED
            if not self.reason:
                failed = [a.claim for a in self.assertions if not a.ok]
                self.reason = f"{len(failed)} assertion(s) did not hold: {failed}"
            return self.state
        if not self.assertions:
            self.state = NOT_ASSERTED
            if not self.reason:
                self.reason = (
                    "the step ran but asserted nothing, which establishes "
                    "nothing; a step with no assertions is never a pass"
                )
            return self.state
        self.state = ASSERTED
        return self.state

    def as_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "name": self.name,
            "state": self.settle(),
            "reason": self.reason,
            "seconds": round(self.seconds, 2),
            "assertions": [a.as_dict() for a in self.assertions],
            "findings": list(self.findings),
            "notes": list(self.notes),
        }


def _read_published_rows(path: Any) -> list[dict[str, Any]]:
    """Read a verdict parquet back as rows. READ BACK, not kept in memory.

    The assertions about what was published are made against the FILE the
    publisher wrote, not against the list it returned: the parquet is the
    artifact the clearinghouse loader will read, and a schema that dropped or
    re-typed a column would be invisible to a check against the in-memory
    rows.
    """
    import pyarrow.parquet as pq

    return pq.read_table(Path(path).expanduser()).to_pylist()


def in_pod_leg() -> dict[str, Any]:
    """The live in-pod load leg. REPORTED, never claimed, never mocked.

    Three independent reasons it is not reachable from here, each of which
    alone is sufficient: the BERDL transport is broken on kbhub
    (KBUtilLib#91), ``kbaseincubator.council`` is not bootstrapped there, and
    kbhub is not a Maestro or AgentForge worker so no worker can reach it.
    """
    return {
        "leg": "live in-pod load into kbaseincubator.council on kbhub",
        "state": "awaiting operator acceptance",
        "why": [
            "KBUtilLib#91: the BERDL transport has been broken on kbhub "
            "since the 2026-10-19 image (removed kwargs delta_lake / "
            "use_hive / tenant_name / connector make Spark/Trino calls raise "
            "TypeError). Worked around in-process only, by "
            "albert-load-artifacts/kbu_compat.py.",
            "the kbaseincubator.council namespace is not bootstrapped on "
            "kbhub, so there is nothing to append to.",
            "kbhub is not a Maestro or AgentForge worker, so this rehearsal "
            "cannot reach it from any worker -- by design, not by oversight.",
        ],
        "what_would_close_it": (
            "a RECORDED kbhub smoke test that reads and appends to a "
            "DISPOSABLE council namespace with the DEPLOYED package version, "
            "plus the scanner's Jupyter startup registration. Albert owns "
            "both. A mocked load is NEVER an acceptance."
        ),
        "production_clearinghouse_rows_written": 0,
    }


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _fmt(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return repr(value)
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:.6f}"
    if isinstance(value, (list, tuple, set, dict)):
        try:
            text = json.dumps(value, default=str, sort_keys=True)
        except TypeError:
            # A dict keyed on anything but a JSON scalar, or an unsortable
            # key set. RENDERED ANYWAY: losing a whole completed run to a
            # formatting failure is exactly what happened on the first real
            # run of this report, and a repr is always available.
            text = repr(value)
        return text if len(text) <= 300 else text[:297] + "..."
    return str(value)


def render_report(report: Mapping[str, Any]) -> str:
    """Render a rehearsal report as the operator-facing text block."""
    out: list[str] = []
    summary = report.get("summary") or {}
    out.append("=" * 72)
    out.append("COUNCIL-RECORD-LAKEHOUSE-V1 -- ACCEPTANCE REHEARSAL")
    out.append("=" * 72)
    for key in ("started_at", "finished_at", "seconds", "host", "interpreter"):
        if report.get(key) is not None:
            out.append(f"{key:<16} {_fmt(report[key])}")
    for key in ("store_id", "snapshot_id", "snapshot_2_id", "work_root", "sink"):
        if report.get(key) is not None:
            out.append(f"{key:<16} {_fmt(report[key])}")
    out.append("")
    out.append(
        f"VERDICT: {summary.get('verdict', 'unknown')}  "
        f"({summary.get(ASSERTED, 0)} asserted, {summary.get(FAILED, 0)} failed, "
        f"{summary.get(NOT_ASSERTED, 0)} not asserted; "
        f"{summary.get('assertions_held', 0)}/{summary.get('assertions_total', 0)} "
        "assertions held)"
    )
    out.append("")
    for step in report.get("steps") or ():
        out.append("-" * 72)
        out.append(
            f"STEP {step['number']}  {step['name']}\n"
            f"  STATE: {step['state']}   ({step['seconds']}s)"
        )
        if step.get("reason"):
            out.append(f"  REASON: {step['reason']}")
        for assertion in step.get("assertions") or ():
            mark = "ok  " if assertion["ok"] else "FAIL"
            out.append(f"  [{mark}] {assertion['claim']}")
            out.append(
                f"         expected={_fmt(assertion['expected'])}  "
                f"measured={_fmt(assertion['measured'])}"
            )
            if assertion.get("evidence"):
                out.append(f"         evidence: {assertion['evidence']}")
        for note in step.get("notes") or ():
            out.append(f"  note: {note}")
        for finding in step.get("findings") or ():
            out.append(f"  FINDING: {finding}")
    out.append("-" * 72)
    leg = report.get("in_pod_leg") or {}
    out.append(f"LIVE IN-POD LEG: {leg.get('state')}")
    out.append(f"  {leg.get('leg')}")
    for why in leg.get("why") or ():
        out.append(f"  - {why}")
    out.append(f"  closes when: {leg.get('what_would_close_it')}")
    out.append(
        "  production clearinghouse rows written: "
        f"{leg.get('production_clearinghouse_rows_written')}"
    )
    out.append("=" * 72)
    return "\n".join(out)


# --------------------------------------------------------------------------
# The rehearsal
# --------------------------------------------------------------------------


#: Where a run directory for the 2026-10-19 go_mf@4 ADP1+PMI campaign could
#: plausibly be. Searched, and the search is REPORTED -- /scratch on h100 is
#: ephemeral and is not backed up, so "absent" is an expected answer.
DEFAULT_RUN_DIR_ROOTS: tuple[str, ...] = (
    "/scratch/chenry",
    "/scratch/fliu/hub_home/chenry",
    "~/Dropbox/Jobs/function-to-go",
    "~/projects/KBUtilLib",
)

#: The campaign the rehearsal is about.
CAMPAIGN_QUERY = "go_mf@4"
CAMPAIGN_ID = "function-to-go/2026-10-19-adp1-pmi"

#: The verdict export and the provenance declaration, both committed artifacts
#: outside this repo. Independent of the store, which is what makes step 7 a
#: cross-check rather than a restatement.
DEFAULT_VERDICT_EXPORT = (
    "~/Dropbox/Jobs/function-to-go/2026-10-19-clearinghouse/council-results/"
    "function_to_go_verdicts.jsonl"
)
DEFAULT_PROVENANCE = "~/Dropbox/Jobs/function-to-go/AUTHORITATIVE-JOB-PROVENANCE.json"

#: The campaign's twelve published jobs, in the order that resolves winners:
#: the ten chunk jobs then the two retry passes LAST, so a retried item's
#: verdict supersedes its chunk's. Taken from the provenance declaration at
#: run time; this literal is the ORDER only, and the step asserts the set
#: against the file rather than trusting it.
CHUNK_JOB_IDS: tuple[str, ...] = (
    "2c94eb1b7abd41dca337a1eb9d78d652",
    "a4d2ac39fa60445da3ff2f682b062ed3",
    "7b8a58a4810c4373ba1e84460e02fac1",
    "ff29236aa120486e823c74a0e0812bf4",
    "8952d8af0fd24d4090191d3abff16568",
    "c39b3d2045194e969b223adfb751073e",
    "9bbf0144c87e47028595c4863af5401a",
    "d4dc1d10363e46379f9f528a4c3002c0",
    "34cf8a8c68e7415485818f08061be241",
    "8c870d97ddf349f9904470fb99f9e4c6",
)
RETRY_JOB_IDS: tuple[str, ...] = (
    "4bba63453f284182aa1505af6bc7dcfa",
    "52149f84f3404b9da747fd525433fa38",
)


class Rehearsal:
    """Drives the nine steps against one snapshot and one scratch namespace.

    Every step is run inside :meth:`_run`, which catches whatever the step
    raises and records it as ``FAILED`` with its traceback. A step that blew
    up must not stop the ones after it: the report's value is the COMPLETE
    picture of what the rail does and does not establish, and an exception in
    step 5 says nothing about step 9.
    """

    def __init__(
        self,
        *,
        snapshot: Any,
        work_root: Any,
        store_id: str = REHEARSAL_STORE_ID,
        provenance: Any = DEFAULT_PROVENANCE,
        verdict_export: Any = DEFAULT_VERDICT_EXPORT,
        run_dir_roots: Sequence[str] = DEFAULT_RUN_DIR_ROOTS,
        declared_by: str = "council-acceptance-rehearsal",
    ) -> None:
        self.snapshot = Path(snapshot).expanduser()
        self.work_root = require_scratch_target(work_root, what="work root")
        self.store_id = store_id
        self.provenance = Path(provenance).expanduser()
        self.verdict_export = Path(verdict_export).expanduser()
        self.run_dir_roots = tuple(run_dir_roots)
        self.declared_by = declared_by

        self.steps: list[Step] = []
        # Filled in as the steps run; later steps read these.
        self.sink: Any = None
        self.store_copy: Path | None = None
        self.snapshot_id: str | None = None
        self.snapshot_2_id: str | None = None
        self.state_root: Path | None = None
        self.ledger: Any = None
        self.run_dir: Path | None = None
        self.bundle: Mapping[str, Any] | None = None
        self._receipt: Mapping[str, Any] | None = None
        self._published_rows: list[dict[str, Any]] | None = None

    # -- plumbing -------------------------------------------------------

    @property
    def sink_path(self) -> Path:
        return self.work_root / "lakehouse" / "council_sink.sqlite"

    def _run(self, number: str, name: str, body: Callable[[Step], None]) -> Step:
        step = Step(number=number, name=name)
        started = time.time()
        try:
            body(step)
        except Exception as exc:  # noqa: BLE001 - recorded, never raised past here
            step.fail(f"{type(exc).__name__}: {exc}")
            step.note("traceback:\n" + traceback.format_exc())
        step.seconds = time.time() - started
        step.settle()
        self.steps.append(step)
        return step

    def _store(self, path: Any, **kwargs: Any) -> Any:
        from ...ai.council.store.sqlite import SqliteStore

        return SqliteStore(f"sqlite:///{Path(path).expanduser()}", **kwargs)

    def _source_counts(self) -> dict[str, int]:
        """Row counts read straight out of the snapshot file, for comparison.

        The rehearsal's assertions compare the SINK against the STORE, so the
        store side has to be measured independently of the archiver that
        wrote the sink -- otherwise the comparison is the archiver agreeing
        with itself.
        """
        connection = sqlite3.connect(f"file:{self.snapshot}?mode=ro", uri=True)
        try:
            out: dict[str, int] = {}
            for table in (
                "jobs",
                "items",
                "calls",
                "attempts",
                "workers",
                "query_types",
                "endpoint_state",
            ):
                out[table] = connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            return out
        finally:
            connection.close()

    # -- step 0 ---------------------------------------------------------

    def step_0_run_directory(self, step: Step) -> None:
        """FIND THE RUN DIRECTORY FIRST, because the bundle depends on it.

        ``council_bundle.build_bundle_from_run`` needs ``PLAN.json`` and the
        ``abstain_file`` / ``fanout_file`` it names. Those two files are what
        make the clearinghouse key set recoverable: one council item can fan
        out to several ``query_id``s and a retrieval abstention has NO council
        item at all, so neither the submitted set nor the key set is derivable
        from the council projection. /scratch on this host is ephemeral and
        is not backed up, so the directory's location is SEARCHED and the
        search is reported rather than assumed.
        """
        searched: list[str] = []
        candidates: list[dict[str, Any]] = []
        for root in self.run_dir_roots:
            base = Path(root).expanduser()
            searched.append(str(base))
            if not base.is_dir():
                continue
            for plan_file in sorted(base.rglob("PLAN.json")):
                try:
                    plan = json.loads(plan_file.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001 - a bad plan is not a plan
                    continue
                if not isinstance(plan, Mapping):
                    continue
                candidates.append(
                    {
                        "path": str(plan_file),
                        "query": plan.get("query"),
                        "counts": plan.get("counts"),
                    }
                )
        step.note(f"roots searched: {searched}")
        step.note(
            f"PLAN.json files found: {len(candidates)}; of those, "
            f"{sum(1 for e in candidates if str(e.get('query') or '') == CAMPAIGN_QUERY)} "
            f"name query {CAMPAIGN_QUERY}"
        )
        matches = [
            entry
            for entry in candidates
            if str(entry.get("query") or "") == CAMPAIGN_QUERY
            and int(((entry.get("counts") or {}) or {}).get("submit") or 0) > 10000
        ]
        if not matches:
            step.not_asserted(
                f"run directory absent: {searched}. No PLAN.json on this host "
                f"describes the {CAMPAIGN_QUERY} ADP1+PMI campaign "
                f"({len(candidates)} PLAN.json files found)."
            )
            return
        if len(matches) > 1:
            step.find(
                f"{len(matches)} run directories name this campaign: "
                f"{[m['path'] for m in matches]}. The first was used. Two "
                "plans for one campaign could declare different parameter "
                "sets, and the parameter_set_hash is the partition key."
            )
        self.run_dir = Path(matches[0]["path"]).parent
        plan = json.loads((self.run_dir / "PLAN.json").read_text(encoding="utf-8"))
        step.check(
            "a run directory for the campaign exists and its PLAN.json names "
            f"query {CAMPAIGN_QUERY}",
            CAMPAIGN_QUERY,
            str(plan.get("query")),
            evidence=f"run_dir={self.run_dir}",
        )
        step.check(
            "the plan declares both record files, and BOTH are present on "
            "disk -- /scratch is ephemeral, so this is the check that matters",
            [True, True],
            [
                Path(str(plan.get("abstain_file") or "")).is_file(),
                Path(str(plan.get("fanout_file") or "")).is_file(),
            ],
            evidence=(
                f"abstain_file={plan.get('abstain_file')} "
                f"fanout_file={plan.get('fanout_file')}"
            ),
        )

        fanout = [
            json.loads(raw)
            for raw in Path(str(plan["fanout_file"])).read_text(
                encoding="utf-8"
            ).splitlines()
            if raw.strip()
        ]
        abstain = [
            json.loads(raw)
            for raw in Path(str(plan["abstain_file"])).read_text(
                encoding="utf-8"
            ).splitlines()
            if raw.strip()
        ]
        declared = dict(plan.get("counts") or {})
        step.check(
            "the record files hold exactly the row counts the plan declares",
            {"fanout": int(declared.get("fanout") or -1),
             "abstain": int(declared.get("abstain") or -1)},
            {"fanout": len(fanout), "abstain": len(abstain)},
        )
        step.check(
            "the fanout maps 19,276 distinct query_ids onto 19,276 distinct "
            "item_ids -- this campaign's fanout is 1:1, so no text is shared",
            (19276, 19276, 19276),
            (
                len(fanout),
                len({str(r["query_id"]) for r in fanout}),
                len({str(r["item_id"]) for r in fanout}),
            ),
        )
        step.check(
            "the two partitions are DISJOINT on query_id -- an overlap would "
            "mean the files came from different plans",
            0,
            len(
                {str(r["query_id"]) for r in fanout}
                & {str(r["query_id"]) for r in abstain}
            ),
        )
        step.check(
            "EVERY abstain row carries its function text, so no abstention "
            "will be published with an empty, permanently-frozen text cell",
            760,
            sum(1 for r in abstain if str(r.get("text") or "").strip()),
            evidence=f"{len(abstain)} abstain rows, all with text",
        )
        step.check(
            "the plan carries the parameter set and its hash -- the partition "
            "key, which is not derivable from the archive",
            True,
            bool(plan.get("parameter_set_hash")) and bool(plan.get("parameter_set")),
            evidence=(
                f"parameter_set_hash={plan.get('parameter_set_hash')} "
                f"mapping_hash={plan.get('mapping_hash')}"
            ),
        )
        step.note(
            "the campaign's SUBMITTED inventory is therefore 19,276 fanout + "
            "760 abstentions = 20,036 distinct query_ids, which is every "
            "distinct entity_hash in priority_adp1_pmi.tsv."
        )
        step.find(
            "THIS PLAN WAS NOT WRITTEN BY STAGE 1 and a reader should know "
            "it. `OntologyMappingUtils.council_plan` writes ~25 keys; this "
            f"file carries {len(plan)} ({sorted(plan)}), missing run_dir, "
            "run_id, generated_at, plan_hash, chunks, items_file, "
            "ranked_file, concurrency, transport_allocation, mapping_version, "
            "unrun_bundle_file and counts.universe/done/unrun. It was "
            "hand-assembled on 2026-10-23 to drive the run-directory export "
            "and publish (MANIFEST.toml, PUBLISH.json and shard/ sit beside "
            "it), because the 2026-10-19 campaign itself was driven by "
            "`kbu submit --query go_mf@4 --input chunkNN.jsonl` from "
            "run_chunks.sh and predates stage 1 entirely. CONSEQUENCE, "
            "measured not assumed: build_bundle_from_run reads only `query`, "
            "`mapping_hash`, `parameter_set`, `parameter_set_hash`, "
            "`plan_hash`, `generated_at` and the two record files, so it "
            "works -- but the bundle's `plan` block, and therefore the "
            "PLAN.json the publisher re-writes beside its output, carries a "
            "plan that no stage-1 run would produce, and its `plan_hash` is "
            "the empty string."
        )
        step.find(
            "THE FANOUT'S query_id KEY IS CORROBORATED BUT NOT INDEPENDENT. "
            "Measured: all 19,276 fanout query_ids and all 760 abstain "
            "query_ids are entity_hashes in priority_adp1_pmi.tsv (20,036 "
            "distinct, and definition -> entity_hash is 1:1 there with zero "
            "ambiguity); every fanout item_id exists in the twelve published "
            "jobs; and the fanout's (query_id, item_id) pairing agrees with "
            "the text join store-item-text -> TSV-definition -> entity_hash "
            "for 19,276 of 19,276 rows with 0 disagreements. That text join "
            "is ALSO how the 2026-10-22 verdict export resolved its "
            "clearinghouse_entity_hash "
            "(/scratch/chenry/council/export_results.py). So step 7's KEY is "
            "common to both sides and is not independently corroborated by "
            "it; what step 7 does independently check is the VERDICT CONTENT "
            "(go_ids, decided_by) produced by two different folds -- the "
            "publisher's bundle-ordered verdicts_by_item versus "
            "export_results.py's item_order scan."
        )
        step.find(
            "THE RESULTS MANIFEST MISDESCRIBES THE 760. "
            "2026-10-19-clearinghouse/council-results/MANIFEST.json records "
            "`in_priority_list_but_NEVER_submitted: 760` with the note "
            "'definitions that were not in the submitted set'. They are in "
            "fact the plan's RETRIEVAL ABSTENTIONS -- declined before "
            "submission with a recorded abstain_reason, 'placeholder "
            "function: ...' and the like -- which is a materially different "
            "statement: an abstention is a published verdict with "
            "council=null and a decline reason, not a gap. 19,276 + 760 = "
            "20,036 = the TSV's full distinct-definition count, so there is "
            "NO unexplained residue at all."
        )

    # -- step 1 ---------------------------------------------------------

    def step_1_bootstrap(self, step: Step) -> None:
        """Bootstrap the council namespace into a SCRATCH dataset."""
        # IMPORTED FOR ITS SIDE EFFECT, and that is the documented
        # contract: council_bundle registers publication_bundle with
        # council_sink.register_table AT IMPORT, so a sink built before the
        # module was imported simply does not carry the table. The rehearsal
        # publishes a bundle, so it must be registered before bootstrap.
        from ...genome.ontology_mapping import council_bundle  # noqa: F401
        from .council_sink import SqliteCouncilSink, column_names, table_configs

        path = require_scratch_target(self.sink_path, what="scratch sink")
        if path.exists():
            path.unlink()
        self.sink = SqliteCouncilSink(path)
        declared = [str(config["name"]) for config in table_configs()]
        report = self.sink.bootstrap()

        step.check(
            "bootstrap creates every declared council table",
            sorted(declared),
            sorted(report["tables"]),
        )
        missing = [table for table in declared if not self.sink.table_exists(table)]
        step.check(
            "every declared table exists in the scratch dataset after bootstrap",
            [],
            missing,
            evidence=f"namespace={self.sink.namespace!r} file={path}",
        )
        drift = {
            table: {
                "declared": list(column_names(table)),
                "created": list(self.sink.catalog_columns(table)),
            }
            for table in declared
            if tuple(self.sink.catalog_columns(table)) != tuple(column_names(table))
        }
        step.check(
            "each created table's columns, READ BACK out of the database, "
            "equal its declaration",
            {},
            drift,
        )
        again = self.sink.bootstrap()
        step.check(
            "bootstrap is idempotent -- a second call leaves the same tables",
            sorted(declared),
            sorted(again["tables"]),
        )
        step.check(
            "the dataset is a SCRATCH file and is not the production store",
            True,
            str(path) not in PRODUCTION_STORE_PATHS
            and str(path).startswith(SCRATCH_PREFIXES),
            evidence=str(path),
        )
        step.note(
            "publication_bundle is present because council_bundle registers "
            "it with council_sink.register_table at import, not because "
            "council_schema declares it -- it is a per-campaign artifact, "
            "not part of a snapshot's archive."
        )

    # -- step 2 ---------------------------------------------------------

    def step_2_identity_and_delivery(self, step: Step) -> None:
        """Assign identity, stage a manifest-v1 delivery, run the SCANNER."""
        from ...ai.council.store.sqlite import StoreIdentityAlreadyDeclared
        from .council_archive import require_complete_delivery
        from .council_delivery import (
            MANIFEST_FILENAME,
            CouncilDeliveryScanner,
            build_manifest,
            manifest_digest,
            read_manifest,
            write_manifest,
        )

        # A FRESH COPY. assign_store_identity is one-way, so it runs only here.
        store_dir = require_scratch_target(self.work_root / "store", what="store copy")
        store_dir.mkdir(parents=True, exist_ok=True)
        self.store_copy = require_scratch_target(
            store_dir / "council-rehearsal.sqlite", what="store copy"
        )
        if self.store_copy.exists():
            self.store_copy.unlink()
        shutil.copyfile(self.snapshot, self.store_copy)
        step.check(
            "the identity target is a fresh copy, not the shared snapshot or "
            "the production store",
            True,
            str(self.store_copy) not in PRODUCTION_STORE_PATHS
            and self.store_copy != self.snapshot,
            evidence=f"copied {self.snapshot} -> {self.store_copy}",
        )

        store = self._store(self.store_copy)
        try:
            step.check(
                "the production snapshot carries NO declared archive identity "
                "before this rehearsal assigns one",
                None,
                store.store_identity(),
            )
            assigned = store.assign_store_identity(
                self.store_id, declared_by=self.declared_by
            )
            step.check(
                "assign_store_identity declares exactly the id it was given",
                self.store_id,
                assigned["store_id"],
                evidence=f"declared_by={assigned['declared_by']!r}",
            )
            step.check(
                "the identity reads back out of the store file itself",
                self.store_id,
                (store.store_identity() or {}).get("store_id"),
            )
            repeat = store.assign_store_identity(
                self.store_id, declared_by=self.declared_by
            )
            step.check(
                "re-declaring the SAME id writes nothing (idempotent)",
                assigned["declared_seq"],
                repeat["declared_seq"],
            )
            refused = None
            try:
                store.assign_store_identity("rehearsal/not-this-one")
            except StoreIdentityAlreadyDeclared as exc:
                refused = type(exc).__name__
            step.check(
                "declaring a DIFFERENT id over an existing one RAISES rather "
                "than replacing it",
                "StoreIdentityAlreadyDeclared",
                refused,
            )

            drop = require_scratch_target(
                self.work_root / "drop" / "delivery-seq1", what="delivery drop"
            )
            if drop.exists():
                shutil.rmtree(drop)
            drop.mkdir(parents=True, exist_ok=True)
            taken = store.snapshot(drop / "council-snapshot.sqlite")
        finally:
            store.close()

        self.snapshot_id = str(taken["snapshot_id"])
        step.check(
            "SqliteStore.snapshot() reports a VERIFIED snapshot_id over the "
            "bytes it wrote",
            64,
            len(self.snapshot_id),
            ok=len(self.snapshot_id) == 64,
            evidence=f"snapshot_id={self.snapshot_id} bytes={taken['bytes']:,}",
        )
        step.note(
            "the rehearsal's snapshot_id is NOT the production snapshot's "
            "37535e28...: assigning the identity added a store_identity row, "
            "so these are deliberately different bytes. Keying archived rows "
            "on the verified digest of the file actually delivered is the "
            "point."
        )

        manifest = build_manifest(
            drop,
            delivery_id=f"rehearsal-{self.snapshot_id[:12]}-seq1",
            store_id=self.store_id,
            snapshot_id=self.snapshot_id,
            snapshot_file="council-snapshot.sqlite",
            snapshot_seq=1,
            snapshot_schema_version=2,
            store_host=str(taken.get("store_host") or ""),
            deadline=timedelta(hours=6),
        )
        write_manifest(manifest, drop / MANIFEST_FILENAME)
        reread = read_manifest(drop / MANIFEST_FILENAME)
        step.check(
            "the delivery manifest is COUNCIL-DELIVERY v1",
            1,
            int(reread["schema_version"]),
        )
        step.check(
            "the manifest round-trips through read_manifest with the same "
            "delivery identity digest",
            manifest_digest(manifest),
            manifest_digest(reread),
        )
        step.check(
            "the manifest carries store_host, so an archived row keeps where "
            "the snapshot was taken",
            True,
            bool(reread.get("store_host")),
            evidence=f"store_host={reread.get('store_host')!r}",
        )
        step.check(
            "the manifest's inventory is MEASURED: the snapshot entry's "
            "sha256 equals the snapshot_id",
            self.snapshot_id,
            next(
                entry["sha256"]
                for entry in reread["files"]
                if entry["path"] == "council-snapshot.sqlite"
            ),
        )

        self.state_root = self.work_root / "archive-state"
        scanner = CouncilDeliveryScanner(
            drop_dir=self.work_root / "drop",
            scratch_dir=self.work_root / "archive-scratch",
            sink=self.sink,
            state_root=self.state_root,
            holder="acceptance-rehearsal",
        )
        tick = scanner.tick()
        step.check(
            "the scanner ENUMERATES the drop directory and finds the delivery",
            1,
            int(tick["deliveries"]),
        )
        [outcome] = tick["reports"]
        step.check(
            "the delivery reconciles to COMPLETE through the scanner",
            "complete",
            outcome["state"],
            evidence=f"stages={sorted(k for k in outcome if k in ('archive', 'publish'))}",
        )
        step.check(
            "the archive stage settled ok",
            "ok",
            str((outcome.get("archive") or {}).get("state")),
            evidence=str((outcome.get("archive") or {}).get("detail") or ""),
        )
        step.check(
            "the publish stage is NOT APPLICABLE -- never 'ok' -- because "
            "this delivery declares no publication bundle",
            "not_applicable",
            str((outcome.get("publish") or {}).get("state")),
        )
        completion = require_complete_delivery(
            self.sink,
            store_id=self.store_id,
            snapshot_id=self.snapshot_id,
            state_root=self.state_root,
        )
        step.check(
            "the archiver's load ledger says the delivery is COMPLETE",
            True,
            bool(completion["complete"]),
        )
        unreconciled = {
            table: detail
            for table, detail in completion["tables"].items()
            if not detail.get("reconciled")
        }
        step.check(
            "every archiver table reconciled its expected row count",
            {},
            unreconciled,
            evidence=json.dumps(
                {
                    table: detail.get("observed_rows")
                    for table, detail in completion["tables"].items()
                },
                sort_keys=True,
            ),
        )
        step.check(
            "job_disposition is declared NOT WRITTEN by the archiver -- "
            "selection is a separate layer",
            ["job_disposition"],
            list(completion.get("not_written_here") or []),
        )
        step.note(
            "driven through CouncilDeliveryScanner.tick(), not by calling "
            "archive_snapshot directly: the rail is what is being rehearsed."
        )

    # -- step 3 ---------------------------------------------------------

    def step_3_archive_by_direct_query(self, step: Step) -> None:
        """Assert the archive BY DIRECT QUERY -- not by any exit code."""
        from .council_schema import ROLE_BRIEF_UNEXTRACTABLE

        source = self._source_counts()
        where = {"store_id": self.store_id, "snapshot_id": self.snapshot_id}
        count = lambda table, **extra: self.sink.count_rows(  # noqa: E731
            table, where={**where, **extra}
        )

        step.check("21 jobs are present in the archive", 21, count("jobs"))
        step.check(
            "the archived jobs count equals the store's",
            source["jobs"],
            count("jobs"),
        )

        calls_total = count("calls")
        live = count("calls", live=1)
        not_live = count("calls", live=0)
        step.check(
            "the archived calls row count is the store's FULL 203,821",
            203821,
            calls_total,
        )
        step.check(
            "the archived calls count equals the store's, measured "
            "independently of the archiver",
            source["calls"],
            calls_total,
        )
        step.check(
            "the archived calls count is NOT the live subset",
            True,
            calls_total != live,
            evidence=f"total={calls_total:,} live={live:,}",
        )
        step.check(
            "the live / not-live split is recoverable PER ROW and partitions "
            "the table",
            calls_total,
            live + not_live,
            evidence=f"live={live:,} not_live={not_live:,}",
        )
        step.check(
            "the live subset is 88,465 over all 21 jobs, as measured in "
            "tranche 1",
            88465,
            live,
        )
        per_role = {
            f"{role}/live={flag}": count("calls", role=role, live=flag)
            for role in ("proposer", "critic", "meta")
            for flag in (0, 1)
        }
        step.check(
            "the live split is recoverable per role",
            {
                "critic/live=0": 23,
                "critic/live=1": 20368,
                "meta/live=0": 0,
                "meta/live=1": 4951,
                "proposer/live=0": 115333,
                "proposer/live=1": 63146,
            },
            per_role,
        )

        published = list(CHUNK_JOB_IDS) + list(RETRY_JOB_IDS)
        live_published = sum(
            self.sink.count_rows(
                "calls", where={**where, "live": 1, "job_id": job_id}
            )
            for job_id in published
        )
        step.check(
            "the PRD's 82,270 is the live subset of the TWELVE PUBLISHED "
            "JOBS, and it reconciles exactly",
            82270,
            live_published,
        )
        step.find(
            "THE 82,270 vs 88,465 DISAGREEMENT IS RESOLVED, and BOTH numbers "
            "are correct for different populations. The task header states "
            "'LIVE CALLS ARE 88,465, NOT 82,270' and that the PRD's figure "
            "'does not match this snapshot'. Measured against this snapshot: "
            "`SELECT COUNT(*) FROM calls WHERE live=1` is 88,465 over all 21 "
            "jobs, and `... AND job_id IN (<the 12 published>)` is 82,270 "
            "EXACTLY. So 88,465 is the whole store's live subset and 82,270 "
            "is the published campaign's -- the difference, 6,195, is the "
            "trials and pilots and the two cancelled look-alikes. Neither "
            "figure is wrong; the PRD and the header are counting different "
            "things. The header's instruction to report the PRD's figure as "
            "a mismatch is therefore the thing to correct at source."
        )

        by_status = {
            status: count("items", status=status)
            for status in ("ok", "error", "pending", "cancelled")
        }
        step.check(
            "archived items by status match the store exactly",
            {"ok": 20125, "error": 627, "pending": 242, "cancelled": 38499},
            by_status,
        )
        step.check(
            "the archived items total equals the store's",
            source["items"],
            count("items"),
        )
        step.check(
            "the archived attempts total equals the store's",
            source["attempts"],
            count("attempts"),
        )

        workers = self.sink.select_rows("workers", where=where)
        step.check(
            "workers rows are present",
            source["workers"],
            len(workers),
        )
        step.check(
            "workers.version is preserved as NULL -- not defaulted, not "
            "dropped",
            0,
            sum(1 for row in workers if row.get("version") is not None),
            evidence=f"{len(workers)} worker rows, every version IS NULL",
        )

        briefs = self.sink.select_rows("role_brief", where=where)
        step.check(
            "role_brief is populated for every (query_name, query_version) "
            "in query_types",
            15,
            len(briefs),
            evidence="5 query versions x 3 roles (proposer, critic, meta)",
        )
        step.check(
            "query_types count equals the store's",
            source["query_types"],
            count("query_types"),
        )
        unextractable = [
            (row["query_name"], row["query_version"], row["role"])
            for row in briefs
            if str(row.get("mission_text")) == ROLE_BRIEF_UNEXTRACTABLE
        ]
        step.check(
            "no role_brief mission_text is recorded unextractable -- every "
            "spec in this store carries a readable system prompt per role",
            [],
            unextractable,
        )
        step.check(
            "every role_brief carries a non-empty answer_schema",
            0,
            sum(1 for row in briefs if not str(row.get("answer_schema") or "")),
        )

        endpoints = self.sink.select_rows("endpoint_state", where=where)
        step.check(
            "endpoint_state is archived -- the ONLY per-gateway token record "
            "that exists anywhere",
            source["endpoint_state"],
            len(endpoints),
            evidence=json.dumps(
                {
                    str(row["endpoint"]): int(row["lifetime_tokens"])
                    for row in endpoints
                },
                sort_keys=True,
            ),
        )

        # reply_parsed: the trap named in the header, measured on the archive.
        connection = sqlite3.connect(f"file:{self.snapshot}?mode=ro", uri=True)
        try:
            not_null = connection.execute(
                "SELECT COUNT(*) FROM calls WHERE reply_parsed IS NOT NULL"
            ).fetchone()[0]
            json_null = connection.execute(
                "SELECT COUNT(*) FROM calls WHERE reply_parsed = 'null'"
            ).fetchone()[0]
            reply_text = connection.execute(
                "SELECT COUNT(*) FROM calls WHERE reply_text IS NOT NULL"
            ).fetchone()[0]
        finally:
            connection.close()
        step.check(
            "calls.reply_parsed IS NOT NULL is true for EVERY row and "
            "therefore means nothing",
            calls_total,
            not_null,
        )
        step.check(
            "the real absent-parse count comes from the TEXT 'null'",
            116445,
            json_null,
            evidence=(
                f"{calls_total:,} rows, {json_null:,} are the text 'null', "
                f"so {calls_total - json_null:,} carry a real parsed reply"
            ),
        )
        step.find(
            "calls.reply_parsed STORES PYTHON None AS THE TEXT 'null', so "
            "`reply_parsed IS NOT NULL` is true for all 203,821 rows and is "
            "a vacuous filter. Measured: IS NOT NULL = 203,821; = 'null' = "
            "116,445; IS NULL = 0. Any consumer counting parsed replies with "
            "IS NOT NULL overcounts by 116,445. council_archive's "
            "role_brief_rows already handles the same trap for query_types."
            "spec and says so in its docstring; nothing warns a consumer of "
            "calls."
        )
        step.find(
            "THE SNAPSHOT MANIFEST'S reply_text COUNT IS OFF BY 44. "
            "~/Dropbox/Jobs/function-to-go/_store-backups/MANIFEST.json "
            "records 'the raw reply text (87,507 rows)'; measured on the "
            f"snapshot it names, `reply_text IS NOT NULL` is {reply_text:,}. "
            "Cosmetic -- the manifest's own content counts (203,821 calls / "
            "59,493 items / 21 jobs) all reconcile exactly -- but the "
            "WHY_THIS_MATTERS prose figure does not."
        )

    # -- step 4 ---------------------------------------------------------

    def step_4_dispositions(self, step: Step) -> None:
        """Seed dispositions from the provenance JSON. 12 published, 9 excluded."""
        import hashlib

        from .council_disposition import (
            DispositionLedger,
            seed_from_provenance,
            write_dispositions,
        )

        fixture = (
            Path(__file__).resolve().parents[4].parent
            / "tests"
            / "berdl"
            / "fixtures"
            / "AUTHORITATIVE-JOB-PROVENANCE.json"
        )
        digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()  # noqa: E731
        if fixture.is_file():
            step.check(
                "the live provenance JSON and the committed test fixture are "
                "BYTE-IDENTICAL",
                digest(self.provenance),
                digest(fixture),
                evidence=f"live={self.provenance} fixture={fixture}",
            )
        else:
            step.note(
                f"the committed fixture was not found at {fixture}; the live "
                "file was used and the two could not be compared"
            )
        step.note(f"seeded from the LIVE file: {self.provenance}")

        ledger_path = self.work_root / "disposition" / "declarations.jsonl"
        if ledger_path.exists():
            ledger_path.unlink()
        self.ledger = DispositionLedger(ledger_path)
        seeded = seed_from_provenance(
            self.ledger, self.provenance, declared_by=self.declared_by
        )

        step.check(
            "the provenance declares all 21 jobs",
            21,
            int(seeded["jobs"]),
        )
        step.check(
            "12 jobs are declared published",
            12,
            int(seeded["counts"]["published"]),
        )
        step.check(
            "9 jobs are declared excluded",
            9,
            len(seeded["excluded"]),
            evidence=json.dumps(seeded["counts"], sort_keys=True),
        )
        step.check(
            "the 12 published ids are the ten chunk jobs plus the two retry "
            "passes",
            sorted(set(CHUNK_JOB_IDS) | set(RETRY_JOB_IDS)),
            sorted(seeded["published"]),
        )
        step.check(
            "re-seeding the same file changes nothing",
            {"added": 0, "changed": 0, "unchanged": 21},
            {
                key: len(value)
                for key, value in seed_from_provenance(
                    self.ledger, self.provenance, declared_by=self.declared_by
                ).items()
                if key in ("added", "changed", "unchanged")
            },
        )

        write_dispositions(
            self.sink,
            self.ledger,
            store_id=self.store_id,
            snapshot_id=str(self.snapshot_id),
            snapshot_seq=1,
            store_host="poplar.cels.anl.gov",
            load_batch_id=f"rehearsal-disposition-{str(self.snapshot_id)[:12]}",
        )
        where = {"store_id": self.store_id, "snapshot_id": self.snapshot_id}
        rows = self.sink.select_rows("job_disposition", where=where)
        step.check(
            "BY DIRECT QUERY on job_disposition: 12 published",
            12,
            sum(1 for row in rows if str(row["disposition"]) == "published"),
        )
        step.check(
            "BY DIRECT QUERY on job_disposition: 9 excluded",
            9,
            sum(1 for row in rows if str(row["disposition"]) != "published"),
        )
        step.check(
            "one job_disposition row per job -- the table's declared primary "
            "key (store_id, snapshot_id, job_id) permits no more",
            21,
            len({str(row["job_id"]) for row in rows}),
            ok=len(rows) == 21 == len({str(row["job_id"]) for row in rows}),
            evidence=f"{len(rows)} rows",
        )

    # -- steps 5, 6, 7b -------------------------------------------------

    def _needs_run_dir(self, step: Step, leg: str) -> bool:
        """Declare *step* unassertable when step 0 found no run directory."""
        if self.run_dir is not None:
            return False
        step.not_asserted(
            f"{leg} depends on the campaign's publication bundle, which "
            "build_bundle_from_run builds from the run directory's PLAN.json "
            "and the two record files it names. Step 0 found no run "
            "directory for this campaign, so there is nothing to build from. "
            "Re-run stage 1 (`kbu ontomap council-plan`, which spends no "
            "tokens) to author one."
        )
        return True

    def _needs_bundle(self, step: Step, leg: str) -> bool:
        if self.bundle is not None:
            return False
        step.not_asserted(
            f"{leg} needs the campaign's publication bundle and step 5 did "
            "not produce one. See step 5."
        )
        return True

    # -- step 5 ---------------------------------------------------------

    def step_5_bundle(self, step: Step) -> None:
        """Produce the publication bundle and assert its fanout reconstructs
        every query_id in the submitted inventory."""
        from ...genome.ontology_mapping.council_bundle import (
            BUNDLE_FILENAME,
            BundleConflictError,
            JobNotInBundleError,
            abstentions,
            build_bundle_from_run,
            bundle_query_ids,
            content_digest,
            fanout_map,
            ordered_job_ids,
            publish_bundle,
            read_bundle,
            read_lakehouse_bundle,
            require_publishable,
            write_bundle,
        )

        if self._needs_run_dir(step, "bundle production"):
            return

        bundle = build_bundle_from_run(
            self.run_dir,
            campaign_id=CAMPAIGN_ID,
            store_id=self.store_id,
            snapshot_id=str(self.snapshot_id),
            job_ids=list(CHUNK_JOB_IDS),
            retry_job_ids=list(RETRY_JOB_IDS),
            written_by=self.declared_by,
        )
        self.bundle = bundle

        submitted = [str(q) for q in bundle_query_ids(bundle)]
        pairs = fanout_map(bundle)
        declines = abstentions(bundle)
        step.check(
            "the bundle's SUBMITTED inventory is the fanout plus the "
            "abstentions, on DISTINCT query_ids",
            (20036, 20036),
            (len(submitted), len(set(submitted))),
            evidence="19,276 fanout + 760 abstentions",
        )
        step.check(
            "the fanout reconstructs every query_id in the submitted "
            "inventory that has a council item",
            19276,
            len(pairs),
        )
        step.check(
            "every fanout query_id maps to an item_id, and the map recovers "
            "the clearinghouse key set from the council record",
            0,
            sum(1 for query_id, item_id in pairs.items() if not query_id or not item_id),
        )
        step.check(
            "every abstention is carried WITH its decline reason and with "
            "item_id present-and-NULL -- not absent",
            (760, 760, 760),
            (
                len(declines),
                sum(1 for row in declines if row.get("decline_reason")),
                sum(1 for row in declines if "item_id" in row and row["item_id"] is None),
            ),
        )
        step.check(
            "the bundle's declared counts agree with its own records",
            {"fanout": 19276, "abstained": 760, "inputs": 20036, "items": 19276},
            dict((bundle.get("submitted") or {}).get("counts") or {}),
        )
        step.check(
            "WINNERS RESOLVE BY DECLARED ORDER: the ten chunk jobs first, "
            "then the two retry jobs LAST, preserved exactly as declared",
            list(CHUNK_JOB_IDS) + list(RETRY_JOB_IDS),
            list(ordered_job_ids(bundle)),
        )
        step.check(
            "the bundle is bound to THIS store and THIS snapshot",
            (self.store_id, str(self.snapshot_id)),
            (str(bundle["store_id"]), str(bundle["snapshot_id"])),
        )
        step.check(
            "the bundle's declared digest covers its own content",
            content_digest(bundle),
            str(bundle["bundle_digest"]),
        )

        bundle_dir = require_scratch_target(
            self.work_root / "bundle", what="bundle directory"
        )
        bundle_dir.mkdir(parents=True, exist_ok=True)
        bundle_path = bundle_dir / BUNDLE_FILENAME
        if bundle_path.exists():
            bundle_path.unlink()
        written = write_bundle(bundle, bundle_path)
        step.check(
            "the on-disk bundle round-trips through read_bundle with its "
            "digest VERIFIED",
            str(bundle["bundle_digest"]),
            str(read_bundle(bundle_path)["bundle_digest"]),
            evidence=f"{written['bytes']:,} bytes at {bundle_path}",
        )
        rewrite = write_bundle(bundle, bundle_path)
        step.check(
            "an identical rewrite is a no-op by content",
            False,
            bool(rewrite["written"]),
        )
        conflicting = {**bundle, "campaign_id": CAMPAIGN_ID + "-other"}
        conflicting["bundle_digest"] = content_digest(conflicting)
        refused = None
        try:
            write_bundle(conflicting, bundle_path)
        except BundleConflictError as exc:
            refused = type(exc).__name__
        step.check(
            "a DIFFERENT bundle for the same path is a CONFLICT to reject, "
            "never an update -- the clearinghouse slot is frozen on first "
            "write",
            "BundleConflictError",
            refused,
        )

        published = publish_bundle(
            self.sink, bundle, archive_state_root=self.state_root
        )
        from_lake = read_lakehouse_bundle(self.sink, CAMPAIGN_ID)
        step.check(
            "the lakehouse copy agrees with the on-disk one BY CONTENT DIGEST",
            str(bundle["bundle_digest"]),
            str(from_lake["bundle_digest"]),
            evidence=json.dumps(
                {k: v for k, v in published.items() if k != "bundle"},
                default=str,
                sort_keys=True,
            )[:280],
        )
        step.check(
            "publish_bundle's lakehouse row names this campaign and this "
            "store -- it passed require_complete_delivery on the way in",
            (CAMPAIGN_ID, self.store_id),
            (
                str(from_lake["campaign_id"]),
                str(from_lake["store_id"]),
            ),
        )

        step.check(
            "all twelve published jobs pass BOTH gates -- bundle-listed AND "
            "disposition published -- and require_publishable returns them",
            sorted(set(CHUNK_JOB_IDS) | set(RETRY_JOB_IDS)),
            sorted(
                require_publishable(
                    self.sink,
                    bundle,
                    list(CHUNK_JOB_IDS) + list(RETRY_JOB_IDS),
                    snapshot_id=str(self.snapshot_id),
                )
            ),
            evidence="council_bundle.require_publishable, the one two-gate test",
        )
        cancelled = "477d6c53cf3b4547b2a4b750aabe5adf"
        refused = None
        try:
            require_publishable(
                self.sink, bundle, [cancelled], snapshot_id=str(self.snapshot_id)
            )
        except JobNotInBundleError as exc:
            refused = type(exc).__name__
        step.check(
            "the cancelled 19,276-item look-alike is REFUSED by the bundle "
            "gate -- it is authoritative by label, by size and by status",
            "JobNotInBundleError",
            refused,
            evidence=f"job {cancelled}",
        )

    # -- step 6 ---------------------------------------------------------

    def step_6_publish(self, step: Step) -> None:
        """Publish into the SCRATCH clearinghouse. BOTH halves.

        6a is the DEFAULT-FLOOR REFUSAL and 6b is the operator's deliberate
        publish at an explicit floor. Both are rehearsed because the real
        republish IS that two-step: the campaign's coverage is below 1.0 by
        eight items, so the shipped default refuses it BY DESIGN and lowering
        the floor is an explicit act, not a configuration detail.
        """
        from .clearinghouse_ingest import (
            DEFAULT_COVERAGE_FLOOR,
            ClearinghouseJoinAssertionError,
        )
        from ...genome.ontology_mapping.council_bundle import bundle_query_ids
        from ...genome.ontology_mapping.verdicts import COUNCIL_REFERENCE_FIELDS
        from .council_publish import (
            FROZEN_RESULT_TYPE,
            RESULT_TYPE,
            publish_from_lakehouse,
            resolvable_query_ids,
            select_verdicts,
        )

        if self._needs_bundle(step, "publication into the scratch dataset"):
            return
        bundle = self.bundle
        assert bundle is not None

        # The abstention texts, from the plan's OWN abstain record -- the
        # only place they exist. An abstention has no council item, so its
        # function_text is NOT in the lakehouse, and the clearinghouse slot
        # is frozen on first write: an empty text published once stays empty.
        plan = dict(bundle.get("plan") or {})
        texts = {
            str(row["query_id"]): str(row.get("text") or "")
            for raw in Path(str(plan["abstain_file"])).read_text(
                encoding="utf-8"
            ).splitlines()
            if raw.strip()
            for row in (json.loads(raw),)
        }
        step.check(
            "a function text is sourced for EVERY abstention, from the plan's "
            "abstain record",
            760,
            sum(1 for value in texts.values() if value.strip()),
        )

        selection = select_verdicts(
            self.sink, bundle, state_root=self.state_root
        )
        resolvable = resolvable_query_ids(bundle, selection["verdicts"])
        submitted = {str(q) for q in bundle_query_ids(bundle)}
        unresolved_expected = sorted(submitted - resolvable)
        step.check(
            "the campaign's coverage is below 1.0 by exactly EIGHT query_ids",
            8,
            len(unresolved_expected),
            evidence=(
                f"{len(resolvable):,} resolvable of {len(submitted):,} "
                f"submitted = coverage {len(resolvable) / len(submitted):.6f}"
            ),
        )

        # -- 6a: the DEFAULT FLOOR REFUSES -------------------------------
        out_a = require_scratch_target(
            self.work_root / "publish-default-floor", what="publish dir"
        )
        if out_a.exists():
            shutil.rmtree(out_a)
        out_a.mkdir(parents=True, exist_ok=True)
        step.check(
            "the shipped default coverage floor is 1.0",
            1.0,
            float(DEFAULT_COVERAGE_FLOOR),
        )
        refusal = None
        receipt_a = None
        try:
            receipt_a = publish_from_lakehouse(
                self.sink,
                bundle,
                publish_dir=out_a,
                state_root=self.state_root,
                texts=texts,
            )
        except ClearinghouseJoinAssertionError as exc:
            refusal = str(exc)
        step.check(
            "6a: publishing at the DEFAULT floor REFUSES -- it raises rather "
            "than publishing a campaign that is eight items short",
            "ClearinghouseJoinAssertionError",
            "ClearinghouseJoinAssertionError" if refusal else type(receipt_a),
            ok=refusal is not None,
            evidence=(refusal or "")[:400],
        )
        step.check(
            "6a: NO shard directory was written",
            False,
            (out_a / "shard").exists(),
        )
        step.check(
            "6a: no verdict parquet was written either -- the guard runs "
            "BEFORE the join and before anything is written",
            False,
            (out_a / "verdicts.parquet").exists(),
        )
        step.check(
            "6a: the refusal reports the eight as a COUNT it can be checked "
            "against",
            True,
            "8 key(s) name no entity" in (refusal or ""),
            evidence="from join_assertions' coverage failure message",
        )
        step.find(
            "THE DEFAULT-FLOOR REFUSAL DOES NOT NAME THE EIGHT, it only "
            "counts them. clearinghouse_ingest.join_assertions builds its "
            "coverage failure from counts (`resolved_keys`, `source_keys`, "
            "`unresolved_keys`) and its report carries no list of the "
            "unresolved keys, so the operator who hits the refusal is told "
            "'8 key(s) name no entity the clearinghouse holds' and cannot "
            "see WHICH eight without lowering the floor and reading the 6b "
            "receipt's `unresolved_query_ids`. The task header states the "
            "default-floor publish 'REFUSES and names the eight'; measured, "
            "it refuses and COUNTS them. A one-line addition of "
            "`unresolved_sample` to the join report would close it; NOT done "
            "here -- clearinghouse_ingest.py is tranche 1-3 code and this is "
            "a finding, not a fix."
        )

        # -- 6b: the operator's deliberate explicit floor -----------------
        floor = 0.999
        out_b = require_scratch_target(
            self.work_root / "publish-explicit-floor", what="publish dir"
        )
        if out_b.exists():
            shutil.rmtree(out_b)
        out_b.mkdir(parents=True, exist_ok=True)
        receipt = publish_from_lakehouse(
            self.sink,
            bundle,
            publish_dir=out_b,
            state_root=self.state_root,
            texts=texts,
            coverage_floor=floor,
        )
        step.check(
            "6b: publishing with an EXPLICIT --coverage-floor below the "
            "measured coverage succeeds -- the operator's deliberate act",
            True,
            bool(receipt),
            evidence=f"--coverage-floor {floor}",
        )
        step.check(
            "6b: the guard it passed was evaluated SOURCE-SIDE, before the "
            "join -- a relationship test over an empty join passes vacuously",
            "source-side, before the join",
            str(receipt["guard"]["evaluated"]),
        )
        step.check(
            "6b: a shard directory WAS written this time",
            True,
            (out_b / "shard").is_dir(),
        )

        rows = _read_published_rows(out_b / "verdicts.parquet")
        decided = [row for row in rows if row.get("council") is not None]
        abstained = [row for row in rows if row.get("council") is None]
        step.check(
            "6b: 20,028 assertion rows -- 19,268 council-decided plus 760 "
            "abstentions, on distinct query_ids",
            (20028, 20028, 19268, 760),
            (
                len(rows),
                len({str(row["query_id"]) for row in rows}),
                len(decided),
                len(abstained),
            ),
        )
        step.find(
            "THE SPEC'S '19,268 ASSERTION ROWS' IS SHORT BY THE 760 "
            "ABSTENTIONS, and the task header is internally inconsistent "
            "about it. The taskplan says '19,268 assertions' and the header's "
            "fact B says '19,276 submitted', while the header's fact C "
            "requires abstentions to be published WITH TEXT and asserts "
            "abstentions_without_text == 0. Measured against the real plan: "
            "the submitted inventory is 19,276 fanout + 760 abstentions = "
            "20,036 distinct query_ids; 19,268 council items settled ok; and "
            "the publisher emits 20,028 rows (19,268 decided + 760 "
            "abstentions with council=null), leaving 8 unresolved. So "
            "coverage is 20,028/20,036 = 0.999601, NOT the header's "
            "0.999585 (= 19,268/19,276, which omits the abstentions on both "
            "sides). All three figures -- 19,268, 19,276 and 0.999585 -- "
            "describe the campaign MINUS its abstentions, because they were "
            "computed before the abstain record was located. 8 unresolved is "
            "right in every reading."
        )
        step.check(
            "6b: ZERO rows are attributable to the cancelled or pilot jobs -- "
            "every council-decided row names one of the twelve",
            set(),
            {
                str(row["council"]["job_id"])
                for row in decided
                if str(row["council"]["job_id"])
                not in set(CHUNK_JOB_IDS) | set(RETRY_JOB_IDS)
            },
            evidence=(
                "job_ids present: "
                + json.dumps(
                    sorted({str(row["council"]["job_id"]) for row in decided})
                )[:200]
            ),
        )
        step.check(
            "6b: EVERY council-decided payload carries council with all "
            "THREE fields non-null -- a partial reference looks joinable",
            0,
            sum(
                1
                for row in decided
                if any(
                    not str((row["council"] or {}).get(field) or "")
                    for field in COUNCIL_REFERENCE_FIELDS
                )
            ),
            evidence=f"fields={list(COUNCIL_REFERENCE_FIELDS)}",
        )
        step.check(
            "6b: every council cell names THIS store's declared identity",
            {self.store_id},
            {str(row["council"]["store_id"]) for row in decided},
        )
        step.check(
            "6b: EVERY retrieval abstention carries council=null WITH its "
            "decline reason -- three nulls side by side is a different "
            "statement from 'there is no council item'",
            (760, 760),
            (
                sum(1 for row in abstained if row.get("council") is None),
                sum(
                    1
                    for row in abstained
                    if str(row.get("decline_reason") or "").startswith("abstained:")
                ),
            ),
        )
        step.check(
            "6b: every abstention's status is NULL -- retrieval declined "
            "before any council item existed",
            0,
            sum(1 for row in abstained if row.get("status") is not None),
        )
        step.check(
            "6b: NO payload carries council_entity_hash -- the council's own "
            "per-item hash matched the clearinghouse hash 0 of 19,268 times",
            [],
            [
                column
                for column in (rows[0].keys() if rows else ())
                if "council_entity_hash" in str(column)
            ],
        )
        step.check(
            "6b: the publisher writes its OWN result_type and never touches "
            "the frozen one",
            ("go_molecular_function", "go_council_mapping"),
            (RESULT_TYPE, FROZEN_RESULT_TYPE),
            ok=RESULT_TYPE != FROZEN_RESULT_TYPE,
        )

        step.check(
            "6b RECEIPT, reconciled on DISTINCT query_ids: 20,036 submitted "
            "against 20,028 with a verdict, 8 unresolved",
            (20036, 20028, 8, True),
            (
                int(receipt["query_ids_submitted"]),
                int(receipt["query_ids_with_verdict"]),
                int(receipt["query_ids_unresolved"]),
                bool(receipt["reconciled"]),
            ),
        )
        step.check(
            "6b RECEIPT: the eight unresolved query_ids are NAMED, not hidden",
            8,
            len(receipt["unresolved_query_ids"]),
            evidence=json.dumps(sorted(receipt["unresolved_query_ids"])),
        )
        step.check(
            "the eight the DEFAULT floor refused for are EXACTLY the eight "
            "the explicit-floor receipt names",
            unresolved_expected,
            sorted(receipt["unresolved_query_ids"]),
        )
        step.check(
            "6b RECEIPT: abstentions_without_text is ZERO -- every abstention "
            "was published with a real text, and the slot is frozen on first "
            "write",
            [],
            list(receipt["abstentions_without_text"]),
            evidence=f"{receipt['abstentions']} abstentions in the bundle",
        )
        step.check(
            "6b RECEIPT: items, rows and query_ids are reported as THREE "
            "different numbers",
            {"items_submitted": 19276, "inputs_submitted": 20036, "export_rows": 20028},
            {
                "items_submitted": receipt["items_submitted"],
                "inputs_submitted": receipt["inputs_submitted"],
                "export_rows": receipt["export_rows"],
            },
        )
        step.check(
            "6b RECEIPT: the LOAD is reported as awaiting operator "
            "acceptance, never as success",
            "awaiting operator acceptance",
            str(receipt["load"]["state"]),
        )
        step.check(
            "6b RECEIPT: the target table's Iceberg snapshot id is reported "
            "as None WITH its reason -- it is assigned in the pod",
            None,
            receipt["target_table_snapshot_id"],
        )
        step.check(
            "6b: the publish wrote into the SCRATCH dataset, and no "
            "production clearinghouse row exists",
            True,
            str(out_b).startswith(SCRATCH_PREFIXES),
            evidence=str(out_b),
        )
        self._receipt = receipt
        self._published_rows = rows
        step.note(
            "the receipt's declines_by_reason is "
            + json.dumps(receipt.get("declines_by_reason") or {}, sort_keys=True)
        )

    # -- step 7 ---------------------------------------------------------

    def step_7_cross_check(self, step: Step) -> None:
        """Cross-check the published rows against the INDEPENDENT artifact.

        Disagreement is a FINDING and is reported PER ROW, never as a count.
        """
        from collections import Counter

        rows = getattr(self, "_published_rows", None)
        if rows is None:
            step.not_asserted(
                "the published assertion rows do not exist: step 6 did not "
                "produce them. See step 6."
            )
            return
        if not self.verdict_export.is_file():
            step.not_asserted(f"verdict export absent: {self.verdict_export}")
            return

        export: dict[str, dict[str, Any]] = {}
        export_rows = 0
        for raw in self.verdict_export.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            export_rows += 1
            record = json.loads(raw)
            export[str(record["clearinghouse_entity_hash"])] = record
        step.check(
            "the independent verdict export holds 19,268 rows on 19,268 "
            "distinct clearinghouse_entity_hashes",
            (19268, 19268),
            (export_rows, len(export)),
            evidence=str(self.verdict_export),
        )
        step.check(
            "the export's own decided_by distribution",
            {"unanimous": 14619, "meta": 4649},
            dict(Counter(str(r.get("decided_by")) for r in export.values())),
        )

        published = {str(row["query_id"]): row for row in rows}
        decided = {
            query_id: row
            for query_id, row in published.items()
            if row.get("council") is not None
        }
        only_published = sorted(set(published) - set(export))
        only_export = sorted(set(export) - set(published))
        step.check(
            "every row in the independent export has a published counterpart "
            "on clearinghouse_entity_hash == query_id",
            [],
            only_export[:50],
            ok=not only_export,
            evidence=f"{len(only_export)} export-only query_ids",
        )
        step.check(
            "the 760 rows present ONLY on the published side are exactly the "
            "abstentions, which are absent from the export BY CONSTRUCTION "
            "and are therefore NOT disagreements",
            (760, 760),
            (
                len(only_published),
                sum(
                    1
                    for query_id in only_published
                    if published[query_id].get("council") is None
                ),
            ),
        )
        step.check(
            "the 19,268 COUNCIL-DECIDED published rows are exactly the "
            "export's key set",
            (19268, []),
            (len(decided), sorted(set(decided) ^ set(export))[:50]),
        )

        go_disagree: list[dict[str, Any]] = []
        decided_disagree: list[dict[str, Any]] = []
        for query_id in sorted(set(decided) & set(export)):
            mine = decided[query_id]
            theirs = export[query_id]
            my_ids = [str(x) for x in (mine.get("go_ids") or [])]
            their_ids = [str(x) for x in (theirs.get("go_ids") or [])]
            if my_ids != their_ids:
                go_disagree.append(
                    {
                        "query_id": query_id,
                        "published_go_ids": my_ids,
                        "export_go_ids": their_ids,
                        "council_item_id": str(
                            (mine.get("council") or {}).get("item_id")
                        ),
                    }
                )
            if str(mine.get("decided_by") or "") != str(theirs.get("decided_by") or ""):
                decided_disagree.append(
                    {
                        "query_id": query_id,
                        "published_decided_by": mine.get("decided_by"),
                        "export_decided_by": theirs.get("decided_by"),
                        "council_item_id": str(
                            (mine.get("council") or {}).get("item_id")
                        ),
                    }
                )
        step.check(
            "GO ids agree PER ROW with the independent export across all "
            "19,268 council-decided rows",
            [],
            go_disagree[:50],
            ok=not go_disagree,
            evidence=f"{len(go_disagree)} disagreeing rows of {len(decided):,}",
        )
        step.check(
            "decided_by agrees PER ROW with the independent export",
            [],
            decided_disagree[:50],
            ok=not decided_disagree,
            evidence=f"{len(decided_disagree)} disagreeing rows of {len(decided):,}",
        )
        if go_disagree or decided_disagree:
            step.find(
                "PER-ROW DISAGREEMENT WITH THE INDEPENDENT EXPORT. "
                f"go_ids: {len(go_disagree)} row(s); decided_by: "
                f"{len(decided_disagree)} row(s). Every one is listed above "
                "by query_id with BOTH values -- this is a finding, not a "
                "count."
            )
        step.check(
            "the published rows reproduce the export's decided_by "
            "distribution exactly",
            {"unanimous": 14619, "meta": 4649},
            dict(Counter(str(row.get("decided_by")) for row in decided.values())),
        )
        step.check(
            "the published rows name the SAME winning council item as the "
            "export's council_item_id, per row",
            [],
            [
                {
                    "query_id": query_id,
                    "published": str((decided[query_id]["council"] or {}).get("item_id")),
                    "export": str(export[query_id].get("council_item_id")),
                }
                for query_id in sorted(set(decided) & set(export))
                if str((decided[query_id]["council"] or {}).get("item_id"))
                != str(export[query_id].get("council_item_id"))
            ][:50],
        )
        step.check(
            "the publisher's bundle-ordered winner fold picks the SAME retry "
            "job as the export's independent fold, for every retried item",
            [],
            [
                {
                    "query_id": query_id,
                    "published_job": str(
                        (decided[query_id]["council"] or {}).get("job_id")
                    ),
                    "export_job": str(export[query_id].get("job_id")),
                }
                for query_id in sorted(set(decided) & set(export))
                if str((decided[query_id]["council"] or {}).get("job_id"))
                != str(export[query_id].get("job_id"))
            ][:50],
            evidence=(
                "105 of the 19,268 verdicts come from the two retry rounds; "
                "retries resolve LAST by the bundle's declared order"
            ),
        )

        empty_text = [
            query_id
            for query_id, row in published.items()
            if not str(row.get("function_text") or "").strip()
        ]
        step.check(
            "no published row carries an empty function_text -- the "
            "clearinghouse slot is frozen on first write",
            [],
            empty_text[:50],
            ok=not empty_text,
        )
        no_genomes = sum(1 for row in published.values() if not (row.get("genomes") or []))
        step.find(
            "THE LAKEHOUSE PUBLISHER CANNOT RECOVER `genomes` AND PUBLISHES "
            f"IT EMPTY: {no_genomes:,} of {len(published):,} rows carry an "
            "empty genomes list, where the 2026-10-22 export carried the "
            "ADP1/PMI genome lists resolved from priority_adp1_pmi.tsv. "
            "verdicts.export_rows takes an optional `genomes=` map and "
            "council_publish.lakehouse_export_rows never passes one -- "
            "correctly, because which genomes carry a function is a "
            "CLEARINGHOUSE fact and not a council one, and inventing it from "
            "a TSV inside the publisher would be a text join by another "
            "name. Recording it because the two artifacts are NOT "
            "interchangeable on that column and a consumer diffing them will "
            "see it."
        )
        step.note(
            "ABSTENTIONS ARE ABSENT FROM THE EXPORT BY CONSTRUCTION -- the "
            "2026-10-22 export was built from `items WHERE status='ok'`, so "
            "a function declined before submission has no row there. Their "
            "760 published rows are therefore NOT disagreements, and are "
            "asserted above as exactly the published-only set."
        )
        step.note(
            "THE `council` CELL HAS NO INDEPENDENT ARTIFACT TO BE CHECKED "
            "AGAINST: the export predates the lakehouse publisher and "
            "carries no `council` column at all. Its council_item_id and "
            "job_id are cross-checked above, which covers two of the three "
            "fields; store_id has no counterpart because the export was "
            "written before the store declared an identity."
        )

    def step_8_reuse_seed(self, step: Step) -> None:
        """Seed a reuse-only store from the archived rows."""
        from .council_seed import SEED_TABLES, read_seed_rows

        seed = read_seed_rows(
            self.sink,
            store_id=self.store_id,
            snapshot_id=str(self.snapshot_id),
            query_name="go_mf",
            query_version="4",
            state_root=self.state_root,
        )
        step.check(
            "the seed read refuses nothing: the snapshot is completely "
            "archived",
            True,
            bool(seed["receipt"]["complete"]),
        )
        step.check(
            "the seed carries only live, ok, in-scope calls",
            82416,
            int(seed["counts"]["calls"]),
            evidence=json.dumps(seed["counts"], sort_keys=True),
        )
        step.check(
            "attempts, endpoint_state and workers are deliberately NOT read "
            "into a seed",
            ("query_types", "jobs", "items", "calls"),
            tuple(SEED_TABLES),
        )

        reuse_path = require_scratch_target(
            self.work_root / "reuse" / "council.sqlite", what="reuse store"
        )
        if reuse_path.exists():
            reuse_path.unlink()
        store = self._store(reuse_path, check_version=False)
        try:
            store.init_schema()
            imported = store.seed_from_rows(
                seed["rows"],
                store_id=self.store_id,
                snapshot_id=str(self.snapshot_id),
                query_name="go_mf",
                query_version="4",
                seeded_by=self.declared_by,
            )
            step.check(
                "seed_from_rows imported the calls it was given",
                int(seed["counts"]["calls"]),
                int((imported.get("counts") or {}).get("calls", -1)),
                evidence=json.dumps(imported.get("counts") or {}, sort_keys=True),
            )

            sample = [
                str(row["prompt_hash"])
                for row in seed["rows"]["calls"][:: max(1, len(seed["rows"]["calls"]) // 25)]
            ][:25]
            hits = [ph for ph in sample if store.find_reusable(ph) is not None]
            step.check(
                "find_reusable HITS for a sample of known prompt_hashes "
                "after seeding",
                len(sample),
                len(hits),
                evidence=f"sampled {len(sample)} prompt_hashes across the seed",
            )
            step.check(
                "find_reusable MISSES a prompt_hash that was never seeded",
                None,
                store.find_reusable("0" * 64),
            )

            claimed = store.claim_calls(
                "rehearsal-worker",
                {"argo", "mag", "claude", "codex"},
                limit=100,
            )
            step.check(
                "NO IMPORTED ROW IS CLAIMABLE, asserted through the REAL "
                "store.claim_calls",
                [],
                claimed,
                ok=not claimed,
                evidence=f"claim_calls returned {len(claimed)} rows",
            )
            records = store.seed_records()
            step.check(
                "the reuse store records exactly ONE seed origin",
                1,
                len(records),
                evidence=json.dumps(records, default=str, sort_keys=True)[:300],
            )
        finally:
            store.close()

        step.note(
            "TRANCHE 3'S STATED LIMITS, which this does not contradict and "
            "does not test away: a seeded job is hidden from the QUEUE (it "
            "is tombstoned with deleted_at set and select_claimable excludes "
            "it), NOT from a caller who names it directly; and `kbu council "
            "db export` INCLUDES seeded rows. Both are documented behaviour "
            "of seed_from_rows, not defects found here."
        )

    # -- step 9 ---------------------------------------------------------

    def step_9_second_snapshot(self, step: Step) -> None:
        """Archive a SECOND, MUTATED snapshot and assert non-additivity."""
        from .council_archive import current_projection, require_complete_delivery
        from .council_delivery import (
            MANIFEST_FILENAME,
            CouncilDeliveryScanner,
            build_manifest,
            write_manifest,
        )

        assert self.store_copy is not None
        target = require_scratch_target(self.store_copy, what="mutation target")

        # MUTATE THE REHEARSAL'S OWN COPY. One appended call row and one
        # bumped endpoint counter -- the smallest change that makes the two
        # snapshots distinguishable in both an additive and a non-additive
        # table.
        connection = sqlite3.connect(str(target))
        try:
            connection.row_factory = sqlite3.Row
            template = connection.execute(
                "SELECT * FROM calls ORDER BY call_id LIMIT 1"
            ).fetchone()
            row = dict(template)
            row["call_id"] = "rehearsal-appended-call-0001"
            columns = ", ".join(f'"{name}"' for name in row)
            placeholders = ", ".join("?" for _ in row)
            connection.execute(
                f"INSERT INTO calls ({columns}) VALUES ({placeholders})",
                tuple(row.values()),
            )
            before = connection.execute(
                "SELECT epoch_tokens, lifetime_tokens FROM endpoint_state "
                "WHERE endpoint = 'argo'"
            ).fetchone()
            bump = 1000
            connection.execute(
                "UPDATE endpoint_state SET epoch_tokens = epoch_tokens + ?, "
                "lifetime_tokens = lifetime_tokens + ? WHERE endpoint = 'argo'",
                (bump, bump),
            )
            connection.commit()
            argo_before = int(before["lifetime_tokens"])
        finally:
            connection.close()

        store = self._store(target)
        try:
            drop = require_scratch_target(
                self.work_root / "drop" / "delivery-seq2", what="delivery drop"
            )
            if drop.exists():
                shutil.rmtree(drop)
            drop.mkdir(parents=True, exist_ok=True)
            taken = store.snapshot(drop / "council-snapshot.sqlite")
        finally:
            store.close()
        self.snapshot_2_id = str(taken["snapshot_id"])
        step.check(
            "the second snapshot has a DIFFERENT verified digest",
            True,
            self.snapshot_2_id != self.snapshot_id,
            evidence=f"seq1={self.snapshot_id} seq2={self.snapshot_2_id}",
        )
        step.check(
            "the second snapshot carries the SAME declared store identity",
            self.store_id,
            str(taken["store_id"]),
        )

        manifest = build_manifest(
            drop,
            delivery_id=f"rehearsal-{self.snapshot_2_id[:12]}-seq2",
            store_id=self.store_id,
            snapshot_id=self.snapshot_2_id,
            snapshot_file="council-snapshot.sqlite",
            snapshot_seq=2,
            snapshot_schema_version=2,
            store_host=str(taken.get("store_host") or ""),
            deadline=timedelta(hours=6),
        )
        write_manifest(manifest, drop / MANIFEST_FILENAME)
        scanner = CouncilDeliveryScanner(
            drop_dir=self.work_root / "drop",
            scratch_dir=self.work_root / "archive-scratch",
            sink=self.sink,
            state_root=self.state_root,
            holder="acceptance-rehearsal",
        )
        tick = scanner.tick()
        step.check(
            "the scanner now enumerates BOTH deliveries every tick",
            2,
            int(tick["deliveries"]),
        )
        states = {
            str(report["snapshot_id"]): str(report["state"])
            for report in tick["reports"]
        }
        step.check(
            "both deliveries reconcile to complete, and the first is served "
            "from its CACHED receipt re-checked against the lakehouse",
            {str(self.snapshot_id): "complete", self.snapshot_2_id: "complete"},
            states,
        )
        step.check(
            "the second delivery is COMPLETE in the ledger",
            True,
            bool(
                require_complete_delivery(
                    self.sink,
                    store_id=self.store_id,
                    snapshot_id=self.snapshot_2_id,
                    state_root=self.state_root,
                )["complete"]
            ),
        )

        base = {"store_id": self.store_id}
        calls_1 = self.sink.count_rows(
            "calls", where={**base, "snapshot_id": self.snapshot_id}
        )
        calls_2 = self.sink.count_rows(
            "calls", where={**base, "snapshot_id": self.snapshot_2_id}
        )
        step.check(
            "BOTH snapshots are independently queryable by snapshot_id, and "
            "the first is UNCHANGED by the second's arrival",
            (203821, 203822),
            (calls_1, calls_2),
        )
        step.check(
            "nothing was overwritten to make the second snapshot current: "
            "the store's archived calls total is the SUM of two independent "
            "snapshots",
            calls_1 + calls_2,
            self.sink.count_rows("calls", where=base),
        )

        projected = current_projection(
            self.sink, "endpoint_state", store_id=self.store_id
        )
        step.check(
            "current_projection resolves by snapshot_seq to the SECOND "
            "snapshot",
            {self.snapshot_2_id},
            {str(row["snapshot_id"]) for row in projected},
        )
        step.check(
            "current_projection resolves to snapshot_seq 2",
            {2},
            {int(row["snapshot_seq"]) for row in projected},
        )

        argo = {
            seq: int(row["lifetime_tokens"])
            for seq, snapshot_id in ((1, self.snapshot_id), (2, self.snapshot_2_id))
            for row in self.sink.select_rows(
                "endpoint_state",
                where={**base, "snapshot_id": snapshot_id, "endpoint": "argo"},
            )
        }
        step.check(
            "endpoint_state is NOT ADDITIVE across snapshots: seq 2 is the "
            "RUNNING TOTAL, not seq 1 plus seq 2",
            argo_before + 1000,
            argo[2],
            evidence=(
                f"seq1={argo[1]:,} seq2={argo[2]:,}; the additive reading "
                f"would be {argo[1] + argo[2]:,}, which is ~2x the tokens "
                "ARGO was ever charged"
            ),
        )
        step.check(
            "the additive reading of endpoint_state is WRONG, and by how "
            "much: two observations of one counter are not two spends",
            True,
            argo[1] + argo[2] != argo[2],
            evidence=f"sum={argo[1] + argo[2]:,} truth={argo[2]:,}",
        )
        step.note(
            "the mutation was one appended calls row "
            "('rehearsal-appended-call-0001', a copy of the lowest call_id "
            "with a new id) and +1,000 tokens on the argo endpoint counter, "
            "applied to the REHEARSAL'S OWN store copy under "
            f"{self.work_root} -- never to the shared snapshot and never to "
            "the production store."
        )

    # -- driving --------------------------------------------------------

    def run(self, *, only: Sequence[str] | None = None) -> dict[str, Any]:
        """Run every step (or only the named ones) and return the report."""
        from datetime import datetime, timezone

        plan: list[tuple[str, str, Callable[[Step], None]]] = [
            ("0", "find the run directory", self.step_0_run_directory),
            ("1", "bootstrap the council namespace into a SCRATCH dataset",
             self.step_1_bootstrap),
            ("2", "assign identity, stage a manifest-v1 delivery, run the "
                  "SCANNER to COMPLETE", self.step_2_identity_and_delivery),
            ("3", "assert the archive BY DIRECT QUERY",
             self.step_3_archive_by_direct_query),
            ("4", "seed dispositions from the provenance JSON",
             self.step_4_dispositions),
            ("5", "produce the publication bundle", self.step_5_bundle),
            ("6", "publish into the SCRATCH clearinghouse (6a default floor "
                  "refusal, 6b explicit floor)", self.step_6_publish),
            ("7", "cross-check against the independent verdict export",
             self.step_7_cross_check),
            ("8", "seed a reuse-only store from the archived rows",
             self.step_8_reuse_seed),
            ("9", "archive a SECOND, mutated snapshot",
             self.step_9_second_snapshot),
        ]
        wanted = None if not only else {str(entry) for entry in only}
        started = datetime.now(tz=timezone.utc)
        clock = time.time()
        for number, name, body in plan:
            if wanted is not None and number not in wanted:
                continue
            self._run(number, name, body)
        finished = datetime.now(tz=timezone.utc)

        steps = [step.as_dict() for step in self.steps]
        tally = {state: 0 for state in (ASSERTED, FAILED, NOT_ASSERTED)}
        for step in steps:
            tally[step["state"]] += 1
        held = sum(
            1 for step in steps for a in step["assertions"] if a["ok"]
        )
        total = sum(len(step["assertions"]) for step in steps)
        return {
            "schema_version": 1,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "seconds": round(time.time() - clock, 2),
            "host": os.uname().nodename,
            "interpreter": sys.executable,
            "store_id": self.store_id,
            "snapshot": str(self.snapshot),
            "snapshot_id": self.snapshot_id,
            "snapshot_2_id": self.snapshot_2_id,
            "work_root": str(self.work_root),
            "sink": str(self.sink_path),
            "steps": steps,
            "summary": {
                **tally,
                "assertions_held": held,
                "assertions_total": total,
                "verdict": (
                    "FAILED"
                    if tally[FAILED]
                    else "PARTIAL -- see NOT ASSERTED steps"
                    if tally[NOT_ASSERTED]
                    else "ASSERTED"
                ),
            },
            "in_pod_leg": in_pod_leg(),
            "tokens_spent": 0,
            "live_council_runs": 0,
        }


def run_rehearsal(**kwargs: Any) -> dict[str, Any]:
    """Build a :class:`Rehearsal` and run it. The one callable entry point."""
    only = kwargs.pop("only", None)
    rehearsal = Rehearsal(**kwargs)
    try:
        return rehearsal.run(only=only)
    finally:
        if rehearsal.sink is not None:
            rehearsal.sink.close()
