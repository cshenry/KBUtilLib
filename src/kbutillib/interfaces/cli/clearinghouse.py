"""``kbu clearinghouse`` — read AND operator verbs over the fifteen-table clearinghouse.

The read verbs (``tables``/``stats``/``known``/``show``/``content``/``results``/
``sources``/``health``) report on the lakehouse and work off-pod, degrading
rather than failing. The operator verbs run the bootstrap load: ``plan`` and
``shard`` (the SHARD stage -- anywhere, no pod, no credentials) and ``load`` and
``verify`` (the INGEST stage -- IN-POD ONLY, refusing early off-pod with the
locus named). Every verb -- read or operator -- emits the SAME versioned JSON
envelope under ``--json`` and obeys the same stdout discipline.

Every verb here is a thin facade over
:class:`~kbutillib.domains.kbase.berdl.clearinghouse_capability.ClearinghouseCapability`.
This module contains **no query logic, no table-name construction, and no hash
handling of its own** — it only calls the sanctioned read surface, shapes the
answer into the published output contract, and renders it. Table names come
solely from the capability (which routes them through
:func:`clearinghouse_schema.table_name`); hashes are encoded and bound solely
by the capability. The CLI never concatenates a table name, never formats a
hash into SQL, and never reaches past the abstraction to run its own SQL.

THE OUTPUT CONTRACT (a published interface — a separate dashboard PRD consumes
it and nothing else). Under ``--json`` every verb emits EXACTLY ONE object::

    {"schema_version": 1, "verb": "stats", "generated_at": "...",
     "locus": "off_pod", "data": {...}, "warnings": [...]}

Three structural rules:

1. HUMAN OUTPUT IS A RENDERING OF THE SAME DICT. Each verb builds ONE
   structure (via a ``_build_*`` helper) and then either serialises it
   (``--json``) or renders it as a table (:func:`_render`). There is NO second
   code path that computes anything: the renderer takes the already-built dict,
   so a test can patch :func:`_render` and assert it received exactly what the
   serialiser would have emitted.
2. ``schema_version`` is bumped only on a BREAKING change to ``data``. Additive
   fields do NOT bump it. A consumer pinning major ``1`` MUST tolerate unknown
   keys.
3. ``warnings`` is NEVER EMPTY FOR A DEGRADED ANSWER. An off-pod
   ``stats --include-files`` that cannot read file counts reports that in
   ``warnings`` and OMITS the file fields — it never silently returns partial
   data that looks complete.

STDOUT DISCIPLINE. Under ``--json`` stdout carries the envelope and NOTHING
else — RFC 8259-clean, exactly one object, no banner, no progress line, no
warning text. Every diagnostic and warning goes to STDERR. Warnings ALSO appear
in the envelope's ``warnings`` list (that is where a dashboard reads them);
stderr is for the human watching a run. This is the line kubectl / aws /
gcloud / gh / terraform all draw, and the failure mode for tools that do not is
intermixed log lines corrupting the JSON payload.

See ``agent-io/docs/kbu-clearinghouse-cli.md`` for the verb set and — leading
it — the locus table (which verbs work off-pod and which do not).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

import click

#: The output-contract schema version. Bumped ONLY on a breaking change to a
#: verb's ``data`` shape; additive fields do not bump it (see module docstring
#: rule 2).
SCHEMA_VERSION = 1

#: Fragmentation threshold for :func:`health`: a table whose average Iceberg
#: data-file size is below this is flagged as fragmented. ~8 MiB is the
#: platform's target file size; the pathology this catches is real (a table
#: observed at 2.76M rows across 139 files averaging 288 KiB).
HEALTH_MIN_AVG_FILE_SIZE_BYTES = 8 * 1024 * 1024


# ---------------------------------------------------------------------------
# Envelope + emit
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with a ``Z`` suffix."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _envelope(
    verb: str,
    *,
    locus: str,
    data: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    """Build the one published envelope object every verb emits.

    This is the ONLY structure a verb produces. ``--json`` serialises it
    verbatim; the human path hands the very same object to :func:`_render`.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "verb": verb,
        "generated_at": _now_iso(),
        "locus": locus,
        "data": data,
        "warnings": list(warnings),
    }


