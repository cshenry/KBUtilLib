"""``kbu ontomap`` -- the function-to-GO pipeline's operator surface.

This is the operator's entry point to a STANDING, QUOTED, APPROVED run of the
``function_to_go`` task: assigning GO molecular-function terms to protein
function descriptions. Five stages, in order, each one a verb:

1. **PLAN** -- detect the un-run functions. "Un-run" means no current-state
   ``function_result`` row at this run's clearinghouse ``parameter_set_hash``,
   where the parameter set is ``{query: "<name>@<version>",
   roster_fingerprint, mapping_hash}``. ``council-plan``.
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

WHAT IS IMPLEMENTED HERE. Only the group and ``version``. The seven pipeline
verbs are REGISTERED but each exits non-zero with "not implemented yet" --
they are filled in by later tasks. A stub that exited 0 would be worse than no
stub at all, because a rehearsal of the whole pipeline would pass straight
over it and report success.
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
# The seven pipeline verbs -- registered, NOT yet implemented
# ---------------------------------------------------------------------------

#: The pipeline verbs this module registers but does not implement, with the
#: stage each belongs to. They are registered NOW so the operator surface and
#: its help listing are settled before the stages land, and each one exits
#: NON-ZERO: a stub that exited 0 would let a rehearsal of the whole pipeline
#: pass over a missing stage and report success.
_PLANNED_VERBS: tuple[tuple[str, str], ...] = (
    (
        "council-plan",
        "Stage 1: detect the un-run functions and write the run plan.",
    ),
    (
        "council-quote",
        "Stage 2: price a plan -- items, per-seat tokens, wall clock, concurrency.",
    ),
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
        "council-export",
        "Stage 5: export the verdicts, keyed on query_id, for publication.",
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
