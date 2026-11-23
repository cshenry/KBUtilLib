"""``kbu dataset`` — the operator surface over KBDL's dataset mirror.

WHAT THIS DRIVES. KBDL serves versioned, full-snapshot parquet copies of
selected BERDL lakehouse (Iceberg) tables to external partners. The pod-side
half -- export, verification, manifest, upload, registration -- lives in
:mod:`kbutillib.domains.kbase.berdl.dataset_publish`; the HTTP half lives in
:class:`kbutillib.domains.external.kbdl_service_utils.KBDLServiceUtils`. This
module is the thin CLI over both and contains NO publishing logic of its own:
``publish``/``status`` call :class:`~...dataset_publish.DatasetPublisher`, and
``list``/``hold``/``prune``/``pull`` call the client's dataset methods.

The one exception, and it is deliberate: ``pull``'s install discipline is
HERE, because it is the only verb whose correctness is about the LOCAL
filesystem (see :func:`_pull`). Nothing else in either half knows what a
partner's download directory should look like.

THE LOCUS TABLE (see ``agent-io/docs/kbu-dataset-cli.md`` for the full one):

================  ==============================================
verb              where it runs
================  ==============================================
``list``          anywhere -- one HTTP GET
``status``        IN-POD ONLY -- reads the Iceberg snapshot history
``publish``       IN-POD ONLY -- exports through the pod's Spark
``pull``          anywhere -- HTTP + the local filesystem
``hold``          anywhere -- one HTTP POST
``prune``         anywhere -- HTTP POSTs only
================  ==============================================

``status`` and ``publish`` refuse off-pod EARLY, with the locus named and
before any transport call, carrying the message shape
:class:`~kbutillib.domains.kbase.berdl.capability.BerdlLoadRefusedError` uses
for the clearinghouse load verbs (see :func:`_require_in_pod_or_refuse`).

THE OUTPUT CONTRACT, identical to ``kbu clearinghouse``'s. Under ``--json``
every verb emits EXACTLY ONE object::

    {"schema_version": 1, "verb": "status", "generated_at": "...",
     "locus": "off_pod", "data": {...}, "warnings": [...]}

and the human output is a RENDERING OF THE SAME DICT -- each verb builds one
structure and then either serialises it or hands it to :func:`_render`, so
there is no second code path that computes anything. Under ``--json`` stdout
carries the envelope and nothing else; warnings also go to stderr.

A REFUSAL IS NOT AN ENVELOPE. Every refusal here (off-pod, a retired version,
an existing ``--dest``, a failed digest) is a :class:`click.ClickException`:
a clean non-zero exit with the reason on stderr and NO object on stdout, in
both human and ``--json`` mode. That is the same line
``kbu clearinghouse load`` draws, and it is what keeps ``--json`` stdout
parseable -- a half-built envelope describing a refusal would be worse than
none.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import click

#: The output-contract schema version. Bumped ONLY on a breaking change to a
#: verb's ``data`` shape; additive fields do not bump it.
SCHEMA_VERSION = 1

#: A published parquet part name. Spelled here rather than imported from
#: ``dataset_publish`` so ``pull`` costs no import of the pod-side module (it
#: pulls pyarrow and the catalog); the two are asserted equal by the tests.
PART_NAME_RE = re.compile(r"^part-[0-9]{5}\.parquet$")

#: The suffix of ``pull``'s working directory: ``<dest>.kbu-partial``. A
#: SIBLING of ``--dest``, never inside it, so the one rename that installs it
#: stays on the same filesystem and so a partial download is never visible at
#: the path a partner was told to read.
PARTIAL_SUFFIX = ".kbu-partial"

#: The resume marker inside the working directory. Holds the manifest object
#: id the directory was started for, and is REMOVED just before the install:
#: its presence means "this directory is an unfinished pull of exactly that
#: manifest", which is the only thing that makes resuming it safe.
PULL_STATE_FILENAME = "pull.json"

#: Bytes read at a time when re-hashing a file already on disk (resume).
HASH_CHUNK_BYTES = 1024 * 1024

#: The ``retire_reason`` ``prune --apply`` records. Names the verb and the
#: policy, so a record read months later says why it was withdrawn.
PRUNE_REASON_TEMPLATE = "retired by kbu dataset prune --keep {keep}"


# ---------------------------------------------------------------------------
# Envelope + emit  (the contract; mirrors kbu clearinghouse exactly)
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """The current UTC time as an ISO-8601 string with a ``Z`` suffix."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _envelope(
    verb: str,
    *,
    locus: str,
    data: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    """Build the one published envelope object every verb emits."""
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

    Under ``--json``: the envelope and NOTHING else on stdout; warnings are
    mirrored to stderr for the human. Otherwise :func:`_render` writes the
    human view and warnings go to stderr.
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
# Human rendering — driven from the built envelope only
# ---------------------------------------------------------------------------