def _emit(envelope: dict[str, Any], *, as_json: bool) -> None:
    """Emit one built envelope, honouring the stdout discipline.

    Under ``--json``: the envelope and NOTHING else goes to stdout (one clean
    JSON object); every warning is ALSO mirrored to stderr for the human, but
    stdout stays a pristine payload. Otherwise: :func:`_render` writes the
    human table to stdout and warnings go to stderr.
    """
    warnings = envelope.get("warnings", [])
    if as_json:
        for warning in warnings:
            click.echo(f"warning: {warning}", err=True)
        click.echo(json.dumps(envelope))
        return
    _render(envelope)
    for warning in warnings:
        click.echo(f"warning: {warning}", err=True)


# ---------------------------------------------------------------------------
# Human rendering — one function, driven from the built envelope only
# ---------------------------------------------------------------------------


def _render(envelope: dict[str, Any]) -> None:
    """Render a built envelope as a human-readable table on stdout.

    Takes the SAME dict the ``--json`` path serialises — there is no second
    computation path. Tests patch this function and assert it received exactly
    the object the serialiser would have emitted (output-contract rule 1). The
    rendering is intentionally simple and derives everything from ``data``.
    """
    data = envelope.get("data", {})
    verb = envelope.get("verb", "")

    rows: Optional[list[dict[str, Any]]] = None
    if verb in ("tables", "stats", "health"):
        rows = data.get("tables")
    elif verb == "sources":
        rows = data.get("sources")
    elif verb == "known":
        click.echo(
            f"present: {len(data.get('present', []))} of {data.get('requested', 0)}"
        )
        for h in data.get("present", []):
            click.echo(f"  {h}")
        return
    elif verb == "content":
        rows = data.get("content")
    elif verb == "results":
        rows = data.get("results")
    elif verb == "show":
        click.echo(f"type={data.get('entity_type')} hash={data.get('hash')}")
        click.echo(f"present: {data.get('present')}")
        click.echo(f"content rows: {len(data.get('content', []))}")
        click.echo(f"current-state results: {len(data.get('results', []))}")
        return
    elif verb == "plan":
        rows = data.get("tables")
    elif verb == "shard":
        rows = data.get("shards")
    elif verb == "load":
        click.echo(
            f"run_id={data.get('run_id')} namespace={data.get('namespace')} "
            f"dry_run={data.get('dry_run')}"
        )
        rows = data.get("tables")
    elif verb == "verify":
        click.echo(f"run_id={data.get('run_id')} namespace={data.get('namespace')}")
        for line in data.get("discrepancies", []):
            click.echo(f"discrepancy: {line}")
        rows = data.get("tables")

    if rows is None:
        # Fallback: print the data dict compactly (should not normally happen).
        click.echo(json.dumps(data, default=str))
        return

    if not rows:
        click.echo("(no rows)")
        return

    _render_table(rows)


def _render_table(rows: list[dict[str, Any]]) -> None:
    """Render a list of uniform dict rows as a fixed-width text table."""
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    widths = {
        col: max(len(col), *(len(_cell(row.get(col))) for row in rows))
        for col in columns
    }
    header = "  ".join(f"{col:<{widths[col]}}" for col in columns)
    click.echo(header)
    click.echo("-" * len(header))
    for row in rows:
        click.echo(
            "  ".join(f"{_cell(row.get(col)):<{widths[col]}}" for col in columns)
        )


def _cell(value: Any) -> str:
    """Format one cell value for the human table."""
    if value is None:
        return "—"
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return str(value)


# ---------------------------------------------------------------------------
# Capability construction
# ---------------------------------------------------------------------------


def _capability() -> Any:
    """Construct a :class:`ClearinghouseCapability` (lazy import — no pod dep).

    Imported inside the function so importing this CLI module never pulls a
    pod-only dependency and so ``kbu --help`` stays fast and import-safe.
    """
    from ...domains.kbase.berdl.clearinghouse_capability import (
        ClearinghouseCapability,
    )

    return ClearinghouseCapability()


def _locus(cap: Any) -> str:
    """Report the capability's execution locus, defensively."""
    return cap._locus()  # noqa: SLF001 -- the only locus source is the capability


