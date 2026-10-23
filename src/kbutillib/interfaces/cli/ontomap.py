"""``kbu ontomap`` -- the function-to-GO pipeline's operator surface.

This is the operator's entry point to a STANDING, QUOTED, APPROVED run of the
``function_to_go`` task: assigning GO molecular-function terms to protein
function descriptions. Five stages, in order, each one a verb:

1. **PLAN** -- detect the un-run functions. "Un-run" means no current-state
   ``function_result`` row at this run's clearinghouse ``parameter_set_hash``,
   where the parameter set is ``{query: "<name>@<version>",
   bench_fingerprint, mapping_hash}``. ``council-plan``.

   ``bench_fingerprint`` and NOT ``roster_fingerprint``: the bench partitions
   the corpus, the transport does not. ``Roster.fingerprint()`` covers the
   endpoint kind, the route spelling, the token ceiling, the temperature and
   the extras, so moving one seat from the codex CLI to MAG moves it --
   which would re-partition all 47,341 clearinghouse functions and re-charge
   ~199M tokens for a pure cost optimisation. See
   :func:`~kbutillib.domains.ai.council.types.bench_fingerprint`.
2. **QUOTE** -- price the plan before anything is spent: items, tokens per
   seat, wall clock, concurrency the gateways will actually tolerate.
   ``council-quote``.
3. **APPROVE** -- a human says yes to a specific quote. Nothing in stage 4
   runs without an approval record naming the quote it approves.
   ``council-approve``.
4. **RUN** -- execute the approved plan (``council-run``), including the
   first-class retry pass over items whose only defect was a silently
   truncated response, and report progress with ``council-status``.
5. **PUBLISH** -- export the verdicts (``council-export``) and hand them to
   the clearinghouse manifest/shard/load/verify chain (``council-publish``).
   The join key into the clearinghouse is ``query_id`` -- the clearinghouse
   ``entity_hash``, carried through from the ranked file. NEVER the council's
   own ``items.entity_hash``, and never the definition text.

**THIS MODULE OWNS NO PIPELINE LOGIC.** Every verb is a thin facade over
:class:`~kbutillib.domains.genome.ontology_mapping.ontology_mapping_utils.OntologyMappingUtils`,
the council store and the clearinghouse chain -- all of which already exist. A
verb that reimplemented any of them would be a second place that could
disagree with the library, so there is none.

THE OUTPUT CONTRACT. Under ``--json`` every verb emits EXACTLY ONE object, the
same envelope ``kbu council`` and ``kbu clearinghouse`` publish::

    {"schema_version": 1, "verb": "version", "generated_at": "...",
     "data": {...}, "warnings": [...]}

Three structural rules, carried over verbatim:

1. HUMAN OUTPUT IS A RENDERING OF THE SAME DICT. Each verb builds one
   envelope (via a ``_build_*`` helper) and then either serialises it
   (``--json``) or hands that very object to :func:`_render`. There is no
   second code path that computes anything.
2. ``schema_version`` is bumped only on a BREAKING change to ``data``.
   Additive fields do not bump it; a consumer pinning major ``1`` must
   tolerate unknown keys.
3. ``warnings`` is never empty for a degraded answer.

STDOUT DISCIPLINE. Under ``--json``, stdout carries the envelope and nothing
else; warnings are mirrored to stderr for the human.

WHAT IS IMPLEMENTED HERE. The group, ``version``, ``council-plan``,
``council-quote`` and ``council-export``. The remaining four pipeline verbs
are REGISTERED but each exits non-zero with "not implemented yet" -- they are
filled in by later tasks. A stub that exited 0 would be worse than no stub at
all, because a rehearsal of the whole pipeline would pass straight over it and
report success.

``council-quote`` OWNS NO PRICING LOGIC EITHER. It delegates to
``kbu council quote``'s own :func:`~.council.build_quote`, which is the same
function that command calls -- two code paths that both priced a plan would
be two numbers that could disagree, and the one an operator approved would be
whichever CLI they happened to type.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Sequence

import click

#: The output-contract schema version. Bumped ONLY on a breaking change to a
#: verb's ``data`` shape; additive fields do not bump it (see module docstring
#: rule 2).
SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Envelope + emit
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """The current UTC time as an ISO-8601 string with a ``Z`` suffix."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _envelope(
    verb: str,
    *,
    data: Dict[str, Any],
    warnings: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Build the one published envelope object every verb emits."""
    return {
        "schema_version": SCHEMA_VERSION,
        "verb": verb,
        "generated_at": _now_iso(),
        "data": data,
        "warnings": list(warnings or []),
    }


def _emit(envelope: Dict[str, Any], *, as_json: bool) -> None:
    """Emit one built envelope, honouring the stdout discipline."""
    warnings = envelope.get("warnings", [])
    if as_json:
        for warning in warnings:
            click.echo(f"warning: {warning}", err=True)
        click.echo(json.dumps(envelope, default=_jsonable))
        return
    _render(envelope)
    for warning in warnings:
        click.echo(f"warning: {warning}", err=True)


def _jsonable(value: Any) -> str:
    """Last-resort encoder for a value ``json`` cannot serialise itself."""
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


def _rows(rows: Sequence[Dict[str, Any]], columns: Sequence[str]) -> None:
    """Print *rows* as a column-aligned table of *columns*."""
    if not rows:
        click.echo("(none)")
        return
    table = [[_cell(row.get(col)) for col in columns] for row in rows]
    widths = [
        max(len(col), *(len(line[i]) for line in table)) for i, col in enumerate(columns)
    ]
    click.echo("  ".join(col.ljust(widths[i]) for i, col in enumerate(columns)).rstrip())
    for line in table:
        click.echo("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(line)).rstrip())


def _cell(value: Any) -> str:
    """Format one cell value for the human table."""
    if value is None:
        return "-"
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value)
    return str(value)


def _render(envelope: Dict[str, Any]) -> None:
    """Render a built envelope on stdout.

    Takes the SAME dict the ``--json`` path serialises -- there is no second
    computation path, which is what lets a test patch this function and assert
    it received exactly what the serialiser would have emitted.
    """
    verb = str(envelope.get("verb") or "")
    data = envelope.get("data") or {}

    if verb == "council-quote":
        # Rendered by `kbu council`'s own renderer, under the verb name that
        # renderer keys on. This group adds no second rendering of a quote:
        # the two commands must print the same thing from the same dict.
        from .council import _render as _render_council

        _render_council({"verb": "quote", "data": data})
        return

    if verb == "council-plan":
        counts = data.get("counts") or {}
        click.echo(f"plan:            {data.get('plan_file')}")
        click.echo(f"query:           {data.get('query')}   profile: {data.get('profile')}")
        click.echo(f"parameter_set:   {str(data.get('parameter_set_hash'))[:12]}  "
                   f"(bench {str(data.get('bench_fingerprint'))[:12]}, "
                   f"mapping {str(data.get('mapping_hash'))[:12]})")
        click.echo(f"plan_hash:       {str(data.get('plan_hash'))[:12]}")
        click.echo(
            "counts:          "
            + ", ".join(f"{key}={counts.get(key)}" for key in
                        ("universe", "done", "unrun", "submit", "abstain", "fanout"))
        )
        concurrency = data.get("concurrency") or {}
        click.echo(
            f"chunks:          {len(data.get('chunks') or [])} of "
            f"{concurrency.get('chunk_size')}, "
            f"{concurrency.get('chunks_in_flight')} in flight "
            f"({concurrency.get('issue')})"
        )
        _rows(
            data.get("transport_allocation") or [],
            ("role", "seat_name", "gateway", "endpoint_kind", "canonical_model"),
        )
        return

    if verb == "council-export":
        counts = data.get("counts") or {}
        click.echo(f"export:          {data.get('out_path')}")
        click.echo(f"query:           {data.get('query')}")
        click.echo(f"parameter_set:   {str(data.get('parameter_set_hash'))[:12]}  "
                   f"(mapping {str(data.get('mapping_hash'))[:12]}, "
                   f"roster {str(data.get('roster_fingerprint'))[:12]})")
        click.echo(f"jobs:            {', '.join(data.get('job_ids') or []) or '-'}")
        click.echo(
            "counts:          "
            + ", ".join(
                f"{key}={counts.get(key)}"
                for key in ("inputs", "rows", "withheld", "abstained", "decided")
            )
        )
        click.echo(
            "declines:        "
            + ", ".join(
                f"{key}={counts.get(key, 0)}"
                for key in ("abstained", "no_candidate_matched", "contentless")
            )
        )
        click.echo(
            f"with go_ids:     {counts.get('with_go_ids')}   "
            f"retried: {counts.get('retried')}   "
            f"bytes: {data.get('bytes')}"
        )
        return

    if verb == "version":
        provenance = data.get("provenance") or {}
        click.echo(f"mapping_version: {data.get('mapping_version')}")
        click.echo(f"mapping_hash:    {data.get('mapping_hash')}")
        click.echo(f"go_release:      {provenance.get('go_release')}")
        click.echo(f"placeholder_rules: {provenance.get('placeholder_rules')}")
        _rows(
            provenance.get("models") or [],
            ("artifact", "ontomap_method_version", "sha256"),
        )
        params = provenance.get("params") or {}
        if params:
            click.echo(
                "params: "
                + ", ".join(f"{key}={params[key]}" for key in sorted(params))
            )
        return

    click.echo(json.dumps(data, indent=2, sort_keys=True, default=_jsonable))


# ---------------------------------------------------------------------------
# The facade
# ---------------------------------------------------------------------------


def _ontomap() -> Any:
    """Construct the :class:`OntologyMappingUtils` this invocation works through.

    One factory, so a test can patch exactly one name. The import is lazy and
    inside the function: the retrieval backend pulls numpy/sklearn/joblib, and
    ``kbu --help`` must not pay for them (nor fail on a host that has not got
    them). A host missing them becomes a clean CLI error, below, rather than an
    ImportError traceback.
    """
    from ...domains.genome.ontology_mapping.ontology_mapping_utils import (
        OntologyMappingUtils,
    )

    return OntologyMappingUtils()


def _fail(exc: BaseException) -> "click.ClickException":
    """An ontomap failure as a clean CLI error rather than a traceback."""
    return click.ClickException(f"{type(exc).__name__}: {exc}")


def _json_option(function: Any) -> Any:
    """``--json``, spelled once."""
    return click.option(
        "--json", "as_json", is_flag=True, default=False, help="Emit the JSON envelope."
    )(function)


# ---------------------------------------------------------------------------
# ``kbu ontomap`` group
# ---------------------------------------------------------------------------


@click.group(name="ontomap")
def ontomap_cmd() -> None:
    """Run the standing function-to-GO council pipeline, in five stages.

    \b
    1. `council-plan`     detect the un-run functions (no current-state
                          function_result row at this run's parameter_set_hash)
    2. `council-quote`    price that plan -- items, tokens, wall clock,
                          per-gateway concurrency -- before anything is spent
    3. `council-approve`  a human approves one specific quote; stage 4 refuses
                          to run without an approval naming it
    4. `council-run`      execute the approved plan (with the retry pass), and
                          `council-status` to watch it
    5. `council-publish`  `council-export` the verdicts, then hand them to the
                          clearinghouse manifest/shard/load/verify chain

    `version` is free and spends nothing: it reports the retrieval backend's
    identity (mapping_version, mapping_hash, provenance), which is part of
    every stage's parameter set. Under `--json` every verb emits the one
    envelope `kbu council` and `kbu clearinghouse` publish.
    """


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------


def _build_version(util: Any) -> Dict[str, Any]:
    """Build the ``version`` envelope: the retrieval backend's identity."""
    try:
        report = dict(util.mapping_version())
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001 - a refusal is a CLI error
        raise _fail(exc) from exc
    return _envelope(
        "version",
        data={
            "mapping_version": report.get("mapping_version"),
            "mapping_hash": report.get("mapping_hash"),
            "provenance": report.get("provenance") or {},
        },
        warnings=[],
    )


@ontomap_cmd.command(name="version")
@_json_option
def version_cmd(as_json: bool) -> None:
    """Report the retrieval backend's identity: version, hash, provenance.

    Spends nothing and reaches no network. ``mapping_hash`` is folded into
    every pipeline stage's clearinghouse ``parameter_set_hash``, so a backend
    swap makes previously-run functions un-run again -- which is exactly why
    this verb exists separately from the stages that consume it.
    """
    try:
        util = _ontomap()
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001 - a missing backend dep is a CLI error
        raise _fail(exc) from exc
    _emit(_build_version(util), as_json=as_json)


# ---------------------------------------------------------------------------
# council-quote -- stage 2, delegated to `kbu council quote`
# ---------------------------------------------------------------------------


@ontomap_cmd.command(name="council-quote")
@click.option(
    "--plan",
    required=True,
    type=click.Path(exists=True),
    help="Run directory holding PLAN.json, or the plan file itself.",
)
@click.option("--profile", default=None, help="Bench to quote. Default: the plan's own.")
@click.option("--seat", multiple=True, metavar="SEAT_NAME=ALIAS", help="Override one seat (repeatable).")
@click.option(
    "--meta-fire-rate",
    default=None,
    type=float,
    help="Fraction of items reaching meta. Default: 0.241, measured over n=19,268.",
)
@click.option(
    "--out",
    default=None,
    type=click.Path(),
    help="Where to write QUOTE.json. Default: the run directory the plan came from.",
)
@click.option(
    "--no-write",
    "no_write",
    is_flag=True,
    default=False,
    help="Compute and print the quote without writing QUOTE.json.",
)
@click.option("--config", "config_path", default=None, type=click.Path(exists=True, dir_okay=False), help="Council configuration YAML.")
@click.option("--store", "store_url", default=None, help="Override the council store URL.")
@_json_option
def council_quote_cmd(
    plan: str,
    profile: Optional[str],
    seat: Sequence[str],
    meta_fire_rate: Optional[float],
    out: Optional[str],
    no_write: bool,
    config_path: Optional[str],
    store_url: Optional[str],
    as_json: bool,
) -> None:
    """Stage 2: price a plan -- per-seat tokens, reuse, wall clock, dollars.

    SPENDS NOTHING. Delegates to `kbu council quote`, which is the only
    implementation; this verb exists so the five pipeline stages read as one
    sequence under `kbu ontomap`.

    Writes `QUOTE.json` into the run directory, carrying the `plan_hash` the
    approval in stage 3 binds to.
    """
    from .council import _council, build_quote

    context = click.Context(click.Command("quote"))
    context.obj = {"config_path": config_path, "store_url": store_url}
    try:
        util = _council(context)
        util.require_schema()
    except Exception as exc:  # noqa: BLE001 - an unusable store is a CLI error
        raise _fail(exc) from exc
    try:
        envelope = build_quote(
            util,
            plan=plan,
            profile=profile,
            seat=seat,
            meta_fire_rate=meta_fire_rate,
            out=out,
            write=not no_write,
        )
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001 - a bad plan is a CLI error
        raise _fail(exc) from exc
    # Re-labelled to this group's verb so the envelope says which command was
    # run, but it is the SAME dict `kbu council quote` emits.
    envelope["verb"] = "council-quote"
    _emit(envelope, as_json=as_json)


# ---------------------------------------------------------------------------
# council-plan -- stage 1
# ---------------------------------------------------------------------------


def _build_council_plan(util: Any, **kwargs: Any) -> Dict[str, Any]:
    """Build the ``council-plan`` envelope from the facade's own plan dict.

    OWNS NO PLANNING LOGIC. Every number in the envelope comes from
    :meth:`OntologyMappingUtils.council_plan`, which is the only
    implementation; this builder selects what an operator reads and warns
    where the answer is degraded.
    """
    try:
        plan = dict(util.council_plan(**kwargs))
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001 - an unusable plan is a CLI error
        raise _fail(exc) from exc

    counts = dict(plan.get("counts") or {})
    warnings = []
    if not counts.get("submit"):
        warnings.append(
            "the plan holds NO submit items: every function in the "
            f"clearinghouse universe ({counts.get('universe', 0)}) either "
            f"already has a current-state result at this parameter_set_hash "
            f"({counts.get('done', 0)}) or was declined by retrieval "
            f"({counts.get('abstain', 0)}). Stage 2 will refuse to quote it."
        )
    if counts.get("abstain"):
        warnings.append(
            f"{counts['abstain']} un-run function(s) were declined by "
            "retrieval and are NOT in the submit set; they are recorded in "
            f"{plan.get('abstain_file')} and stage 5 publishes them as "
            "decline_reason=abstained:* rather than dropping them."
        )
    return _envelope("council-plan", data=plan, warnings=warnings)


@ontomap_cmd.command(name="council-plan")
@click.option(
    "--run-dir",
    required=True,
    type=click.Path(file_okay=False),
    help="Run directory PLAN.json is written into (created if absent).",
)
@click.option(
    "--query",
    default=None,
    help='Council query as "<name>@<version>". Default: the shipped go_mf@1.',
)
@click.option("--profile", default=None, help="Bench to plan against. Default: the configured one.")
@click.option(
    "--chunk-size",
    default=None,
    type=int,
    help="Submit items per chunk. Default: 2000 (KBUtilLib issue 84 workaround).",
)
@click.option(
    "--scratch-dir",
    default=None,
    type=click.Path(file_okay=False),
    help="Local ROOT for the ranked/items files; function-to-go/<run-id>/ is "
    "appended. Default: $KBU_SCRATCH_DIR, else /scratch/<user>.",
)
@_json_option
def council_plan_cmd(
    run_dir: str,
    query: Optional[str],
    profile: Optional[str],
    chunk_size: Optional[int],
    scratch_dir: Optional[str],
    as_json: bool,
) -> None:
    """Stage 1: detect the un-run functions and write the run plan.

    SPENDS NO LLM TOKENS. It reads the clearinghouse, runs the retrieval
    stage locally and writes `PLAN.json`; the first token is spent in stage 4,
    behind a quote and an approval.

    "Un-run" means no current-state `function_result` row at this run's
    clearinghouse `parameter_set_hash`, over the parameter set
    `{query, bench_fingerprint, mapping_hash}`. `bench_fingerprint` is
    model-only, so re-routing a seat to a different gateway re-selects
    NOTHING, while a bench, query-version or mapping_hash change re-selects
    the whole corpus.

    `PLAN.json` is the only file written into the run directory. The ranked
    file, the items file and the abstain/fanout records are machine-local
    intermediates and go under the scratch directory.
    """
    from ...domains.genome.ontology_mapping.ontology_mapping_utils import (
        DEFAULT_COUNCIL_QUERY,
        PLAN_CHUNK_SIZE,
    )

    try:
        util = _ontomap()
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001 - a missing backend dep is a CLI error
        raise _fail(exc) from exc
    _emit(
        _build_council_plan(
            util,
            run_dir=run_dir,
            query=query or DEFAULT_COUNCIL_QUERY,
            chunk_size=PLAN_CHUNK_SIZE if chunk_size is None else chunk_size,
            profile=profile,
            scratch_dir=scratch_dir,
        ),
        as_json=as_json,
    )


# ---------------------------------------------------------------------------
# council-export -- stage 5
# ---------------------------------------------------------------------------


def _build_council_export(util: Any, **kwargs: Any) -> Dict[str, Any]:
    """Build the ``council-export`` envelope from the facade's export record.

    OWNS NO EXPORT LOGIC. Every number comes from
    :meth:`OntologyMappingUtils.council_export`, including its own warnings,
    which are carried through rather than restated: a withheld item is a
    DEGRADED answer and the envelope contract says ``warnings`` is never empty
    for one.
    """
    try:
        export = dict(util.council_export(**kwargs))
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001 - an unusable run dir is a CLI error
        raise _fail(exc) from exc

    counts = dict(export.get("counts") or {})
    warnings = list(export.get("warnings") or [])
    if not counts.get("rows"):
        warnings.append(
            "the export holds NO rows: the plan named "
            f"{counts.get('inputs', 0)} input function(s) and none of them "
            "produced a publishable row. A shard run over zero rows is a "
            "producer failure, not a clean load."
        )
    return _envelope("council-export", data=export, warnings=warnings)


@ontomap_cmd.command(name="council-export")
@click.option(
    "--run-dir",
    required=True,
    type=click.Path(file_okay=False, exists=True),
    help="Run directory holding PLAN.json (and RUN.json, for the job ids).",
)
@click.option(
    "--out",
    "out_path",
    default=None,
    type=click.Path(dir_okay=False),
    help="Where the parquet goes. Default: <scratch>/verdicts.parquet. A path "
    "inside the run directory is refused.",
)
@click.option(
    "--job-id",
    "job_id",
    multiple=True,
    help="Council job to read verdicts from (repeatable, in order). Default: "
    "whatever RUN.json names.",
)
@click.option(
    "--retry-job-id",
    "retry_job_id",
    multiple=True,
    help="Council job belonging to the retry pass (repeatable). Its verdicts "
    "supersede the chunk jobs' and its rows are marked retried.",
)
@click.option(
    "--scratch-dir",
    default=None,
    type=click.Path(file_okay=False),
    help="Local ROOT for the default --out; function-to-go/<run-id>/ is "
    "appended. Default: $KBU_SCRATCH_DIR, else /scratch/<user>.",
)
@_json_option
def council_export_cmd(
    run_dir: str,
    out_path: Optional[str],
    job_id: Sequence[str],
    retry_job_id: Sequence[str],
    scratch_dir: Optional[str],
    as_json: bool,
) -> None:
    """Stage 5: export the run's verdicts as PARQUET, one row per input.

    SPENDS NOTHING. Reads `PLAN.json`, the plan's abstain/fanout records and
    the council store; writes one parquet file on LOCAL SCRATCH. The published
    artifact is the shard run (`council-publish`); this file is its input, so
    writing it into the synced run directory is refused.

    ONE ROW PER INPUT `query_id`, including all three decline classes --
    `abstained:<reason>`, `no_candidate_matched` and `contentless` -- each with
    `go_ids = []`. Recording them is what stops stage 1 re-selecting them
    forever: un-run means "no current-state result row", so an ABSENT row is
    indistinguishable from "not yet run".

    An item with NO VERDICT (every call failed) is withheld and reported as a
    warning: it is genuinely un-run and the next plan must re-select it.

    `query_id` comes from the ranked data the plan carried through, never from
    a text join and never from the council's own `items.entity_hash`.
    """
    try:
        util = _ontomap()
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001 - a missing backend dep is a CLI error
        raise _fail(exc) from exc
    _emit(
        _build_council_export(
            util,
            run_dir=run_dir,
            out_path=out_path,
            job_ids=list(job_id) or None,
            retry_job_ids=list(retry_job_id) or None,
            scratch_dir=scratch_dir,
        ),
        as_json=as_json,
    )


# ---------------------------------------------------------------------------
# The remaining four pipeline verbs -- registered, NOT yet implemented
# ---------------------------------------------------------------------------

#: The pipeline verbs this module registers but does not implement, with the
#: stage each belongs to. They are registered NOW so the operator surface and
#: its help listing are settled before the stages land, and each one exits
#: NON-ZERO: a stub that exited 0 would let a rehearsal of the whole pipeline
#: pass over a missing stage and report success. ``council-plan`` and
#: ``council-quote`` have LEFT this list -- stages 1 and 2 landed -- which is
#: the expected shape of progress through the five stages.
_PLANNED_VERBS: tuple[tuple[str, str], ...] = (
    (
        "council-approve",
        "Stage 3: record a human's approval of one specific quote.",
    ),
    (
        "council-run",
        "Stage 4: execute an approved plan, including the retry pass.",
    ),
    (
        "council-status",
        "Stage 4: report progress, spend and blockers for a running plan.",
    ),
    (
        "council-publish",
        "Stage 5: hand an export to the clearinghouse manifest/shard/load/verify chain.",
    ),
)


def _register_stub(name: str, summary: str) -> None:
    """Register one not-yet-implemented verb that exits non-zero when run."""

    @ontomap_cmd.command(
        name=name,
        short_help=summary,
        help=f"{summary}\n\nNOT IMPLEMENTED YET -- registered so the operator "
        "surface is settled; running it is an error, never a silent success.",
    )
    @_json_option
    def _stub(as_json: bool, _verb: str = name) -> None:
        raise click.ClickException(
            f"kbu ontomap {_verb}: not implemented yet. The verb is registered "
            "but its stage has not landed; nothing was planned, quoted, run or "
            "published."
        )


for _verb_name, _verb_summary in _PLANNED_VERBS:
    _register_stub(_verb_name, _verb_summary)