def _render(envelope: dict[str, Any]) -> None:
    """Render a built envelope on stdout.

    Takes the SAME dict ``--json`` serialises; there is no second computation
    path. Tests patch this and assert it received exactly what the serialiser
    would have emitted.
    """
    data = envelope.get("data", {})
    verb = envelope.get("verb", "")

    if verb == "list":
        _render_table(data.get("datasets") or [])
        return
    if verb == "status":
        click.echo(
            f"dataset={data.get('dataset')} status={data.get('status')} "
            f"target={data.get('target')}"
        )
        click.echo(
            f"published_version={data.get('version')} "
            f"published_snapshot={data.get('published_snapshot_id')}"
        )
        for operation in data.get("operations") or []:
            click.echo(f"  since published: {operation}")
        return
    if verb == "publish":
        click.echo(
            f"dataset={data.get('dataset')} status={data.get('status')} "
            f"dry_run={data.get('dry_run')} force={data.get('force')}"
        )
        click.echo(
            f"target={data.get('target')} skipped={data.get('skipped')} "
            f"would_skip={data.get('would_skip')}"
        )
        if data.get("reason"):
            click.echo(f"reason: {data['reason']}")
        click.echo(f"workdir={data.get('workdir')}")
        for step in data.get("steps") or []:
            click.echo(f"  step: {step}")
        if data.get("version"):
            click.echo(f"registered: {data['version']}")
        return
    if verb == "pull":
        click.echo(
            f"dataset={data.get('dataset')} version={data.get('version')} "
            f"state={data.get('state')}"
        )
        click.echo(f"manifest={data.get('manifest_object_id')}")
        click.echo(
            f"rows={data.get('row_count')} files={len(data.get('files') or [])}"
        )
        _render_table(data.get("files") or [])
        click.echo(f"installed at {data.get('dest')}")
        return
    if verb == "hold":
        click.echo(
            f"dataset={data.get('dataset')} version={data.get('version')} "
            f"action={data.get('action')}"
        )
        click.echo(f"hold_reason={data.get('record', {}).get('hold_reason')}")
        return
    if verb == "prune":
        click.echo(
            f"dataset={data.get('dataset')} keep={data.get('keep')} "
            f"applied={data.get('applied')}"
        )
        click.echo(f"published: {data.get('published_versions')}")
        click.echo(f"held (never retired): {data.get('held_versions')}")
        click.echo(f"would retire: {data.get('retire_versions')}")
        for record in data.get("retired") or []:
            click.echo(
                f"  retired {record.get('version')}: {record.get('state')}"
            )
        reclaim = data.get("reclaim") or {}
        click.echo(
            f"reclaim ({'real' if data.get('applied') else 'dry run estimate'}): "
            f"deleted={len(reclaim.get('deleted') or [])} "
            f"kept_foreign={len(reclaim.get('kept_foreign') or [])} "
            f"kept_busy={len(reclaim.get('kept_busy') or [])} "
            f"bytes_freed={reclaim.get('bytes_freed')}"
        )
        return
    click.echo(json.dumps(data, default=str))  # pragma: no cover -- no such verb


def _render_table(rows: list[dict[str, Any]]) -> None:
    """Render a list of uniform dict rows as a fixed-width text table."""
    if not rows:
        click.echo("(no rows)")
        return
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
# Locus, client and publisher construction (all lazy)
# ---------------------------------------------------------------------------


def _locus() -> str:
    """``"in_pod"`` or ``"off_pod"``, from the one canonical test.

    Delegates to
    :func:`kbutillib.domains.kbase.berdl.capability.berdl_notebook_utils_importable`
    -- the SAME importability probe ``BerdlCapability.locus()`` uses -- and
    never to an environment variable: an env var set by a laptop shell is how
    an off-pod process talks itself into believing it has a Spark session.
    Imported inside the function so ``kbu --help`` stays import-cheap.
    """
    from ...domains.kbase.berdl.capability import berdl_notebook_utils_importable

    return "in_pod" if berdl_notebook_utils_importable() else "off_pod"