def _require_in_pod_or_refuse(cap: Any, verb: str) -> None:
    """Refuse an in-pod-only operator verb off-pod, EARLY, naming the locus.

    The load/verify verbs write (or read back a write) through a Spark session
    that exists only inside the BERDL pod. The refusal must be EARLY -- before
    ``ingest_shards``/``verify_run`` resolves any namespace, opens the ledger, or
    makes any transport call -- and it must NAME THE LOCUS as the reason: a user
    who just ran ``kbu clearinghouse stats`` from a laptop will run
    ``kbu clearinghouse load`` next, and a bare ``BerdlLoadRefusedError`` from
    deep in a transport is not an adequate answer. Resolving the locus here and
    raising a :class:`click.ClickException` gives a clean non-zero exit with a
    stderr message that says exactly why, in both human and ``--json`` mode,
    with zero transport calls made.
    """
    locus = _locus(cap)
    if locus != "in_pod":
        raise click.ClickException(
            f"clearinghouse {verb} is IN-POD ONLY and this process is "
            f"{locus!r}: it writes through a Spark session that exists only "
            "inside the BERDL JupyterHub pod, so nothing has been staged and no "
            "transport call has been made. Run this from inside the pod. (The "
            f"locus {locus!r} is the reason, not a missing flag.)"
        )


# ---------------------------------------------------------------------------
# Hash-input parsing (a --hash value or a --hashes-file, no hash handling)
# ---------------------------------------------------------------------------


