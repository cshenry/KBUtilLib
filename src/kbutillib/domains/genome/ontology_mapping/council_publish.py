"""Stage 5's PRODUCER half: render, gate and shard the run's verdicts.

:func:`council_publish` is the verb behind ``kbu ontomap council-publish``.  It
is the last stage of the standing, quoted, operator-approved ``function_to_go``
pipeline -- detect un-run -> quote -> approve -> run -> publish -- and it owns
the half of the hand-off that happens on THIS machine.

**IT OWNS NO PIPELINE INFRASTRUCTURE.**  Every step is a call into something
that already exists: :func:`~kbutillib.domains.kbase.berdl.clearinghouse_backfill.resolve_manifest_path`
for the shipped ``function-to-go-council`` preset,
:func:`~kbutillib.domains.kbase.berdl.clearinghouse_manifest.load_manifest` +
:func:`~kbutillib.domains.kbase.berdl.clearinghouse_manifest.shard_plan` for the
plan-time gate (the single plan-time validator -- it is what ``kbu
clearinghouse plan`` resolves a manifest through), and
:func:`~kbutillib.domains.kbase.berdl.clearinghouse_shard.shard_manifest` for
the bronze write.  What is left here is the ORDER those are called in, the
RENDER of the preset's four per-run markers, and the record of what was
published.

**THERE IS NO SECOND MANIFEST FORMAT.**  The sharder already publishes
``<out>/run.json`` carrying, per batch, a sha256, a row count, a byte count and
a ``[hash_min, hash_max]`` range.  That IS the completeness contract this
hand-off needs, and the ingest side already verifies every field of it, so
inventing a second inventory would mean two descriptions of one directory that
could disagree -- and the one a load trusted would be whichever it happened to
read.  This module WRITES ``run.json`` through the sharder and checks it back;
it does not write its own.

SHARDING RUNS ANYWHERE -- its own docstring says so, "no pod and no
credentials" -- so this whole verb runs on h100, beside the run directory.
Nothing here is in-pod and nothing here needs a token.
``_require_in_pod_or_refuse`` guards exactly two verbs, ``load`` and ``verify``,
and both are the CONSUMER's half: Dropbox syncs ``<run-dir>/shard/`` to kbhub,
where ``kbu clearinghouse load`` (fill-only, idempotent, ledger-resumable) and
``kbu clearinghouse verify`` run.  **NOTHING IN THIS MODULE RUNS ON KBHUB.**

THIS MODULE IMPORTS NO NUMPY, for the same reason :mod:`.council_run`,
:mod:`.unrun` and :mod:`.verdicts` do not: publishing ranks nothing, so a
publish stage living in :mod:`.ontology_mapping_utils` -- which imports
:mod:`.go_text`, and through it numpy / scikit-learn / joblib -- would be
unrunnable on a host without the retrieval stack for no reason at all.  That is
also why the run's SCRATCH directory is recovered from ``PLAN.json``'s own
absolute ``items_file`` rather than by re-resolving ``$KBU_SCRATCH_DIR``: the
plan already recorded where it wrote, and re-deriving it could answer
differently from the stage that actually wrote the file.

WHAT THE RENDER SUBSTITUTES, AND WHY A RENDERED COPY AT ALL.  The preset ships
with four deliberately-inert markers, because its three parameter-set values
and its source label vary per run and neither of the alternatives works:
``load_manifest`` expands ``${VAR}`` placeholders over the LOCATOR half only
(the mapping half must be byte-identical everywhere, or one manifest would
write different partition values on different hosts), so a ``${VAR}`` in
``parameter_set`` is a legal string that would silently hash the TEMPLATE; and
``parameter_set_column`` is for a source whose set varies per RECORD, where
this one is a single constant for the whole file.  So the publish step writes a
run-local copy of the manifest with the markers substituted, which is also the
reviewable record of exactly what was published.

THE INERT HASH IS A GATE, NOT A COMMENT.  The un-rendered parameter set hashes
to :data:`UNRENDERED_PARAMETER_SET_HASH`; a ``function_result`` row at that
hash means the manifest was loaded un-rendered, and every one of the 47,341
clearinghouse functions would read as answered by whichever bench got there
first.  So the rendered manifest's own ``parameter_set_hash`` -- taken from
``shard_plan``, not recomputed here -- is compared against the one
``PLAN.json`` recorded, and a mismatch (or the inert value) refuses before a
single row is written.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

__all__ = [
    "PRESET_NAME",
    "MANIFEST_FILENAME",
    "PUBLISH_FILENAME",
    "SHARD_SUBDIR",
    "UNRENDERED_PARAMETER_SET_HASH",
    "PublishRefused",
    "render_manifest",
    "council_publish",
]


#: The shipped backfill preset this verb renders. Resolved BY NAME through
#: ``resolve_manifest_path``, never by path: the preset is package data, so
#: ``council-publish`` has to work from a plain ``pip install`` on a host with
#: no source tree.
PRESET_NAME = "function-to-go-council"

#: The rendered manifest, written into the run directory. Small, TOML, and the
#: reviewable record of what was published -- which is the other half of why
#: the render is a FILE rather than an in-memory object.
MANIFEST_FILENAME = "MANIFEST.toml"

#: The publish record, in the run directory beside ``PLAN.json``, ``RUN.json``
#: and ``APPROVED.json``. MUST equal
#: :data:`~kbutillib.domains.kbase.berdl.clearinghouse_ingest.PUBLISH_RECORD_FILENAME`
#: -- the receipt reads the ITEM accounting out of this file, so the producer
#: and the consumer have to agree on its name. Stated here rather than
#: imported, so this module needs no module-level dependency on the berdl
#: stack; a test asserts the two are the same string.
PUBLISH_FILENAME = "PUBLISH.json"

#: The shard run's directory, under the run directory. It is what Dropbox
#: syncs to kbhub and what ``kbu clearinghouse load`` is pointed at.
SHARD_SUBDIR = "shard"

#: The parameter-set hash of the preset AS SHIPPED. The preset's own header
#: names this constant for exactly this check; a row at this hash means the
#: markers were never substituted.
UNRENDERED_PARAMETER_SET_HASH = (
    "7bccf01baa4f69e139545bf712247b8f49047c6569bad1e107b3e3417f960d49"
)

#: The 64 zeros both digest markers ship as.
_ZERO_DIGEST = "0" * 64

#: The preset's verdict-parquet LOCATOR, exactly as it ships. Rendered to the
#: run's own absolute parquet path, so the rendered manifest is self-contained
#: and does not depend on an environment the load never sees.
_MARKER_PATH = (
    'path        = "${KBU_F2GO_VERDICTS:-${MANIFEST_DIR}'
    '/function-to-go-council-example.parquet}"'
)

#: The result ``source`` label, exactly as it ships.
_MARKER_SOURCE = '  source              = "@const(council/go_mf@0)"'

#: The ``parameter_set`` line, exactly as it ships.
_MARKER_PARAMETER_SET = (
    '  parameter_set       = { query = "go_mf@0", bench_fingerprint = '
    f'"{_ZERO_DIGEST}", mapping_hash = "{_ZERO_DIGEST}" }}'
)

#: The one target table this preset writes. Asserted rather than assumed: the
#: functions were READ from ``function_entity``/``function_content`` in stage
#: 1, so a render that somehow produced an entity or content plan would mint a
#: SECOND identity for a function that already has one.
_TARGET_TABLE = "function_result"

#: The ``PLAN.json`` fields the render needs, every one of which is a per-run
#: value with no safe default. Named here so a plan written by an older stage
#: 1 is refused with the missing field named, rather than rendered with an
#: empty string into a partition key.
_REQUIRED_PLAN_FIELDS = (
    "query",
    "bench_fingerprint",
    "mapping_hash",
    "parameter_set_hash",
    "source",
)


class PublishRefused(Exception):
    """Stage 5 will not publish, and :attr:`check` says why.

    Carries the failed check as a field as well as in the message, exactly as
    :class:`~kbutillib.domains.genome.ontology_mapping.council_run.RunRefused`
    does, so a caller can branch on WHICH check failed without parsing
    English.
    """

    def __init__(self, check: str, detail: str) -> None:
        self.check = check
        self.detail = detail
        super().__init__(f"council_publish check {check!r} failed: {detail}")


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# The render
# ---------------------------------------------------------------------------


def render_manifest(
    preset_text: str, *, plan: Mapping[str, Any], verdicts_path: Union[str, Path]
) -> str:
    """Substitute the preset's four per-run markers, or refuse.

    Pure: text in, text out. Every marker must appear EXACTLY ONCE in the
    shipped text and is replaced once, which is what makes a preset edit that
    moved or duplicated one a loud failure here rather than a publish of the
    template. The header comments mention the same strings, so the anchors are
    whole declaration LINES and never the bare values.

    The four, all from ``PLAN.json``:

    ===============================  ==================================
    marker                           substituted with
    ===============================  ==================================
    ``path`` locator                 the run's verdict parquet, absolute
    ``source = @const(council/..)``  ``"council/" + plan["query"]``
    ``parameter_set.query``          ``plan["query"]``
    ``parameter_set.*_hash``         the plan's two digests
    ===============================  ==================================

    ``source`` and ``parameter_set.query`` are two spellings of ONE fact and
    ``clearinghouse_parameters.validate_council_parameter_set`` checks that
    they agree, so a render that substituted one and missed the other fails at
    plan time rather than publishing under a partition whose halves disagree.
    Both are written from the same ``plan["query"]`` here for that reason.

    Raises:
        PublishRefused: A marker is absent or appears more than once, the plan
            omits a required field, or the rendered text still holds a
            ``${VAR}``.
    """
    missing = [field for field in _REQUIRED_PLAN_FIELDS if not plan.get(field)]
    if missing:
        raise PublishRefused(
            "plan_fields",
            f"PLAN.json omits {missing}. Every one of those is a per-run "
            "value with no safe default -- the query version and the two "
            "digests ARE the partition key, and a blank one would publish "
            "under a partition nothing can reproduce. Re-run "
            "`kbu ontomap council-plan`.",
        )
    query = str(plan["query"])
    text = preset_text
    substitutions = (
        (_MARKER_PATH, f'path        = "{Path(verdicts_path)}"', "path"),
        (
            _MARKER_SOURCE,
            f'  source              = "@const(council/{query})"',
            "source",
        ),
        (
            _MARKER_PARAMETER_SET,
            "  parameter_set       = { "
            f'query = "{query}", '
            f'bench_fingerprint = "{plan["bench_fingerprint"]}", '
            f'mapping_hash = "{plan["mapping_hash"]}" }}',
            "parameter_set",
        ),
    )
    for marker, replacement, name in substitutions:
        found = text.count(marker)
        if found != 1:
            raise PublishRefused(
                "marker",
                f"the {name!r} marker appears {found} time(s) in the "
                f"{PRESET_NAME!r} preset; exactly one is required. The "
                "preset was edited and this render would publish the "
                f"TEMPLATE. The anchor is:\n  {marker}",
            )
        text = text.replace(marker, replacement, 1)
    if "${" in text.replace("${MANIFEST_DIR}", ""):
        leftover = [
            line.strip()
            for line in text.splitlines()
            if "${" in line and not line.lstrip().startswith("#")
        ]
        if leftover:
            raise PublishRefused(
                "placeholder",
                "the rendered manifest still holds an unsubstituted "
                f"placeholder: {leftover[:3]}. load_manifest expands "
                "placeholders over the LOCATOR half only, so one left in the "
                "mapping half is a legal string that would be hashed as "
                "written.",
            )
    return text


# ---------------------------------------------------------------------------
# The verb
# ---------------------------------------------------------------------------


def council_publish(
    run_dir: Union[str, Path],
    *,
    verdicts: Optional[Union[str, Path]] = None,
    out_dir: Optional[Union[str, Path]] = None,
    target_bytes: Optional[int] = None,
    preset: str = PRESET_NAME,
) -> Dict[str, Any]:
    """Render the preset, gate it, and shard the run's verdicts.

    Spends NO LLM tokens and makes no transport call: it reads ``PLAN.json``
    and the verdict parquet and writes two files plus a shard run, all on this
    machine.

    THE ORDER, and each step's reason:

    1. **PLAN.json** -- the run identity. The query reference, the two digests
       and the ``parameter_set_hash`` stage 1 computed.
    2. **THE VERDICT PARQUET** -- ``--verdicts``, else ``verdicts.parquet`` in
       the scratch directory ``PLAN.json`` names. ``council_export`` refuses to
       write it into the synced run directory, so it is never beside
       ``PLAN.json``.
    3. **RENDER** (:func:`render_manifest`) into ``<run-dir>/MANIFEST.toml``.
    4. **THE PLAN GATE** -- ``load_manifest`` + ``shard_plan``, the single
       plan-time validator, which is what ``kbu clearinghouse plan`` resolves a
       manifest through. An invalid manifest raises
       :class:`~clearinghouse_manifest.ManifestError` naming the offending key
       BEFORE any file is written. The rendered ``parameter_set_hash`` is then
       compared against the plan's, and the inert un-rendered value is refused
       by name.
    5. **SHARD** -- ``shard_manifest`` into ``<run-dir>/shard``. Runs anywhere.
    6. **CHECK THE INVENTORY BACK** -- every batch in ``run.json`` must carry
       a sha256, a row count and a hash range. That is the completeness
       contract the consumer verifies against, so a batch missing any of it is
       refused here, where it can still be re-sharded.
    7. **PUBLISH.json** -- the item accounting the receipt reconciles against.

    Args:
        run_dir: The run directory holding ``PLAN.json``.
        verdicts: The verdict parquet. Default: ``verdicts.parquet`` in the
            scratch directory ``PLAN.json`` names.
        out_dir: Where the shard run goes. Default ``<run-dir>/shard``, which
            is the path Dropbox syncs to the consumer.
        target_bytes: Target uncompressed bytes per shard.
        preset: The shipped preset to render. Overridable for tests only.

    Returns:
        The publish record, exactly as written to ``PUBLISH.json``, plus
        ``publish_file`` and ``warnings``.

    Raises:
        PublishRefused: Any gate above failed; ``.check`` names which.
        ManifestError: The rendered manifest does not resolve -- raised
            unchanged, because its message already names the offending key.
    """
    from kbutillib.domains.ai.council.quote import PLAN_FILENAME
    from kbutillib.domains.kbase.berdl import clearinghouse_backfill as cb
    from kbutillib.domains.kbase.berdl import clearinghouse_manifest as cm
    from kbutillib.domains.kbase.berdl import clearinghouse_shard as cs

    run_dir = Path(run_dir).expanduser()
    plan = _read_plan(run_dir / PLAN_FILENAME)
    verdicts_path = _resolve_verdicts(plan, verdicts, run_dir=run_dir)

    preset_path, preset_name = cb.resolve_manifest_path(preset)
    rendered_text = render_manifest(
        preset_path.read_text(encoding="utf-8"),
        plan=plan,
        verdicts_path=verdicts_path,
    )
    manifest_path = run_dir / MANIFEST_FILENAME
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_atomic(manifest_path, rendered_text)

    # -- the gate: `kbu clearinghouse plan`'s own validator ---------------
    manifest = cm.load_manifest(manifest_path)
    plans = cm.shard_plan(manifest)
    gate = _gate_report(plans, manifest, cs)
    _assert_partition(plans, plan=plan, manifest_path=manifest_path)

    # -- the shard run: runs anywhere, no pod and no credentials ----------
    shard_root = Path(out_dir).expanduser() if out_dir else run_dir / SHARD_SUBDIR
    kwargs: Dict[str, Any] = {}
    if target_bytes is not None:
        kwargs["target_bytes"] = target_bytes
    reports = cs.shard_manifest(
        manifest, str(shard_root), manifest_path=str(manifest_path), **kwargs
    )
    run_manifest = cs.read_run_manifest(shard_root)
    batches = _check_inventory(run_manifest, shard_root=shard_root)

    counts, declines, warnings = _account(plan, verdicts_path, batches)
    record: Dict[str, Any] = {
        "schema_version": 1,
        "generated_at": _now_iso(),
        "run_dir": str(run_dir),
        "run_id": str(plan.get("run_id") or run_dir.name),
        "query": str(plan["query"]),
        "source": str(plan["source"]),
        "bench_fingerprint": str(plan["bench_fingerprint"]),
        "mapping_hash": str(plan["mapping_hash"]),
        "parameter_set_hash": str(plan["parameter_set_hash"]),
        "plan_hash": str(plan.get("plan_hash") or ""),
        "preset": preset_name or str(preset_path),
        "manifest_file": str(manifest_path),
        "verdicts_file": str(verdicts_path),
        "plan_gate": gate,
        "shard_dir": str(shard_root),
        "run_manifest": str(cs.run_manifest_path(shard_root)),
        "shard_run_id": run_manifest.get("run_id"),
        "shard_unit_id": reports[0].unit_id if reports else None,
        "batches": batches,
        "counts": counts,
        "declines_by_reason": declines,
        # THE DECLARED HAND-OFF. Dropbox syncs `shard_dir` to the pod; these
        # are the two IN-POD verbs that consume it, written down here because
        # NOTHING IN THIS REPOSITORY RUNS THEM -- that is the consumer
        # machine's scope -- and an undeclared hand-off is one somebody has to
        # reconstruct from a docstring. `receipt_file` is where `verify
        # --receipt` should put the receipt: in the run directory, which is
        # the synced surface the producer reads back. Writing it is OPT-IN on
        # the consumer side because `verify` otherwise WRITES NOTHING, and
        # that guarantee is worth more than a default.
        "hand_off": {
            "receipt_file": str(run_dir / ci_receipt_filename()),
            "load": f"kbu clearinghouse load {shard_root} --json",
            "verify": (
                "kbu clearinghouse verify "
                f"{shard_root} --run-id <execution id from load> "
                f"--receipt {run_dir / ci_receipt_filename()} --json"
            ),
        },
        "warnings": warnings,
    }
    publish_file = run_dir / PUBLISH_FILENAME
    _write_atomic(publish_file, json.dumps(record, indent=2, sort_keys=True) + "\n")
    return {**record, "publish_file": str(publish_file)}


# ---------------------------------------------------------------------------
# The steps, each small enough to be read
# ---------------------------------------------------------------------------


def _read_plan(path: Path) -> Dict[str, Any]:
    """``PLAN.json``, or a refusal naming the verb that writes it."""
    if not path.exists():
        raise PublishRefused(
            "plan",
            f"no plan at {path}. Stage 1 writes it: `kbu ontomap "
            "council-plan`. Publishing without it would have no query "
            "version, no bench and no mapping hash -- i.e. no partition key.",
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise PublishRefused(
            "plan", f"{path}: expected a JSON object, got {type(raw).__name__}"
        )
    return dict(raw)


def _resolve_verdicts(
    plan: Mapping[str, Any], given: Optional[Union[str, Path]], *, run_dir: Path
) -> Path:
    """The run's verdict parquet, or a refusal naming the verb that writes it.

    The default is ``verdicts.parquet`` in the SCRATCH directory the plan
    itself recorded (via its absolute ``items_file``), not a re-resolution of
    ``$KBU_SCRATCH_DIR``: the plan already wrote down where it put its
    artefacts, and re-deriving the root could answer differently from the
    stage that actually wrote there -- which would read as "not exported yet".
    """
    if given is not None:
        path = Path(given).expanduser()
        if not path.is_file():
            raise PublishRefused(
                "verdicts", f"--verdicts {path} is not a file."
            )
        return path.resolve()
    items_file = str(plan.get("items_file") or "")
    if not items_file:
        raise PublishRefused(
            "verdicts",
            f"PLAN.json in {run_dir} names no items_file, so the run's "
            "scratch directory cannot be recovered and the default verdict "
            "parquet path is unknown. Pass --verdicts explicitly.",
        )
    candidate = Path(items_file).expanduser().parent / "verdicts.parquet"
    if not candidate.is_file():
        raise PublishRefused(
            "verdicts",
            f"no verdict parquet at {candidate}. Stage 5 writes it: "
            "`kbu ontomap council-export`. Publishing without it would shard "
            "nothing, and a shard run over zero rows is a producer failure "
            "that reads as a clean load.",
        )
    return candidate.resolve()


def _gate_report(
    plans: Sequence[Any], manifest: Any, cs: Any
) -> List[Dict[str, Any]]:
    """The per-table preview ``kbu clearinghouse plan`` reports.

    The same numbers, from the same place: ``estimate_rows`` is metadata-only
    by contract (here, the parquet footer), so the gate never scans the
    source to produce a count it then throws away.
    """
    rows: List[Dict[str, Any]] = []
    for table_plan in plans:
        source = next(
            s for s in manifest.sources if s.name == table_plan.source_name
        )
        adapter = cs.get_adapter(source)
        rows.append(
            {
                "table": table_plan.table,
                "source": table_plan.source_name,
                "entity_type": table_plan.entity_type,
                "kind": table_plan.kind,
                "row_count": adapter.estimate_rows(),
                "estimate_method": adapter.estimate_rows_method(),
                "parameter_set": dict(table_plan.parameter_set or {}),
                "parameter_set_hash": table_plan.parameter_set_hash,
            }
        )
    return rows


def _assert_partition(
    plans: Sequence[Any], *, plan: Mapping[str, Any], manifest_path: Path
) -> None:
    """Refuse a render whose partition is not this run's, before any row.

    Three refusals, and the first two are the ones that would otherwise be
    invisible until someone queried the lake:

    * The rendered hash is the INERT un-rendered one. Every one of the 47,341
      clearinghouse functions would then read as answered by whichever bench
      got to that slot first, and ``current_state()`` would make "un-run at
      THIS version" a query that always returns nothing.
    * The rendered hash is not the hash ``PLAN.json`` recorded. Stage 1
      defined "un-run" as the absence of a current-state row at THAT hash, so
      publishing at any other one leaves the whole plan still un-run.
    * The render produced a table other than ``function_result``. The
      functions were read from ``function_entity``/``function_content``; a
      second entity for a function that already has one is not recoverable by
      a re-run.
    """
    tables = sorted({p.table for p in plans})
    if tables != [_TARGET_TABLE]:
        raise PublishRefused(
            "target_table",
            f"{manifest_path} plans {tables} but this preset must write only "
            f"[{_TARGET_TABLE!r}]. The functions were READ from "
            "function_entity/function_content in stage 1, so re-declaring "
            "their identity here would mint a SECOND entity for a function "
            "that already has one.",
        )
    rendered = {p.parameter_set_hash for p in plans}
    if rendered == {UNRENDERED_PARAMETER_SET_HASH}:
        raise PublishRefused(
            "unrendered",
            f"{manifest_path} hashes to the INERT un-rendered parameter set "
            f"{UNRENDERED_PARAMETER_SET_HASH}, which the preset's header "
            "names for exactly this check. The markers were not substituted, "
            "and a load at that hash would make all 47,341 clearinghouse "
            "functions read as answered by whichever bench got there first.",
        )
    expected = str(plan["parameter_set_hash"])
    if rendered != {expected}:
        raise PublishRefused(
            "parameter_set_hash",
            f"{manifest_path} would publish at parameter_set_hash "
            f"{sorted(rendered)} but PLAN.json recorded {expected}. Stage 1 "
            "defined 'un-run' as the absence of a current-state row at THAT "
            "hash, so publishing at any other one would leave every function "
            "in this plan still un-run -- and would charge the next run for "
            "all of them again.",
        )


def _check_inventory(
    run_manifest: Mapping[str, Any], *, shard_root: Path
) -> List[Dict[str, Any]]:
    """Check the sharder's own ``run.json`` back, and return its batch rows.

    THE COMPLETENESS CONTRACT, checked where it can still be fixed. The
    consumer's ingest gate verifies each batch's sha256 against the bytes, its
    row count against the file and its hash range against the keys it filters
    on -- so a batch whose inventory entry is missing any of those cannot be
    verified on arrival at all, and the only place that is cheap to repair is
    here, before the directory is synced.
    """
    from kbutillib.domains.kbase.berdl.clearinghouse_schema import (  # noqa: PLC0415
        PARAMETER_SET_TABLE,
    )

    batches = [dict(entry) for entry in run_manifest.get("batches") or ()]
    # THE PARAMETER-SET REGISTRY BATCH IS NOT PAYLOAD, and counting it as
    # payload is how an empty publish would slip through: the registry is
    # written from the manifest's own declared parameter set, so it has a row
    # whether or not a single verdict was exported. The rows that matter are
    # the <type>_result rows.
    payload = [
        entry for entry in batches if entry.get("table") != PARAMETER_SET_TABLE
    ]
    payload_rows = sum(int(entry.get("rows") or 0) for entry in payload)
    if not payload_rows:
        raise PublishRefused(
            "shard_run",
            f"the shard run at {shard_root} published {payload_rows} "
            f"{_TARGET_TABLE} row(s) across {len(payload)} payload batch(es) "
            f"(it did write {len(batches) - len(payload)} parameter-set "
            "registry batch, which carries a row regardless). A shard run "
            "over zero rows is a PRODUCER failure, not a clean load: it would "
            "sync, pass every integrity check, append nothing and exit zero, "
            "which is indistinguishable from a re-run of a landed run.",
        )
    required = ("sha256", "rows", "hash_min", "hash_max")
    incomplete = [
        {
            "batch_id": entry.get("batch_id"),
            "missing": [field for field in required if not entry.get(field)],
        }
        for entry in batches
        if any(not entry.get(field) for field in required)
    ]
    if incomplete:
        raise PublishRefused(
            "inventory",
            f"{len(incomplete)} batch(es) in {shard_root}/run.json are "
            f"missing part of the completeness contract: {incomplete[:3]}. "
            "The consumer verifies a batch's bytes against its sha256, its "
            "row count against the file and its hash range against the keys "
            "fill-only filters on; a batch without them cannot be verified on "
            "arrival.",
        )
    return [
        {
            "table": entry.get("table"),
            "batch_id": entry.get("batch_id"),
            "file": entry.get("file"),
            "rows": entry.get("rows"),
            "bytes": entry.get("bytes"),
            "sha256": entry.get("sha256"),
            "hash_min": entry.get("hash_min"),
            "hash_max": entry.get("hash_max"),
            "unit_id": entry.get("unit_id"),
        }
        for entry in batches
    ]


def _account(
    plan: Mapping[str, Any], verdicts_path: Path, batches: Sequence[Mapping[str, Any]]
) -> tuple:
    """The ITEM accounting the receipt reconciles against, and the declines.

    ITEMS ARE NOT ROWS, in either direction, and the receipt has to be able to
    say so:

    * ``items_submitted`` is the whole un-run set ``PLAN.json`` recorded --
      ``abstain + fanout``, which is the export's ``inputs``. An item whose
      every call failed produced NO row, so it is invisible to the shard run
      and to anything downstream of the export; those residual items are
      exactly what "loaded 19,268 of 19,276" is about, and only the producer
      can count them.
    * ``result_rows_sharded`` is a ROW count. One item can carry several GO
      terms, so it is reported separately and never stands in for the item
      count.

    The declines are tallied from the PUBLISHED PARQUET's own
    ``decline_reason`` column rather than from the export's return value: the
    parquet is the artefact being published, so a tally taken from it cannot
    disagree with what lands.
    """
    import pyarrow.parquet as pq  # noqa: PLC0415 - keep the module import-cheap

    plan_counts = dict(plan.get("counts") or {})
    submitted = int(plan_counts.get("abstain", 0)) + int(
        plan_counts.get("fanout", 0)
    )
    table = pq.read_table(str(verdicts_path), columns=["decline_reason"])
    published = table.num_rows
    declines: Dict[str, int] = {}
    for value in table.column("decline_reason").to_pylist():
        if value is None:
            continue
        declines[str(value)] = declines.get(str(value), 0) + 1

    warnings: List[str] = []
    if not published:
        warnings.append(
            f"the verdict parquet at {verdicts_path} holds NO rows. A shard "
            "run over zero rows is a producer failure, not a clean load."
        )
    unresolved = submitted - published
    if unresolved < 0:
        warnings.append(
            f"the export published {published} row(s) for {submitted} "
            "submitted item(s) -- MORE rows than inputs, which the one-row-"
            "per-input contract forbids. items_unresolved is reported as 0 "
            "and the receipt will not reconcile."
        )
        unresolved = 0
    elif unresolved:
        warnings.append(
            f"{unresolved} of {submitted} submitted item(s) have no "
            "published row: their council item has no verdict, so they are "
            "genuinely un-run and the next `council-plan` will re-select "
            "them. Run the retry pass before publishing if that is not what "
            "you want."
        )
    counts = {
        # The three the receipt reconciles against.
        "items_submitted": submitted,
        "items_with_verdict": published,
        "items_unresolved": unresolved,
        # Rows, which are NOT items -- and the PARAMETER-SET REGISTRY batch is
        # neither: it carries one row per declared parameter set, from the
        # manifest rather than from the data, so counting it here would make
        # the receipt's row reconciliation off by the size of the registry.
        "result_rows_sharded": sum(
            int(entry.get("rows") or 0)
            for entry in batches
            if entry.get("table") == _TARGET_TABLE
        ),
        # The plan's own partition counts, carried so the receipt's reader can
        # see what the run was cut from without opening PLAN.json.
        "plan_universe": plan_counts.get("universe"),
        "plan_done": plan_counts.get("done"),
        "plan_unrun": plan_counts.get("unrun"),
        "plan_submit": plan_counts.get("submit"),
        "plan_abstain": plan_counts.get("abstain"),
        "plan_fanout": plan_counts.get("fanout"),
    }
    return counts, declines, warnings


def ci_receipt_filename() -> str:
    """The receipt's filename, from the ingest side rather than restated.

    Imported lazily and read through a function so this module keeps no
    module-level dependency on the berdl stack (see :data:`PUBLISH_FILENAME`
    for the same reason) while still having exactly ONE definition of the name
    the two halves of stage 5 exchange.
    """
    from kbutillib.domains.kbase.berdl.clearinghouse_ingest import RECEIPT_FILENAME

    return RECEIPT_FILENAME


def _write_atomic(path: Path, text: str) -> Path:
    """Write ``path`` atomically.

    Atomic for the reason every file on this surface is: the run directory is
    Dropbox-synced and a consumer reads these back, so a reader must never see
    a half-written file -- it would be parsed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path