def _require_in_pod_or_refuse(verb: str, locus: str) -> None:
    """Refuse an in-pod-only verb off-pod, EARLY, naming the locus.

    ``status`` reads the Iceberg snapshot history and ``publish`` exports
    through it; both go through a Spark session that exists only inside the
    BERDL JupyterHub pod. The refusal must land BEFORE any transport call and
    must name the locus as the reason -- a user who just ran
    ``kbu dataset list`` from a laptop will run ``kbu dataset publish`` next,
    and a bare error from deep inside a transport is not an adequate answer.

    The message is the shape
    :class:`~kbutillib.domains.kbase.berdl.capability.BerdlLoadRefusedError`
    carries for the clearinghouse load verbs, and that exception is the
    raised :class:`click.ClickException`'s ``__cause__`` so the type survives
    for a programmatic caller; the ClickException is what turns it into a
    clean non-zero exit with no traceback.
    """
    if locus == "in_pod":
        return
    from ...domains.kbase.berdl.capability import BerdlLoadRefusedError

    refusal = BerdlLoadRefusedError(
        f"kbu dataset {verb} is IN-POD ONLY and this process is {locus!r}: it "
        "works through a Spark session over the Iceberg table, which exists "
        "only inside the BERDL JupyterHub pod, so nothing has been exported, "
        "nothing staged and no transport call made. Run this from inside the "
        f"pod. (The locus {locus!r} is the reason, not a missing flag.)"
    )
    raise click.ClickException(str(refusal)) from refusal


def _client() -> Any:
    """A :class:`KBDLServiceUtils` (lazy import -- ``requests`` is not free)."""
    from ...domains.external.kbdl_service_utils import KBDLServiceUtils

    return KBDLServiceUtils()


def _publisher() -> Any:
    """A :class:`DatasetPublisher` over the real in-pod ports and a real client.

    IN-POD ONLY by construction: :class:`InPodLakehouse` and
    :class:`InPodStaging` resolve the pod's Spark session and MinIO client.
    Callers must already have passed :func:`_require_in_pod_or_refuse`.

    The ``KBDLServiceUtils`` instance is what satisfies ``KBDLPort``
    structurally -- it has ``upload_object``, ``poll_jobs_until_terminal``,
    ``get_dataset_version`` and ``publish_dataset_version`` -- with no import
    in either direction.
    """
    from ...domains.kbase.berdl.dataset_publish import (
        DatasetPublisher,
        InPodLakehouse,
        InPodStaging,
    )

    return DatasetPublisher(InPodLakehouse(), InPodStaging(), _client())


# ---------------------------------------------------------------------------
# ``kbu dataset`` group
# ---------------------------------------------------------------------------


@click.group(name="dataset")
def dataset_cmd() -> None:
    """The KBDL dataset mirror: publish, inspect, pull, hold and prune.

    ``status`` and ``publish`` are IN-POD ONLY; ``list``, ``pull``, ``hold``
    and ``prune`` work anywhere. Under ``--json`` every verb emits exactly one
    envelope object -- see ``agent-io/docs/kbu-dataset-cli.md`` for the
    contract, the locus table and the publication gates.
    """


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _build_list(client: Any, locus: str) -> dict[str, Any]:
    """Build the ``list`` envelope: every dataset and its latest version."""
    datasets = client.list_datasets()
    warnings: list[str] = []
    stale = [
        row.get("name")
        for row in datasets
        if isinstance(row, dict) and not row.get("latest_version")
    ]
    if stale:
        warnings.append(
            "no version has ever been registered for: "
            + ", ".join(str(name) for name in stale)
        )
    return _envelope(
        "list", locus=locus, data={"datasets": datasets}, warnings=warnings
    )


@dataset_cmd.command(name="list")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def list_cmd(as_json: bool) -> None:
    """Every mirrored dataset with its latest registered version."""
    _emit(_build_list(_client(), _locus()), as_json=as_json)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def _build_status(publisher: Any, name: str, locus: str) -> dict[str, Any]:
    """Build the ``status`` envelope from ``DatasetPublisher.status``."""
    verdict = dict(publisher.status(name))
    warnings = [str(w) for w in verdict.pop("warnings", [])]
    verdict.setdefault("dataset", name)
    return _envelope("status", locus=locus, data=verdict, warnings=warnings)