def _read_hashes(hash_value: Optional[str], hashes_file: Optional[str]) -> list[str]:
    """Collect caller-supplied hash strings from ``--hash`` and/or a file.

    This does NO encoding, validation, or normalisation of the hashes — that is
    the capability's job (Rule 4: every hash is encoded and bound there). This
    only gathers the raw strings the operator supplied. A file contributes one
    hash per non-blank line.
    """
    out: list[str] = []
    if hash_value:
        out.append(hash_value)
    if hashes_file:
        with open(hashes_file, encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped:
                    out.append(stripped)
    return out


# ---------------------------------------------------------------------------
# ``kbu clearinghouse`` group
# ---------------------------------------------------------------------------


@click.group(name="clearinghouse")
def clearinghouse_cmd() -> None:
    """Read verbs over the fifteen-table BERDL clearinghouse.

    All read verbs work off-pod and DEGRADE rather than fail (an unavailable
    in-pod-only field is omitted and reported in ``warnings``). Under ``--json``
    every verb emits exactly one envelope object; see the module docs for the
    output contract and the docs page for the locus table.
    """


# ---------------------------------------------------------------------------
# tables
# ---------------------------------------------------------------------------


def _build_tables(cap: Any) -> dict[str, Any]:
    """Build the ``tables`` envelope: the fifteen tables with counts + specs."""
    tables = cap.tables()
    return _envelope(
        "tables",
        locus=_locus(cap),
        data={"tables": tables},
        warnings=[],
    )


@clearinghouse_cmd.command(name="tables")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def tables_cmd(as_json: bool) -> None:
    """List the fifteen tables with kind, entity_type, partition spec, row count."""
    _emit(_build_tables(_capability()), as_json=as_json)


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


def _build_stats(cap: Any, *, include_files: bool) -> dict[str, Any]:
    """Build the ``stats`` envelope: per-table row counts (+ optional files).

    The capability owns the degradation: off-pod with ``include_files`` it
    returns the row-count rows WITHOUT file fields and populates its own
    ``warnings`` list. Those warnings are lifted straight into the envelope so
    a degraded answer never has an empty ``warnings`` (output-contract rule 3).
    """
    result = cap.stats(include_files=include_files)
    return _envelope(
        "stats",
        locus=_locus(cap),
        data={"tables": result.get("tables", [])},
        warnings=list(result.get("warnings", [])),
    )


@clearinghouse_cmd.command(name="stats")
@click.option(
    "--include-files",
    "include_files",
    is_flag=True,
    default=False,
    help="Also report Iceberg data-file counts and average sizes (IN-POD ONLY; "
    "omitted with a warning off-pod).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def stats_cmd(include_files: bool, as_json: bool) -> None:
    """Per-table row counts; with --include-files, data-file counts and sizes."""
    _emit(
        _build_stats(_capability(), include_files=include_files),
        as_json=as_json,
    )


# ---------------------------------------------------------------------------
# known
# ---------------------------------------------------------------------------


def _build_known(cap: Any, *, entity_type: str, hashes: list[str]) -> dict[str, Any]:
    """Build the ``known`` envelope: which supplied hashes are in ``<T>_entity``."""
    warnings: list[str] = []
    if not hashes:
        warnings.append("no hashes supplied (--hash or --hashes-file); nothing probed.")
    present = cap.known(entity_type, hashes)
    return _envelope(
        "known",
        locus=_locus(cap),
        data={
            "entity_type": entity_type,
            "requested": len(hashes),
            "present": present,
        },
        warnings=warnings,
    )


@clearinghouse_cmd.command(name="known")
@click.option("--type", "entity_type", required=True, help="Entity type.")
@click.option("--hash", "hash_value", default=None, help="A single hash to probe.")
@click.option(
    "--hashes-file",
    "hashes_file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="File of hashes, one per line.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def known_cmd(
    entity_type: str,
    hash_value: Optional[str],
    hashes_file: Optional[str],
    as_json: bool,
) -> None:
    """Report which of the supplied hashes are present in <type>_entity."""
    hashes = _read_hashes(hash_value, hashes_file)
    _emit(
        _build_known(_capability(), entity_type=entity_type, hashes=hashes),
        as_json=as_json,
    )


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def _build_show(cap: Any, *, entity_type: str, hash_value: str) -> dict[str, Any]:
    """Build the ``show`` envelope: presence + content + current-state results.

    Composes three sanctioned capability reads for one hash — this is
    orchestration, NOT query logic: entity-table presence via ``known()``, the
    type-specialized content row via ``content()``, and current-state results
    via ``results()``. The capability owns every table name, hash encoding, and
    query.
    """
    present = bool(cap.known(entity_type, [hash_value]))
    content = cap.content(entity_type, [hash_value])
    results = cap.results(entity_type, [hash_value])
    return _envelope(
        "show",
        locus=_locus(cap),
        data={
            "entity_type": entity_type,
            "hash": hash_value,
            "present": present,
            "content": content,
            "results": results,
        },
        warnings=[],
    )


@clearinghouse_cmd.command(name="show")
@click.option("--type", "entity_type", required=True, help="Entity type.")
@click.option("--hash", "hash_value", required=True, help="The hash to show.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def show_cmd(entity_type: str, hash_value: str, as_json: bool) -> None:
    """Show one entity: presence + type-specialized content + current-state results."""
    _emit(
        _build_show(_capability(), entity_type=entity_type, hash_value=hash_value),
        as_json=as_json,
    )


# ---------------------------------------------------------------------------
# content
# ---------------------------------------------------------------------------


def _build_content(
    cap: Any, *, entity_type: str, hashes: list[str], all_types: bool
) -> dict[str, Any]:
    """Build the ``content`` envelope: content rows for the supplied hashes.

    ``--all-types`` reads the explicitly-lossy ``all_content`` UNION-ALL view
    (shared columns only) via ``content_all_types()``; without it, the full
    type-specialized rows come from ``content()`` for the named type.
    """
    warnings: list[str] = []
    if not hashes:
        warnings.append("no hashes supplied (--hash or --hashes-file); nothing read.")
    if all_types:
        warnings.append(
            "--all-types reads the lossy all_content view: only columns common "
            "to all five content tables are returned (no sequence, "
            "fasta_reference, definition, ...). Name a --type without "
            "--all-types for full rows."
        )
        rows = cap.content_all_types(hashes)
    else:
        rows = cap.content(entity_type, hashes)
    return _envelope(
        "content",
        locus=_locus(cap),
        data={
            "entity_type": None if all_types else entity_type,
            "all_types": all_types,
            "requested": len(hashes),
            "content": rows,
        },
        warnings=warnings,
    )


@clearinghouse_cmd.command(name="content")
@click.option(
    "--type",
    "entity_type",
    default=None,
    help="Entity type (required unless --all-types).",
)
@click.option("--hash", "hash_value", default=None, help="A single hash.")
@click.option(
    "--hashes-file",
    "hashes_file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help="File of hashes, one per line.",
)
@click.option(
    "--all-types",
    "all_types",
    is_flag=True,
    default=False,
    help="Read the explicitly-lossy all_content UNION-ALL view across all five "
    "types (shared columns only).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def content_cmd(
    entity_type: Optional[str],
    hash_value: Optional[str],
    hashes_file: Optional[str],
    all_types: bool,
    as_json: bool,
) -> None:
    """Content rows for the supplied hashes; --all-types for the lossy view."""
    if not all_types and not entity_type:
        raise click.UsageError("--type is required unless --all-types is given.")
    hashes = _read_hashes(hash_value, hashes_file)
    _emit(
        _build_content(
            _capability(),
            entity_type=entity_type or "",
            hashes=hashes,
            all_types=all_types,
        ),
        as_json=as_json,
    )


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


def _build_results(
    cap: Any,
    *,
    entity_type: str,
    hash_value: Optional[str],
    source: Optional[str],
    result_type: Optional[str],
) -> dict[str, Any]:
    """Build the ``results`` envelope: current-state results for ``<T>_result``.

    With ``--hash`` the point-filtered ``results()`` is used; without it, the
    whole-table ``current_state()`` is used. ``--source`` and ``--result-type``
    are passed through to the capability as the (list-shaped) filters it
    accepts.
    """
    sources = [source] if source else None
    result_types = [result_type] if result_type else None
    if hash_value:
        rows = cap.results(
            entity_type,
            [hash_value],
            sources=sources,
            result_types=result_types,
        )
    else:
        rows = cap.current_state(
            entity_type,
            sources=sources,
            result_types=result_types,
        )
    return _envelope(
        "results",
        locus=_locus(cap),
        data={
            "entity_type": entity_type,
            "hash": hash_value,
            "source": source,
            "result_type": result_type,
            "results": rows,
        },
        warnings=[],
    )


@clearinghouse_cmd.command(name="results")
@click.option("--type", "entity_type", required=True, help="Entity type.")
@click.option("--hash", "hash_value", default=None, help="Filter to one hash.")
@click.option("--source", "source", default=None, help="Filter to one source.")
@click.option(
    "--result-type", "result_type", default=None, help="Filter to one result_type."
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def results_cmd(
    entity_type: str,
    hash_value: Optional[str],
    source: Optional[str],
    result_type: Optional[str],
    as_json: bool,
) -> None:
    """Current-state results for <type>_result (whole-table, or filtered by hash)."""
    _emit(
        _build_results(
            _capability(),
            entity_type=entity_type,
            hash_value=hash_value,
            source=source,
            result_type=result_type,
        ),
        as_json=as_json,
    )


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------


def _build_sources(cap: Any, *, entity_type: Optional[str]) -> dict[str, Any]:
    """Build the ``sources`` envelope: distinct source values with row counts."""
    rows = cap.sources(entity_type)
    return _envelope(
        "sources",
        locus=_locus(cap),
        data={"entity_type": entity_type, "sources": rows},
        warnings=[],
    )


@clearinghouse_cmd.command(name="sources")
@click.option(
    "--type",
    "entity_type",
    default=None,
    help="Restrict to one entity type (default: all five result tables).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def sources_cmd(entity_type: Optional[str], as_json: bool) -> None:
    """Distinct source values with row counts, per result table."""
    _emit(_build_sources(_capability(), entity_type=entity_type), as_json=as_json)


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


def _build_health(cap: Any) -> dict[str, Any]:
    """Build the ``health`` envelope: data-file fragmentation, threshold-flagged.

    Composes ``stats(include_files=True)`` and flags every table whose average
    Iceberg data-file size is below :data:`HEALTH_MIN_AVG_FILE_SIZE_BYTES`. It
    only REPORTS the fragmentation condition — compaction is owned elsewhere.
    Off-pod the file fields are unavailable, so the capability's own warning is
    lifted into the envelope and no table can be evaluated (a degraded answer
    with a non-empty ``warnings`` — output-contract rule 3).
    """
    result = cap.stats(include_files=True)
    warnings = list(result.get("warnings", []))
    fragmented: list[dict[str, Any]] = []
    evaluated = 0
    for report in result.get("tables", []):
        avg = report.get("avg_file_size_bytes")
        if avg is None:
            # File fields absent (off-pod, or an empty table): cannot evaluate.
            continue
        evaluated += 1
        if avg < HEALTH_MIN_AVG_FILE_SIZE_BYTES:
            fragmented.append(
                {
                    "name": report.get("name"),
                    "avg_file_size_bytes": avg,
                    "data_file_count": report.get("data_file_count"),
                    "row_count": report.get("row_count"),
                }
            )
    if evaluated == 0 and not warnings:
        warnings.append(
            "no table could be evaluated for fragmentation (no data-file sizes "
            "available); run health in-pod for file metadata."
        )
    return _envelope(
        "health",
        locus=_locus(cap),
        data={
            "threshold_bytes": HEALTH_MIN_AVG_FILE_SIZE_BYTES,
            "tables_evaluated": evaluated,
            "tables": fragmented,
        },
        warnings=warnings,
    )


@clearinghouse_cmd.command(name="health")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def health_cmd(as_json: bool) -> None:
    """Flag tables whose average data-file size is below the fragmentation threshold."""
    _emit(_build_health(_capability()), as_json=as_json)


# ===========================================================================
# Operator verbs: plan / shard / load / verify
#
# These drive the SHARD stage (plan, shard -- anywhere, no pod, no credentials)
# and the INGEST stage (load, verify -- IN-POD ONLY). They are the same facade
# discipline as the read verbs: each builds ONE envelope via a ``_build_*``
# helper and emits it under the SAME output contract. The command layer holds
# NO ingest logic of its own -- ``plan``/``shard`` call the sanctioned
# shard-stage module functions (clearinghouse_manifest.shard_plan and the
# sharder), and ``load``/``verify`` call ClearinghouseCapability.ingest_shards /
# .verify_run, which own the dev 1206 pre-write assertion, the dev 1194
# postflight check, and the run ledger. A verb NEVER weakens those guards: a
# write-target mismatch or an ambiguous ledger is reported and the verb exits
# non-zero; it never drives past the guard.
# ===========================================================================


def _shard_stage() -> Any:
    """Lazy-import the shard-stage modules (pure; no pod, no credentials).

    Returned as a tuple ``(manifest_module, shard_module)``. Imported inside
    the function so ``kbu --help`` and the read verbs never pull pyarrow or the
    manifest parser, mirroring :func:`_capability`.
    """
    from ...domains.kbase.berdl import (
        clearinghouse_manifest as manifest_module,
    )
    from ...domains.kbase.berdl import (
        clearinghouse_shard as shard_module,
    )

    return manifest_module, shard_module


def _manifest_error_types() -> tuple[type[BaseException], ...]:
    """Return the manifest/shard-time error types ``plan``/``shard`` translate.

    A :class:`~clearinghouse_manifest.ManifestError` (the plan-time validation
    failure that names the offending key) becomes a clean non-zero
    :class:`click.ClickException` rather than a traceback. Imported lazily so the
    CLI import stays pod-free; falls back to :class:`ValueError` (ManifestError's
    own base) if the import is unavailable.
    """
    try:
        from ...domains.kbase.berdl.clearinghouse_manifest import ManifestError

        return (ManifestError,)
    except Exception:  # pragma: no cover - defensive: keep the CLI importable
        return (ValueError,)


def _default_run_id(shard_dir: str) -> str:
    """Derive a default ``run_id`` from a shard directory's name.

    Resuming a load is calling ``load`` again with the same ``run_id``; keying
    it to the shard directory name gives a stable default so a plain
    ``kbu clearinghouse load <dir>`` resumes its own prior run without the
    operator having to remember an id. ``--run-id`` overrides it.
    """
    from pathlib import Path

    name = Path(shard_dir).resolve().name
    return name or "run"


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def _build_plan(manifest_path: str) -> dict[str, Any]:
    """Build the ``plan`` envelope: per-table row counts and target shard sizes.

    Resolves the manifest through
    :func:`clearinghouse_manifest.load_manifest` +
    :func:`~clearinghouse_manifest.shard_plan` (the single plan-time validator --
    an invalid manifest raises :class:`ManifestError` naming the offending key,
    which :func:`plan_cmd` turns into a non-zero exit) and reports, per target
    table, the row count and the shard count/target size sharding WOULD produce.
    It WRITES NOTHING: row counts come from streaming each source adapter's
    records read-only (the sanctioned source read), and the shard count is the
    same ``ceil(estimate_bytes / target_bytes)`` the sharder uses. ``plan`` runs
    anywhere; it is not locus-gated.
    """
    import math

    manifest_module, shard_module = _shard_stage()
    manifest = manifest_module.load_manifest(manifest_path)
    plans = manifest_module.shard_plan(manifest)  # validates; raises on bad key

    target_bytes = shard_module.DEFAULT_TARGET_BYTES
    tables: list[dict[str, Any]] = []
    # Count each source's records ONCE and reuse the count for every target
    # table that source feeds (a source with kinds=[entity, content] shards the
    # same records to two tables), and size the shards from the adapter estimate.
    source_stats: dict[str, tuple[int, int]] = {}
    for plan in plans:
        stats = source_stats.get(plan.source_name)
        if stats is None:
            src = next(s for s in manifest.sources if s.name == plan.source_name)
            adapter = shard_module.get_adapter(src)
            row_count = sum(1 for _ in adapter.iter_records())
            est_bytes = max(1, adapter.estimate_bytes())
            source_stats[plan.source_name] = (row_count, est_bytes)
            stats = source_stats[plan.source_name]
        row_count, est_bytes = stats
        shard_count = max(1, math.ceil(est_bytes / target_bytes))
        tables.append(
            {
                "table": plan.table,
                "source": plan.source_name,
                "entity_type": plan.entity_type,
                "kind": plan.kind,
                "row_count": row_count,
                "estimated_source_bytes": est_bytes,
                "target_shard_bytes": target_bytes,
                "estimated_shards": shard_count,
            }
        )
    return _envelope(
        "plan",
        locus="anywhere",
        data={"manifest": manifest_path, "tables": tables},
        warnings=[],
    )


@clearinghouse_cmd.command(name="plan")
@click.argument("manifest", type=click.Path(exists=True, dir_okay=False))
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def plan_cmd(manifest: str, as_json: bool) -> None:
    """Resolve a manifest; report per-table row counts and target shard sizes. Writes nothing."""
    try:
        envelope = _build_plan(manifest)
    except _manifest_error_types() as exc:
        # An invalid manifest exits non-zero with the offending key named (the
        # ManifestError message already names the source/block/key), and nothing
        # is written -- shard_plan validates before any file could be produced.
        raise click.ClickException(str(exc)) from exc
    _emit(envelope, as_json=as_json)


# ---------------------------------------------------------------------------
# shard
# ---------------------------------------------------------------------------


def _build_shard(
    manifest_path: str, *, out_dir: str, target_bytes: Optional[int]
) -> dict[str, Any]:
    """Build the ``shard`` envelope: the bronze shards written under ``out_dir``.

    Thin over :func:`clearinghouse_shard.shard_manifest`, which validates the
    manifest at plan time (so a bad manifest fails BEFORE any file is written)
    and writes one directory per target table of sorted bronze parquet. Runs
    anywhere -- sharding needs CPU and disk, no pod and no credentials. Reports
    one row per written shard (table, batch, path, rows, hash range).
    """
    manifest_module, shard_module = _shard_stage()
    kwargs: dict[str, Any] = {}
    if target_bytes is not None:
        kwargs["target_bytes"] = target_bytes
    # Resolve the manifest to a Manifest object here (raising ManifestError,
    # which the shard command wraps as a ClickException) BEFORE any file is
    # written: shard_manifest takes the parsed object, not a path, and
    # validates the whole plan before writing, so a bad manifest fails with
    # nothing on disk.
    manifest = manifest_module.load_manifest(manifest_path)
    reports = shard_module.shard_manifest(manifest, out_dir, **kwargs)
    shards = [
        {
            "table": report.table,
            "batch": report.batch_id,
            "path": str(report.path),
            "rows": report.rows,
            "hash_min": report.hash_min,
            "hash_max": report.hash_max,
            "split": report.split,
        }
        for report in reports
    ]
    return _envelope(
        "shard",
        locus="anywhere",
        data={
            "manifest": manifest_path,
            "out_dir": out_dir,
            "shards": shards,
            "shard_count": len(shards),
        },
        warnings=[],
    )


@clearinghouse_cmd.command(name="shard")
@click.argument("manifest", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--out",
    "out_dir",
    required=True,
    type=click.Path(file_okay=False),
    help="Bronze output directory (one subdirectory per target table).",
)
@click.option(
    "--target-bytes",
    "target_bytes",
    type=int,
    default=None,
    help="Target uncompressed bytes per shard (default ~512 MiB).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def shard_cmd(
    manifest: str, out_dir: str, target_bytes: Optional[int], as_json: bool
) -> None:
    """Build bronze parquet shards from a manifest under --out. Runs anywhere."""
    try:
        envelope = _build_shard(manifest, out_dir=out_dir, target_bytes=target_bytes)
    except _manifest_error_types() as exc:
        raise click.ClickException(str(exc)) from exc
    _emit(envelope, as_json=as_json)


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------


def _build_load(
    cap: Any,
    *,
    shard_dir: str,
    run_id: str,
    dry_run: bool,
    reconcile: bool,
) -> dict[str, Any]:
    """Build the ``load`` envelope: the run ledger's outcome for each shard.

    IN-POD ONLY -- but the locus refusal happens in :func:`load_cmd` BEFORE this
    is reached, so a bare :class:`BerdlLoadRefusedError` from deep in a
    transport never surfaces (success criterion (a)). This is thin over
    :meth:`ClearinghouseCapability.ingest_shards`, which owns the dev 1206
    pre-write assertion, the dev 1194 postflight check, and the JSON-lines run
    ledger. ``--dry-run`` is the operator's last gate: ingest_shards' own
    ``dry_run=True`` resolves namespaces, runs the pre-write assertion, reports
    the action each shard WOULD take, and writes nothing -- no ``load()``, no
    ledger line. ``--reconcile`` resolves an ambiguous ``"started"`` batch
    against the table's Iceberg snapshot history rather than refusing.
    """
    result = cap.ingest_shards(
        shard_dir,
        run_id=run_id,
        dry_run=dry_run,
        reconcile=reconcile,
    )
    warnings: list[str] = []
    if dry_run:
        warnings.append(
            "dry-run: no rows were ingested and no ledger line was written; "
            "the reported action for each shard is what a real load WOULD do."
        )
    return _envelope(
        "load",
        locus=_locus(cap),
        data={
            "shard_dir": shard_dir,
            "run_id": result.get("run_id", run_id),
            "namespace": result.get("namespace"),
            "dry_run": result.get("dry_run", dry_run),
            "reconcile": reconcile,
            "tables": result.get("tables", []),
        },
        warnings=warnings,
    )


@clearinghouse_cmd.command(name="load")
@click.argument("shard_dir", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--run-id",
    "run_id",
    default=None,
    help="Run id to ingest/resume under (default: derived from the shard-dir name).",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="The operator's last gate: run the pre-write assertion and report the "
    "action each shard would take, but write nothing (genuinely read-only).",
)
@click.option(
    "--reconcile",
    "reconcile",
    is_flag=True,
    default=False,
    help="Resolve an ambiguous 'started' batch against the table's Iceberg "
    "snapshot history instead of refusing.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def load_cmd(
    shard_dir: str,
    run_id: Optional[str],
    dry_run: bool,
    reconcile: bool,
    as_json: bool,
) -> None:
    """Ingest shards (IN-POD ONLY): verify postflight and write the run ledger.

    Runs in the FOREGROUND to completion and is expected to be killed by a pod
    restart. It is NOT a daemon; resumability comes from the ledger -- re-run
    the same command with the same --run-id.
    """
    cap = _capability()
    # The locus refusal must be EARLY and must name the locus as the reason: a
    # user who just ran `stats` off their laptop will run `load` next, and a
    # bare BerdlLoadRefusedError from deep in a transport is not an adequate
    # answer. This resolves the locus at THIS boundary, before ingest_shards
    # touches any namespace, ledger, or transport.
    _require_in_pod_or_refuse(cap, "load")
    resolved_run_id = run_id or _default_run_id(shard_dir)
    _emit(
        _build_load(
            cap,
            shard_dir=shard_dir,
            run_id=resolved_run_id,
            dry_run=dry_run,
            reconcile=reconcile,
        ),
        as_json=as_json,
    )


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def _build_verify(cap: Any, *, shard_dir: str, run_id: str) -> dict[str, Any]:
    """Build the ``verify`` envelope: a completed run re-checked against its ledger.

    IN-POD ONLY (the refusal is in :func:`verify_cmd`, before this). Thin over
    :meth:`ClearinghouseCapability.verify_run`, which reads the run ledger and
    confirms every ingested table's live ``COUNT(*)`` is at least the ledger's
    recorded rows and that every recorded snapshot still exists. A discrepancy
    is REPORTED in ``warnings`` (and the per-table ``ok`` flag), not raised --
    the whole point of verify is to tell the operator what a load reported FAILED
    on a null postflight actually did, so it must answer rather than fail.
    """
    result = cap.verify_run(shard_dir, run_id=run_id)
    discrepancies = list(result.get("discrepancies", []))
    return _envelope(
        "verify",
        locus=_locus(cap),
        data={
            "shard_dir": shard_dir,
            "run_id": result.get("run_id", run_id),
            "namespace": result.get("namespace"),
            "tables": result.get("tables", []),
            "discrepancies": discrepancies,
        },
        warnings=discrepancies,
    )


@clearinghouse_cmd.command(name="verify")
@click.argument("shard_dir", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--run-id",
    "run_id",
    default=None,
    help="Run id to verify (default: derived from the shard-dir name).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def verify_cmd(shard_dir: str, run_id: Optional[str], as_json: bool) -> None:
    """Re-check a completed run's row counts and snapshots against the ledger (IN-POD ONLY)."""
    cap = _capability()
    _require_in_pod_or_refuse(cap, "verify")
    resolved_run_id = run_id or _default_run_id(shard_dir)
    _emit(
        _build_verify(cap, shard_dir=shard_dir, run_id=resolved_run_id),
        as_json=as_json,
    )
