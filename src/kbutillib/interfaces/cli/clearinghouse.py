"""``kbu clearinghouse`` — read verbs over the fifteen-table clearinghouse.

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
        click.echo(f"present: {len(data.get('present', []))} of {data.get('requested', 0)}")
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


# ---------------------------------------------------------------------------
# Hash-input parsing (a --hash value or a --hashes-file, no hash handling)
# ---------------------------------------------------------------------------


def _read_hashes(
    hash_value: Optional[str], hashes_file: Optional[str]
) -> list[str]:
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
@click.option("--type", "entity_type", default=None, help="Entity type (required unless --all-types).")
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