@dataset_cmd.command(name="status")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def status_cmd(name: str, as_json: bool) -> None:
    """Where NAME stands against its latest registered version (IN-POD ONLY)."""
    locus = _locus()
    _require_in_pod_or_refuse("status", locus)
    _emit(_build_status(_publisher(), name, locus), as_json=as_json)


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------


def _build_publish(
    publisher: Any,
    name: str,
    locus: str,
    *,
    dry_run: bool,
    force: bool,
    keep_workdir: bool,
) -> dict[str, Any]:
    """Build the ``publish`` envelope from ``DatasetPublisher.publish``."""
    report = dict(
        publisher.publish(
            name, dry_run=dry_run, force=force, keep_workdir=keep_workdir
        )
    )
    warnings = [str(w) for w in report.pop("warnings", [])]
    return _envelope("publish", locus=locus, data=report, warnings=warnings)


@dataset_cmd.command(name="publish")
@click.argument("name")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Report the full plan and touch nothing (always reports, whatever "
    "the status or approval state).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Publish even when the status is unchanged/compaction_only -- which "
    "is how a schema-only or README-only change gets published.",
)
@click.option(
    "--keep-workdir",
    is_flag=True,
    default=False,
    help="Keep the local export working directory after a successful publish.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def publish_cmd(
    name: str,
    dry_run: bool,
    force: bool,
    keep_workdir: bool,
    as_json: bool,
) -> None:
    """Export, verify, upload and register one pinned snapshot (IN-POD ONLY)."""
    locus = _locus()
    _require_in_pod_or_refuse("publish", locus)
    _emit(
        _build_publish(
            _publisher(),
            name,
            locus,
            dry_run=dry_run,
            force=force,
            keep_workdir=keep_workdir,
        ),
        as_json=as_json,
    )


# ---------------------------------------------------------------------------
# pull — the only verb whose correctness is about the local filesystem
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    """The sha256 hex digest of a file's bytes, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _fetch_verified(client: Any, object_id: str, path: Path) -> bool:
    """Ensure ``path`` holds exactly the bytes of ``object_id``. Returns resumed.

    THE RESUME IS THE DIGEST, not a length or an mtime. A file already at
    ``path`` is accepted only if its sha256 IS the object id -- which is what
    a KBDL content object id means -- so a truncated file from a killed run is
    re-downloaded rather than trusted, and nothing has to be remembered
    between runs beyond the bytes themselves.

    The download goes through
    :meth:`~kbutillib.domains.external.kbdl_service_utils.KBDLServiceUtils.download_object`,
    which hashes as it streams and only then renames into place, so a
    mismatch never leaves a partial file at ``path``. The digest is then
    checked AGAIN here against the id, because this function's contract is
    about the bytes on disk and a client that skipped the check (a reference
    object id, a stand-in) must not be able to make that contract a lie.

    Returns:
        ``True`` if the file was already there and verified (resumed),
        ``False`` if it was downloaded.
    """
    resumed = path.exists() and _sha256_file(path) == object_id
    if not resumed:
        client.download_object(object_id, path)
    digest = _sha256_file(path)
    if digest != object_id:
        raise click.ClickException(
            f"{path.name} has sha256 {digest} but its KBDL object id is "
            f"{object_id}: a content object's id IS the sha256 of its bytes, "
            "so these are not those bytes. Nothing was installed and the "
            "working directory is left for a retry."
        )
    return resumed


def _resolve_workdir(dest: Path, manifest_object_id: str) -> tuple[Path, bool]:
    """The working directory for this pull, created or resumed. Returns (W, resumed).

    W is ``<dest>.kbu-partial`` and is created with ``exist_ok=False``: the
    create must FAIL on a directory that is already there rather than adopt
    it, because adopting one would silently mix two different versions' files
    under one manifest.

    An existing W is resumable ONLY when its ``pull.json`` names the same
    manifest object id. A W with a different id, or with no ``pull.json`` at
    all, is refused BY NAME -- the operator is told exactly which directory
    to move aside. It is never deleted here: a directory this tool did not
    create, and whose contents it cannot account for, is not its to remove.
    """
    workdir = dest.with_name(dest.name + PARTIAL_SUFFIX)
    state_path = workdir / PULL_STATE_FILENAME
    if os.path.lexists(workdir):
        recorded: Optional[str] = None
        if state_path.is_file():
            try:
                recorded = json.loads(state_path.read_text(encoding="utf-8")).get(
                    "manifest_object_id"
                )
            except (ValueError, OSError):
                recorded = None
        if recorded != manifest_object_id:
            raise click.ClickException(
                f"{workdir} already exists and is not a resumable pull of this "
                f"version: its {PULL_STATE_FILENAME} names "
                f"{recorded!r}, not {manifest_object_id!r}. Nothing has been "
                "downloaded. Move or remove that directory, or pull to a "
                "different --dest."
            )
        return workdir, True
    workdir.mkdir(parents=True, exist_ok=False)
    state_path.write_text(
        json.dumps({"manifest_object_id": manifest_object_id}) + "\n",
        encoding="utf-8",
    )
    return workdir, False


def _pull(
    client: Any, name: str, version: str, dest: str
) -> tuple[dict[str, Any], list[str]]:
    """Download and install one dataset version at ``dest``. Never overwrites.

    THE ORDER IS THE WHOLE POINT, and every step of it is a refusal:

    1. ``dest`` MUST NOT EXIST. Checked with :func:`os.path.lexists`, so an
       empty directory, a file, a live symlink and a BROKEN symlink are all
       refused alike -- a broken symlink is the one ``Path.exists()`` would
       wave through, and renaming onto it would follow it somewhere else.
    2. The version is fetched. A RETIRED one (410) exits non-zero printing
       the tombstone: a withdrawn snapshot must never land on a partner's
       disk looking like a current one.
    3. A sibling working directory W is created or resumed
       (:func:`_resolve_workdir`). Everything is downloaded into W, so a
       partial pull is never visible at ``dest``.
    4. THE MANIFEST OBJECT'S OWN BYTES are written to ``W/MANIFEST.json``
       verbatim -- never re-serialised from a parsed dict. Those bytes hash
       to the manifest object id; a re-serialisation would not, and the
       partner's copy would fail its own verification.
    5. Every file: the name must match :data:`PART_NAME_RE` (so a manifest
       cannot name ``../../etc/x`` or a dotfile), the bytes must hash to the
       object id, and the PARQUET FOOTER's ``num_rows`` must equal the
       manifest's ``row_count`` for that file. The footer check is what
       catches a file that is intact but wrong -- the right bytes of the
       wrong export.
    6. ONLY THEN the install: remove ``W/pull.json``, re-check that ``dest``
       still does not exist, and :func:`os.rename` W to ``dest``. ONE rename,
       so ``dest`` appears complete or not at all; never a copy, which would
       be observable half-done.

    On ANY failure W is left exactly as it is, so a rerun resumes it rather
    than re-downloading what already verified.
    """
    warnings: list[str] = []
    destination = Path(dest)
    if not destination.name:
        raise click.ClickException(
            f"--dest {dest!r} has no final path component, so there is no "
            "directory to create; name the directory to install."
        )

    # 1. --dest must not exist, in ANY form.
    if os.path.lexists(destination):
        raise click.ClickException(
            f"--dest {destination} already exists (as a file, directory or "
            "symlink) and this verb never overwrites anything. Nothing has "
            "been downloaded. Pull to a path that does not exist."
        )

    # 2. The version. A retired one is a refusal that prints the tombstone.
    from ...domains.external.kbdl_service_utils import KBDLGoneError

    try:
        answer = client.get_dataset_version(name, version)
    except KBDLGoneError as exc:
        raise click.ClickException(
            f"{name} version {version} has been RETIRED and is not served; "
            f"nothing was downloaded. Tombstone: {exc.tombstone or exc.body!r}"
        ) from exc

    record = answer.get("version") or {}
    manifest_object_id = record.get("manifest_object_id")
    if not manifest_object_id:
        raise click.ClickException(
            f"the version record for {name} {version} carries no "
            f"manifest_object_id, so there is nothing to verify a download "
            f"against: {sorted(record) if isinstance(record, dict) else record!r}"
        )

    # 3. The working directory, created or resumed.
    workdir, resumed_workdir = _resolve_workdir(destination, manifest_object_id)
    if resumed_workdir:
        warnings.append(
            f"resuming the unfinished pull in {workdir}; files already there "
            "that hash to their object id are not downloaded again."
        )

    # 4+5. Everything is downloaded and verified inside W. A failure in here
    # that is not already a refusal still has to read as one: the operator
    # needs to be told that W is intact and that re-running resumes it, which
    # a bare transport traceback does not say.
    try:
        manifest, files, total_rows, manifest_resumed = _download_and_verify(
            client, workdir, manifest_object_id, warnings
        )
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001 -- re-raised with the locus named
        raise click.ClickException(
            f"the pull of {name} version {version} failed "
            f"({type(exc).__name__}: {exc}). Nothing was installed at "
            f"{destination} and {workdir} is left exactly as it is, so "
            "re-running this command resumes it rather than starting over."
        ) from exc

    # 6. The install: drop the resume marker, re-check, ONE rename.
    (workdir / PULL_STATE_FILENAME).unlink(missing_ok=True)
    if os.path.lexists(destination):
        raise click.ClickException(
            f"--dest {destination} appeared while this pull was verifying, so "
            f"the install was refused rather than overwrite it. Everything is "
            f"verified and intact in {workdir}; move the path that appeared "
            "aside and rename it yourself, or pull again to a free --dest."
        )
    os.rename(workdir, destination)

    data = {
        "dataset": name,
        "version": record.get("version"),
        "state": record.get("state"),
        "dest": str(destination),
        "workdir": str(workdir),
        "manifest_object_id": manifest_object_id,
        "manifest_resumed": manifest_resumed,
        "workdir_resumed": resumed_workdir,
        "row_count": total_rows,
        "files": files,
        "published_at": record.get("published_at"),
        "published_by": record.get("published_by"),
        "source_snapshot_id": record.get("source_snapshot_id"),
    }
    return data, warnings


def _download_and_verify(
    client: Any,
    workdir: Path,
    manifest_object_id: str,
    warnings: list[str],
) -> tuple[dict[str, Any], list[dict[str, Any]], int, bool]:
    """Fill ``workdir`` with the verified manifest, README and every part file.

    Steps 4 and 5 of :func:`_pull`, where every byte is checked. Writes ONLY
    inside ``workdir``; the caller owns the install.
    """
    # 4. The manifest OBJECT's own bytes, verbatim.
    manifest_path = workdir / "MANIFEST.json"
    manifest_resumed = _fetch_verified(client, manifest_object_id, manifest_path)
    manifest_bytes = manifest_path.read_bytes()
    try:
        manifest = json.loads(manifest_bytes)
    except ValueError as exc:
        raise click.ClickException(
            f"the manifest object {manifest_object_id} hashes correctly but is "
            f"not JSON: {exc}. Nothing was installed."
        ) from exc
    if not isinstance(manifest, dict) or not isinstance(
        manifest.get("files"), list
    ):
        raise click.ClickException(
            "the manifest object carries no files array, so there is nothing "
            "to download. Nothing was installed."
        )

    readme = manifest.get("readme")
    if isinstance(readme, str) and readme:
        (workdir / "README.md").write_text(readme, encoding="utf-8")
    else:
        warnings.append(
            "the manifest carries no readme, so no README.md was written "
            "beside the parquet files."
        )

    # 5. Every file: name, digest, footer.
    import pyarrow.parquet as pq

    files: list[dict[str, Any]] = []
    total_rows = 0
    for index, entry in enumerate(manifest["files"]):
        where = f"manifest.files[{index}]"
        if not isinstance(entry, dict):
            raise click.ClickException(f"{where} is not an object.")
        part = entry.get("name")
        if not isinstance(part, str) or not PART_NAME_RE.match(part):
            raise click.ClickException(
                f"{where}.name is {part!r}, which is not a published part name "
                f"({PART_NAME_RE.pattern}). A manifest does not get to name "
                "the file it is written to. Nothing was installed."
            )
        object_id = entry.get("object_id")
        if not isinstance(object_id, str) or not object_id:
            raise click.ClickException(f"{where}.object_id is missing.")
        expected_rows = entry.get("row_count")

        target = workdir / part
        file_resumed = _fetch_verified(client, object_id, target)

        footer_rows = pq.ParquetFile(str(target)).metadata.num_rows
        if footer_rows != expected_rows:
            raise click.ClickException(
                f"{part} has {footer_rows} rows in its parquet footer but the "
                f"manifest declares {expected_rows}: the bytes are intact (they "
                "hash to their object id) and still not the right export. "
                "Nothing was installed and the working directory is left for "
                "inspection."
            )
        total_rows += footer_rows
        files.append(
            {
                "name": part,
                "object_id": object_id,
                "row_count": footer_rows,
                "size_bytes": target.stat().st_size,
                "resumed": file_resumed,
            }
        )

    declared = manifest.get("row_count")
    if declared is not None and declared != total_rows:
        raise click.ClickException(
            f"the parquet footers total {total_rows} rows but the manifest "
            f"declares row_count={declared}: the manifest contradicts itself "
            "and the download cannot be certified. Nothing was installed."
        )

    return manifest, files, total_rows, manifest_resumed


@dataset_cmd.command(name="pull")
@click.argument("name")
@click.option(
    "--version",
    default="latest",
    show_default=True,
    help="The version to pull; the newest registered one by default.",
)
@click.option(
    "--dest",
    required=True,
    help="The directory to install. MUST NOT EXIST -- this verb never "
    "overwrites anything.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def pull_cmd(name: str, version: str, dest: str, as_json: bool) -> None:
    """Download a version to --dest, verifying every byte before installing."""
    data, warnings = _pull(_client(), name, version, dest)
    _emit(
        _envelope("pull", locus=_locus(), data=data, warnings=warnings),
        as_json=as_json,
    )


# ---------------------------------------------------------------------------
# hold
# ---------------------------------------------------------------------------


def _build_hold(
    client: Any, name: str, version: str, locus: str, *, reason: Optional[str]
) -> dict[str, Any]:
    """Build the ``hold`` envelope. ``reason=None`` RELEASES the hold."""
    record = client.hold_dataset_version(name, version, reason)
    return _envelope(
        "hold",
        locus=locus,
        data={
            "dataset": name,
            "version": version,
            "action": "release" if reason is None else "hold",
            "reason": reason,
            "record": record,
        },
        warnings=[],
    )


@dataset_cmd.command(name="hold")
@click.argument("name")
@click.argument("version")
@click.option("--reason", default=None, help="Place a hold, recording why.")
@click.option(
    "--release", is_flag=True, default=False, help="Release an existing hold."
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def hold_cmd(
    name: str,
    version: str,
    reason: Optional[str],
    release: bool,
    as_json: bool,
) -> None:
    """Hold VERSION of NAME against retirement, or release that hold.

    EXACTLY ONE of ``--reason`` and ``--release`` is required. Neither is a
    usage error rather than a default, because there is no safe default: a
    hold with no reason is a hold nobody can later justify removing, and
    treating a bare ``hold`` as a release would silently unprotect a version
    somebody deliberately pinned. Both together is a contradiction.
    """
    if bool(reason) == bool(release):
        raise click.UsageError(
            "exactly one of --reason TEXT (place a hold) and --release "
            "(remove one) is required: "
            + (
                "both were given, and they are opposites."
                if release
                else "neither was given, and there is no safe default."
            )
        )
    _emit(
        _build_hold(
            _client(), name, version, _locus(), reason=None if release else reason
        ),
        as_json=as_json,
    )


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------


def _version_key(record: dict[str, Any]) -> Any:
    """Sort key for a version record: numeric when it can be, else the string."""
    value = record.get("version")
    try:
        return (0, int(value))
    except (TypeError, ValueError):
        return (1, str(value))


def _prune_plan(versions: list[dict[str, Any]], keep: int) -> dict[str, Any]:
    """Which published versions ``prune --keep N`` would retire, OLDEST FIRST.

    Two rules, and the second overrides the first:

    * the newest ``keep`` published versions stay. ``keep`` is at least 1, so
      the plan can never be "retire everything" -- retiring the latest is a
      409 at the service anyway, and a prune that tried it would be asking
      the service to leave partners with nothing current.
    * EVERY HELD VERSION STAYS, however old. A hold exists precisely to
      exempt a version from this, so it is filtered out of the retire list
      here rather than left to fail at the service -- ``--apply`` stops at
      the first error, and one predictable 409 would abandon the rest of a
      correct plan.

    Retired versions are not candidates (they are already retired) and are
    not counted toward ``keep``, which counts what partners can still read.
    """
    published = [
        record
        for record in versions
        if isinstance(record, dict)
        and str(record.get("state", "")).upper() == "PUBLISHED"
    ]
    oldest_first = sorted(published, key=_version_key)
    held = [
        str(record.get("version"))
        for record in oldest_first
        if record.get("hold_reason")
    ]
    candidates = oldest_first[: max(0, len(oldest_first) - keep)]
    retire = [
        str(record.get("version"))
        for record in candidates
        if not record.get("hold_reason")
    ]
    return {
        "published_versions": [str(r.get("version")) for r in oldest_first],
        "held_versions": held,
        "retire_versions": retire,
    }


def _build_prune(
    client: Any, name: str, locus: str, *, keep: int, apply: bool
) -> dict[str, Any]:
    """Build the ``prune`` envelope: the plan, and either a preview or the act.

    WITHOUT ``--apply`` NOTHING CHANGES: the only call made is
    ``reclaim_dataset(dry_run=True, assume_retired=<exactly the versions the
    plan would retire>)``, which is what makes the preview honest -- a dry run
    that did not assume them would report the reclaim of an EMPTY retire list
    and tell the operator nothing about the bytes the real run would free.

    WITH ``--apply`` the order is retire-then-reclaim and never the reverse:
    reclamation only ever deletes objects of versions that are ALREADY
    retired, so the retires are the authorisation for the delete. Each retire
    is its own request and the loop STOPS AT THE FIRST ERROR with the records
    so far reported -- a 409 (the latest, or a hold placed since the plan was
    computed) means the plan is stale, and pressing on would act on a plan
    the service has already contradicted. Reclaim then runs over whatever did
    retire, with no ``assume_retired``.
    """
    plan = _prune_plan(client.list_dataset_versions(name), keep)
    retire_versions = plan["retire_versions"]
    warnings: list[str] = []
    retired: list[dict[str, Any]] = []
    error: Optional[str] = None

    if not apply:
        reclaim = client.reclaim_dataset(
            name, dry_run=True, assume_retired=retire_versions
        )
        warnings.append(
            "dry run: nothing was retired and nothing deleted. The reclaim "
            "figures are the service's ESTIMATE for the "
            f"{len(retire_versions)} version(s) above; re-run with --apply to "
            "act on this plan."
        )
    else:
        reason = PRUNE_REASON_TEMPLATE.format(keep=keep)
        for version in retire_versions:
            try:
                retired.append(
                    client.retire_dataset_version(name, version, reason)
                )
            except Exception as exc:  # noqa: BLE001 -- reported, not swallowed
                error = (
                    f"retiring version {version} failed ({type(exc).__name__}: "
                    f"{exc}); stopped there. {len(retired)} of "
                    f"{len(retire_versions)} version(s) were retired and the "
                    "reclaim below covers only those."
                )
                warnings.append(error)
                break
        reclaim = client.reclaim_dataset(name, dry_run=False)

    data = {
        "dataset": name,
        "keep": keep,
        "applied": bool(apply),
        **plan,
        "retired": retired,
        "reclaim": reclaim,
        "error": error,
    }
    if plan["held_versions"]:
        warnings.append(
            "held and therefore never retired by prune: "
            + ", ".join(plan["held_versions"])
        )
    return _envelope("prune", locus=locus, data=data, warnings=warnings)


@dataset_cmd.command(name="prune")
@click.argument("name")
@click.option(
    "--keep",
    type=int,
    required=True,
    help="How many of the newest published versions to keep. Must be >= 1.",
)
@click.option(
    "--apply",
    "apply_",
    is_flag=True,
    default=False,
    help="Actually retire and reclaim. Without it this is a preview that "
    "changes nothing.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def prune_cmd(name: str, keep: int, apply_: bool, as_json: bool) -> None:
    """Retire all but the newest --keep published versions, then reclaim.

    ``--keep`` MUST BE AT LEAST 1. Zero is a usage error and not a permitted
    "retire everything": the latest version is what partners read, the
    service answers 409 to retiring it, and accepting ``--keep 0`` would make
    a typo look like a policy.
    """
    if keep < 1:
        raise click.UsageError(
            f"--keep must be at least 1, got {keep}: the newest published "
            "version is what partners read and the service refuses to retire "
            "it, so there is no such thing as keeping none."
        )
    envelope = _build_prune(_client(), name, _locus(), keep=keep, apply=apply_)
    _emit(envelope, as_json=as_json)
    if envelope["data"].get("error"):
        raise SystemExit(1)
