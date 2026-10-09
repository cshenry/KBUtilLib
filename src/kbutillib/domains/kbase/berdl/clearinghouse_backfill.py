"""The backfill driver: one command that fills the clearinghouse, unit by unit.

WHAT THIS IS FOR. ``kbu clearinghouse shard`` and ``kbu clearinghouse load``
are the two halves of the pipeline and they are deliberately separate (see
:mod:`clearinghouse_shard`). Driving them by hand over a 264M-document source
is not a thing a person can do: it is 256 shard invocations, 256 loads, 256
verifications, and a record of which of those 768 steps finished -- kept where
a pod restart cannot lose it. This module is that record and that driver.
``kbu clearinghouse backfill run <preset>`` is the one-command path: point it
at a source and it fills.

EVERYTHING PULLS, FROM INSIDE THE POD. The BERDL pod reaches out to Mongo, to
the arc store, to the genome archive -- nothing pushes to it. So the whole
run happens in one process on the pod: shard a unit onto the pod's own local
disk, ingest it (the driver reads the files and appends DataFrames, because
Spark executors cannot see the pod's local disk), verify it, record it, next.

THE UNIT OF WORK IS THE WHOLE DESIGN. A unit is the smallest thing that can be
FINISHED: a mongo unit is one two-hex-digit prefix of the sort key (a uniform
1/256 sample -- see :func:`clearinghouse_shard.unit_key_range`), a genome
archive is one archive, and a source with no natural decomposition is one
unit. A unit is sharded, ingested, VERIFIED and only then recorded done, in
that order, so a run killed anywhere loses at most the unit in flight. The
alternative -- shard everything, then load everything -- has no state between
the two halves that a restart can read, which is how you end up re-reading
264M documents to find out what you already have.

THREE UNIT STATES, AND WHY `done` IS NOT THE ONLY TERMINAL ONE:

- ``done``     -- every batch verified AND every source reached EXHAUSTION.
- ``partial``  -- ``--limit`` stopped it. The data that landed is correct and
                  stays; the unit is NOT done, and a later run without
                  ``--limit`` processes it again (fill-only discards what is
                  already there, so reprocessing is cheap and changes nothing).
- ``incomplete``-- a retryable failure remains. Exits NON-ZERO even when every
                  batch that was emitted verifies, because "the rows I wrote
                  are correct" is not "the unit is finished".

A unit that reports ``done`` on a ``--limit`` stop is the single worst failure
available here: it is a permanent, silent claim that 1/256 of the corpus has
been loaded when 12,000 rows of it have. That is why exhaustion is tracked by
the ADAPTER's own loop (:meth:`clearinghouse_shard.SourceAdapter.exhausted`)
rather than inferred from a row count -- a cursor killed at five million and a
collection that held five million produce identical counts.

RUN IDENTITY IS IMMUTABLE. At run start the registration (see
:class:`RunRegistration`) records the absolute ``--out``, the tenant and
dataset, the EXPANDED manifest's digest, the standardizer version, the source
identities, ``--slice`` and ``--limit`` -- in the run's state directory, beside
the ledger. ``--resume`` restores what was omitted and REFUSES what conflicts.
A scratch run and a production run therefore can never share a run id or a
ledger: the dataset is part of the identity, so resuming a scratch run against
production is refused by name rather than quietly appending proof of one
namespace's rows to the other's ledger.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ...identity import standardizers
from . import clearinghouse_ingest as ci
from . import clearinghouse_manifest as cm
from . import clearinghouse_schema as schema
from . import clearinghouse_shard as cs

#: The per-run state file, at the ROOT of ``--out``. It records each unit's
#: state and the run id, and it is the file ``--resume`` and ``status --out``
#: read. Rewritten atomically (temp + ``os.replace``) on every change, never
#: appended in place: a run of 256 units rewrites it 256 times and a crash
#: mid-rewrite must leave the previous state intact rather than a truncated
#: JSON document naming no units at all.
BACKFILL_STATE_FILE = "backfill-state.json"

#: The run-identity registration, in the run's STATE directory (beside the
#: ledger), not in ``--out``. The two live apart on purpose: ``--out`` is
#: regenerable sharder output an operator may delete, while the registration
#: and the ledger are the only record of what a run WAS and what it wrote.
RUN_REGISTRATION_FILE = "backfill-run.json"

#: Where ``run`` shards to when ``--out`` is omitted: the pod's own persistent
#: home, one directory per run id. Local disk is required, not incidental --
#: the driver reads these files itself and hands DataFrames to Spark.
DEFAULT_OUT_ROOT = "~/clearinghouse-shards"

#: Unit states. See the module docstring for why there are three.
UNIT_DONE = "done"
UNIT_PARTIAL = "partial"
UNIT_INCOMPLETE = "incomplete"
UNIT_PENDING = "pending"

#: How a manifest's units are derived. ``slice`` -- two-hex-digit prefixes of
#: a mongo sort key; ``adapter`` -- labels an adapter's own ``units()`` hook
#: reported; ``single`` -- one unit, the default for a source with no natural
#: decomposition. The kind decides what ``--limit`` MEANS (see
#: :meth:`BackfillRunContext.claim_record`).
UNIT_KIND_SLICE = "slice"
UNIT_KIND_ADAPTER = "adapter"
UNIT_KIND_SINGLE = "single"


class BackfillError(RuntimeError):
    """A backfill could not be planned, registered, resumed or run.

    Distinct from :class:`clearinghouse_manifest.ManifestError` (a manifest is
    wrong) and :class:`clearinghouse_ingest.ClearinghouseBatchIntegrityError`
    (what reached the lake is wrong): this is the DRIVER refusing, e.g. a
    ``--resume`` whose options contradict the run's recorded identity.
    """


# --------------------------------------------------------------------------
# Presets: manifests shipped as package data
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Preset:
    """One shipped backfill manifest."""

    name: str
    path: Path


def preset_root() -> Path:
    """The package-data directory the shipped presets live in."""
    return Path(__file__).resolve().parent / "backfills"


def available_presets() -> list[Preset]:
    """Every shipped preset, by name, in sorted order.

    Presets are PACKAGE DATA, not documentation and not a checkout-only file:
    ``kbu clearinghouse backfill run file-example`` has to work from a plain
    ``pip install kbutillib`` on a pod that has no source tree. The manifests
    and the ``file-example`` fixture are declared in ``pyproject.toml``'s
    ``[tool.setuptools.package-data]`` for exactly that reason.
    """
    root = preset_root()
    if not root.is_dir():  # pragma: no cover - defensive; it is package data
        return []
    return [
        Preset(name=path.stem, path=path) for path in sorted(root.glob("*.toml"))
    ]


def resolve_manifest_path(spec: str) -> tuple[Path, str | None]:
    """Resolve a preset NAME or a filesystem PATH to ``(path, preset_name)``.

    A shipped preset name wins over a same-named file in the cwd. That order
    is deliberate: the presets are the tested, versioned mappings, and
    ``backfill run file-example`` must mean the same thing on every pod
    regardless of what happens to be sitting in the operator's working
    directory. An explicit path (anything containing a separator, or any
    existing file) is still honoured.

    Raises:
        BackfillError: Neither a shipped preset nor a readable file.
    """
    presets = {preset.name: preset for preset in available_presets()}
    if spec in presets and os.sep not in spec:
        return presets[spec].path, spec
    candidate = Path(spec).expanduser()
    if candidate.is_file():
        return candidate, None
    raise BackfillError(
        f"{spec!r} is neither a shipped backfill preset nor a readable "
        f"manifest file. Shipped presets: {sorted(presets) or '(none)'}. "
        "Pass a preset name or a path to a manifest .toml."
    )


def describe_preset(preset: Preset) -> dict[str, Any]:
    """One row of the ``backfill list`` envelope, for one preset.

    Loads the manifest with an EMPTY environment-expansion fallback so that
    listing a preset never requires the environment a RUN of it would need: a
    ``list`` that failed because ``${ARC_ROOT}`` is unset would be unable to
    tell the operator that ``ARC_ROOT`` is what it needs. An unresolvable
    placeholder is reported as ``needs_env`` plus an ``error`` string rather
    than raised.
    """
    row: dict[str, Any] = {
        "name": preset.name,
        "path": str(preset.path),
        "adapters": [],
        "tables": [],
        "pulls_from": [],
        "needs_env": [],
    }
    try:
        manifest = cm.load_manifest(preset.path)
    except cm.ManifestError as exc:
        # Re-read without expansion to recover the declared needs_env, which
        # is the one thing that makes an unresolved placeholder actionable.
        row["error"] = str(exc)
        row["needs_env"] = _declared_needs_env(preset.path)
        return row
    row["adapters"] = sorted({source.adapter for source in manifest.sources})
    row["tables"] = _target_tables(manifest)
    if manifest.backfill is not None:
        row["pulls_from"] = list(manifest.backfill.pulls_from)
        row["needs_env"] = list(manifest.backfill.needs_env)
    return row


def _declared_needs_env(path: Path) -> list[str]:
    """``[backfill].needs_env`` read WITHOUT expanding anything.

    Used only on the error path of :func:`describe_preset`: the manifest did
    not load, so the parsed object does not exist, but the one field that
    explains WHY is sitting in the raw TOML.
    """
    try:
        data = cm.tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # pragma: no cover - defensive
        return []
    raw = data.get("backfill")
    if not isinstance(raw, Mapping):
        return []
    names = raw.get("needs_env")
    if not isinstance(names, list):
        return []
    return [str(name) for name in names]


def _target_tables(manifest: cm.Manifest) -> list[str]:
    """The target tables a manifest writes, via :func:`schema.table_name` ONLY.

    Never concatenated here. ``table_name`` is the single place an entity type
    and a kind become a table name, and a second spelling of that rule is a
    second thing to keep in step with the schema.
    """
    tables: list[str] = []
    for source in manifest.sources:
        for kind in source.kinds:
            name = schema.table_name(source.entity_type, kind)
            if name not in tables:
                tables.append(name)
    if any("result" in source.kinds for source in manifest.sources):
        # Every result source's rows need their registry row, and the sharder
        # emits that batch per unit -- so the registry is a target table of
        # this manifest even though no [[source]] names it.
        if schema.PARAMETER_SET_TABLE not in tables:
            tables.insert(0, schema.PARAMETER_SET_TABLE)
    return tables


# --------------------------------------------------------------------------
# Units of work
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class UnitPlan:
    """How one manifest decomposes into units for one invocation."""

    #: One of :data:`UNIT_KIND_SLICE` / :data:`UNIT_KIND_ADAPTER` /
    #: :data:`UNIT_KIND_SINGLE`.
    kind: str
    #: The unit labels this invocation will process, in order.
    units: tuple[str, ...]
    #: The ``--slice`` text, or ``None``.
    slice_spec: str | None = None
    #: The half-open key range ``--slice`` covers, for the envelope.
    key_range: tuple[str | None, str | None] = (None, None)
    #: Units dropped because ``--limit`` capped the UNIT COUNT (adapter units
    #: only). Reported so a capped run never looks like a complete one.
    units_dropped_by_limit: int = 0


def plan_units(
    manifest: cm.Manifest,
    *,
    slice_spec: str | None = None,
    limit: int | None = None,
) -> UnitPlan:
    """Decide the units one invocation will process.

    THE RULE, in order:

    1. A manifest with any ``mongo`` source decomposes into SLICES -- the
       two-hex-digit prefixes ``--slice`` selects, or all 256 without it.
    2. Otherwise, each source's adapter is asked for its own
       :meth:`~clearinghouse_shard.SourceAdapter.units`. If any reports more
       than the single default label, those labels are the units.
    3. Otherwise it is ONE unit, which completes only when every source and
       table in the manifest verifies.

    ``--slice`` naming a manifest with no mongo source is REFUSED, by adapter
    name. A slice is a range of a mongo sort key; accepting it against a
    parquet file would read the whole file once per prefix and then throw 255
    of those reads away in the fill-only filter, reporting success throughout.

    Raises:
        BackfillError: ``slice_spec`` was given for a non-mongo manifest, or
            is malformed.
    """
    adapters = sorted({source.adapter for source in manifest.sources})
    has_mongo = "mongo" in adapters
    if slice_spec is not None and not has_mongo:
        raise BackfillError(
            f"--slice {slice_spec!r} is only meaningful for the 'mongo' "
            f"adapter, and this manifest's adapter(s) are {adapters}. A slice "
            "is a range of a mongo sort key's two-hex-digit prefix; the "
            f"{adapters[0]!r} adapter has no sort key to slice. Its units are "
            "whatever its units() hook reports (one unit, for a source with no "
            "natural decomposition). Drop --slice."
        )
    if has_mongo:
        try:
            units = cs.slice_prefixes(slice_spec)
            key_range = cs.slice_key_range(slice_spec)
        except ValueError as exc:
            raise BackfillError(str(exc)) from exc
        return UnitPlan(
            kind=UNIT_KIND_SLICE,
            units=units,
            slice_spec=slice_spec,
            key_range=key_range,
        )

    labels: list[str] = []
    for source in manifest.sources:
        for label in cs.get_adapter(source).units():
            if label not in labels:
                labels.append(label)
    if labels == [cs.SINGLE_UNIT]:
        return UnitPlan(kind=UNIT_KIND_SINGLE, units=(cs.SINGLE_UNIT,))
    dropped = 0
    if limit is not None and limit < len(labels):
        # An adapter whose units are not slices caps the NUMBER OF UNITS, not
        # the records inside one: the genome archive's --limit means "do the
        # first N archives of the frozen sorted list", which is only a
        # meaningful sample because the list is sorted and frozen.
        dropped = len(labels) - limit
        labels = labels[:limit]
    return UnitPlan(
        kind=UNIT_KIND_ADAPTER,
        units=tuple(labels),
        units_dropped_by_limit=dropped,
    )


def manifest_for_unit(
    manifest: cm.Manifest, plan: UnitPlan, unit: str
) -> cm.Manifest:
    """The manifest one unit is sharded from.

    For a SLICE unit this rewrites every mongo source's ``min_key``/``max_key``
    locator keys to that prefix's half-open range -- ``max_key`` OMITTED for
    ``"ff"``, which has no successor (see
    :func:`clearinghouse_shard.unit_key_range`). Rewriting the manifest rather
    than only restricting the adapter is what makes the range visible to
    everything downstream: ``shard_plan`` validates it, the adapter's
    ``_query_filter`` builds from it, and the run inventory's
    ``manifest_sha256`` is of the range actually read.

    For any other unit kind the manifest is returned unchanged -- the
    restriction travels as ``adapter_unit`` to
    :meth:`~clearinghouse_shard.SourceAdapter.restrict_to_unit` instead,
    because an adapter's own unit decomposition is not expressible as locator
    keys.
    """
    if plan.kind != UNIT_KIND_SLICE:
        return manifest
    min_key, max_key = cs.unit_key_range(unit)
    sources: list[cm.Source] = []
    for source in manifest.sources:
        if source.adapter != "mongo":
            sources.append(source)
            continue
        locator = {
            key: value
            for key, value in source.locator.items()
            if key not in ("min_key", "max_key")
        }
        locator["min_key"] = min_key
        if max_key is not None:
            locator["max_key"] = max_key
        sources.append(_replace_locator(source, locator))
    return cm.Manifest(sources=tuple(sources), backfill=manifest.backfill)


def _replace_locator(source: cm.Source, locator: Mapping[str, Any]) -> cm.Source:
    """A copy of ``source`` with a new locator (``Source`` is frozen)."""
    return cm.Source(
        name=source.name,
        adapter=source.adapter,
        entity_type=source.entity_type,
        kinds=source.kinds,
        locator=dict(locator),
        hash_spec=source.hash_spec,
        content=source.content,
        result=source.result,
        observed_at_column=source.observed_at_column,
    )


def source_identities(manifest: cm.Manifest) -> list[dict[str, Any]]:
    """The SOURCE IDENTITIES recorded in a run's registration.

    Each source's name, adapter, entity type, kinds and the locator keys that
    say WHERE it reads -- with the per-unit range keys (``min_key``,
    ``max_key``, ``start_after``) excluded, because those vary by unit and the
    identity must be constant for the whole run. This is what makes a resume
    able to notice that the manifest now points at a different collection.
    """
    out: list[dict[str, Any]] = []
    for source in manifest.sources:
        locator = {
            key: value
            for key, value in sorted(source.locator.items())
            if key not in ("min_key", "max_key", "start_after")
        }
        out.append(
            {
                "name": source.name,
                "adapter": source.adapter,
                "entity_type": source.entity_type,
                "kinds": list(source.kinds),
                "locator": locator,
            }
        )
    return out


# --------------------------------------------------------------------------
# The run context handed to adapters
# --------------------------------------------------------------------------


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(value: str) -> str:
    """A source or unit label made safe as one path segment."""
    cleaned = _SAFE_NAME_RE.sub("_", value).strip("._-")
    return cleaned or "unnamed"


@dataclass
class BackfillRunContext:
    """What an adapter is given about the run it is part of.

    Two things, both of which an adapter cannot get any other way:

    DURABLE STATE. :meth:`get_state` / :meth:`put_state` persist a small JSON
    object per (source, unit) in the RUN's state directory, atomically. The
    genome-archive adapter is why this exists: it submits a hash job, waits
    hours for it, and must not resubmit on a resume -- so the job id has to
    outlive the process that minted it. An adapter that kept that id in an
    instance attribute would lose it to the first pod restart and then submit
    a second job against the same archive, which is not merely wasteful but
    produces a second set of results for one input.

    A ``--limit`` BUDGET. :meth:`record_limit` supplies the cap that the
    adapter's OWN iteration loop then enforces, one record at a time (see
    :meth:`clearinghouse_shard.SourceAdapter._admit_record`). The driver
    deliberately does not slice the iterator instead, because a sliced
    iterator is indistinguishable from a source that genuinely ended -- and
    that distinction is exactly what decides ``partial`` versus ``done``.

    WHAT ``--limit`` MEANS IS ADAPTER-DEFINED, which is why it is routed
    through here rather than applied by the driver:

    - SLICE units (mongo): ``--limit`` records per source, per unit. Each
      slice reads up to ``--limit`` documents of its own, so ``--slice 00-0f
      --limit 1000`` is sixteen 1,000-document samples, not 1,000 documents
      from slice ``00``. The cursor is sorted on ``sort_key``, so every reader
      of one source sees the same first ``--limit`` documents.
    - SINGLE unit (the arc store): ``--limit`` records of ONE frozen ordered
      record selection. The cap is applied once TO THE SELECTION, which every
      source then reads -- so all of them see the same records and the loaded
      rows are a coherent sample rather than N rows of one table and none of
      the next. The freezing and ordering are the adapter's job; this supplies
      the number.
    - ADAPTER units (genome archives): no record cap at all -- ``--limit``
      capped the number of UNITS at plan time (see :func:`plan_units`), i.e.
      the first N of the frozen sorted archive list.
    """

    run_id: str
    unit: str
    unit_kind: str
    out_dir: Path
    state_dir: Path
    tenant: str
    dataset: str
    limit: int | None = None
    retry_failed_archives: bool = False

    # -- the --limit cap ----------------------------------------------------

    def record_limit(self, source: str) -> int | None:
        """How many records ONE READER of ``source`` may take; ``None`` = all.

        ``None`` for an ADAPTER-unit run, whose ``--limit`` capped the number
        of units at plan time instead -- capping the records inside an archive
        as well would silently halve the meaning of one flag.
        """
        if self.limit is None or self.unit_kind == UNIT_KIND_ADAPTER:
            return None
        return self.limit

    # -- durable adapter state ---------------------------------------------

    def state_file(self, source: str) -> Path:
        """Where one (source, unit) pair's durable JSON state lives."""
        return (
            self.state_dir
            / "adapter-state"
            / f"{_safe_name(self.unit)}__{_safe_name(source)}.json"
        )

    def get_state(self, source: str) -> dict[str, Any]:
        """Read this (source, unit)'s durable state; ``{}`` when there is none.

        A corrupt file reads as ``{}`` rather than raising: a crash mid-write
        can only truncate the temp file (the real one is replaced atomically),
        so an unreadable state file means something stranger happened, and
        failing a days-long backfill over it would be the wrong trade -- the
        adapter's own "no recorded id" path is already correct.
        """
        path = self.state_file(source)
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def put_state(self, source: str, value: Mapping[str, Any]) -> Path:
        """Persist this (source, unit)'s durable state, ATOMICALLY.

        Temp file plus :func:`os.replace`, fsynced, for the same reason the
        ingest ledger is fsynced: state that is still in the page cache when
        the pod dies is state that was never written, and the whole point of
        this call is to survive the thing that kills the process.
        """
        path = self.state_file(source)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(path, dict(value))
        return path

    def adapter_states(self) -> dict[str, dict[str, Any]]:
        """Every durable state file under this run, keyed by file stem.

        What ``status`` reads to report an adapter WATERMARK: a watermark is
        whatever the adapter chose to persist, so the driver reports the state
        rather than inventing a schema for it.
        """
        root = self.state_dir / "adapter-state"
        out: dict[str, dict[str, Any]] = {}
        if not root.is_dir():
            return out
        for path in sorted(root.glob("*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):  # pragma: no cover - defensive
                continue
            if isinstance(value, dict):
                out[path.stem] = value
        return out


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON to ``path`` atomically and fsynced."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Immutable run identity
# --------------------------------------------------------------------------

#: The registration fields ``--resume`` REFUSES a conflict on. Each one
#: changes what the run's own ledger MEANS, so carrying a run id across a
#: change to any of them would make the recorded proof a statement about data
#: that is not there.
_IDENTITY_FIELDS: tuple[str, ...] = (
    "out",
    "tenant",
    "dataset",
    "manifest_digest",
    "standardizer_version",
    "slice",
)


@dataclass(frozen=True)
class RunRegistration:
    """A backfill run's immutable identity, written once at run start."""

    run_id: str
    out: str
    tenant: str
    dataset: str
    manifest_digest: str
    standardizer_version: str
    sources: tuple[dict[str, Any], ...]
    slice: str | None
    limit: int | None
    preset: str | None
    manifest_path: str
    unit_kind: str
    created_at: str

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "out": self.out,
            "tenant": self.tenant,
            "dataset": self.dataset,
            "manifest_digest": self.manifest_digest,
            "standardizer_version": self.standardizer_version,
            "sources": [dict(source) for source in self.sources],
            "slice": self.slice,
            "limit": self.limit,
            "preset": self.preset,
            "manifest_path": self.manifest_path,
            "unit_kind": self.unit_kind,
            "created_at": self.created_at,
        }


def registration_path(run_id: str) -> Path:
    """Where one run's registration lives (its state directory)."""
    return ci.execution_state_dir(run_id) / RUN_REGISTRATION_FILE


def read_registration(run_id: str) -> dict[str, Any]:
    """One run's registration, or ``{}`` when it was never registered."""
    path = registration_path(run_id)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackfillError(
            f"backfill: run {run_id!r}'s registration at {path} is not "
            f"readable as JSON: {exc}. It is the only record of what this run "
            "was bound to -- its out path, its dataset, its manifest digest -- "
            "so resuming without it would mean guessing. Refusing."
        ) from exc
    return value if isinstance(value, dict) else {}


def register_run(
    *,
    run_id: str,
    out: Path,
    tenant: str,
    dataset: str,
    manifest: cm.Manifest,
    manifest_path: Path,
    slice_spec: str | None,
    limit: int | None,
    preset: str | None,
    unit_kind: str,
    resume: bool,
) -> dict[str, Any]:
    """Register a run's identity, or CHECK a resume against the recorded one.

    A fresh run writes the registration. A ``--resume`` reads it back and
    refuses any conflict in :data:`_IDENTITY_FIELDS` -- naming the field, the
    recorded value and the supplied one. ``--limit`` is NOT in that set: a
    deliberate later run without ``--limit`` is how a ``partial`` unit gets
    finished, so changing it is the intended workflow rather than a conflict.
    The supplied limit is recorded as the LATEST one, and the original stays
    in the ``limit_history`` list so the run's own record still says what
    produced each unit.

    Raises:
        BackfillError: A resume conflicts, or a resume names an unregistered
            run id.
    """
    from datetime import datetime, timezone

    digest = cs.manifest_identity(manifest, manifest_path)[1]
    fresh = RunRegistration(
        run_id=run_id,
        out=str(Path(out).expanduser().resolve()),
        tenant=tenant,
        dataset=dataset,
        manifest_digest=digest,
        standardizer_version=standardizers.STANDARDIZER_VERSION,
        sources=tuple(source_identities(manifest)),
        slice=slice_spec,
        limit=limit,
        preset=preset,
        manifest_path=str(Path(manifest_path).resolve()),
        unit_kind=unit_kind,
        created_at=datetime.now(tz=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
    ).to_json()

    existing = read_registration(run_id)
    if not existing:
        if resume:
            raise BackfillError(
                f"backfill: --resume names run id {run_id!r}, but there is no "
                f"registration for it at {registration_path(run_id)}. A "
                "resume reads the run's recorded identity (out path, dataset, "
                "manifest digest, standardizer version, sources, slice) and "
                "refuses what conflicts; with no registration there is "
                "nothing to check against, so it would not be a resume. "
                "Either run without --resume to start a new run, or check the "
                "run id."
            )
        fresh["limit_history"] = [limit]
        _atomic_write_json(registration_path(run_id), fresh)
        return fresh

    conflicts: list[str] = []
    for key in _IDENTITY_FIELDS:
        was = existing.get(key)
        now = fresh[key]
        if was != now:
            conflicts.append(f"{key}: registered {was!r}, this invocation {now!r}")
    if existing.get("sources") != fresh["sources"]:
        conflicts.append(
            "sources: the manifest's source identities differ from the ones "
            "this run was registered with"
        )
    if conflicts:
        raise BackfillError(
            f"backfill: --resume of run {run_id!r} CONFLICTS with the identity "
            "that run was registered with:\n  - "
            + "\n  - ".join(conflicts)
            + "\nA run id owns a ledger, and that ledger's counts are "
            "statements about one out path, one dataset and one manifest. "
            "Resuming across a change to any of them would append proof of "
            "one thing's rows to another thing's record -- which is exactly "
            "how a scratch run and a production run would come to share a "
            "ledger. Start a new run instead (omit --run-id), or correct the "
            "conflicting option."
        )
    merged = dict(existing)
    merged["limit"] = limit
    history = list(existing.get("limit_history") or [existing.get("limit")])
    if not history or history[-1] != limit:
        history.append(limit)
    merged["limit_history"] = history
    _atomic_write_json(registration_path(run_id), merged)
    return merged


def find_run_for_out(out: Path) -> str | None:
    """The run id that registered ``out``, for ``status --out DIR``.

    Two routes, in order: the state file at the root of ``out`` (written by
    the run itself, so this is the cheap and normal answer), then a scan of
    every registration under the state root for one whose recorded ``out``
    matches. The scan exists because ``--out`` is regenerable sharder output an
    operator may have deleted the state file from, while the registration --
    the actual record of the run -- lives elsewhere and survives that.
    """
    resolved = Path(out).expanduser()
    state = read_state(resolved)
    if state.get("run_id"):
        return str(state["run_id"])
    target = str(resolved.resolve()) if resolved.exists() else str(resolved)
    root = ci.resolve_state_root()
    if not root.is_dir():
        return None
    for path in sorted(root.glob(f"*/{RUN_REGISTRATION_FILE}")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):  # pragma: no cover - defensive
            continue
        if isinstance(value, dict) and value.get("out") == target:
            return str(value.get("run_id") or path.parent.name)
    return None


# --------------------------------------------------------------------------
# The per-run unit state file
# --------------------------------------------------------------------------


def state_path(out: Path) -> Path:
    """Where ``--out``'s unit-state file lives."""
    return Path(out).expanduser() / BACKFILL_STATE_FILE


def read_state(out: Path) -> dict[str, Any]:
    """Read ``<out>/backfill-state.json``, or ``{}`` when there is none."""
    path = state_path(out)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackfillError(
            f"backfill: {path} is not readable as JSON: {exc}. It is the "
            "record of which units finished; resuming without it would "
            "reprocess every unit, and reprocessing is only CHEAP (fill-only), "
            "never free. Refusing so the file can be inspected."
        ) from exc
    return value if isinstance(value, dict) else {}


def write_state(out: Path, state: Mapping[str, Any]) -> Path:
    """Rewrite ``<out>/backfill-state.json`` atomically."""
    path = state_path(out)
    _atomic_write_json(path, dict(state))
    return path


def unit_record(state: Mapping[str, Any], unit: str) -> dict[str, Any]:
    """One unit's recorded entry, or an empty ``pending`` one."""
    units = state.get("units") or {}
    entry = units.get(unit) if isinstance(units, Mapping) else None
    if isinstance(entry, Mapping):
        return dict(entry)
    return {"state": UNIT_PENDING}


def record_planned_units(
    out: Path, *, run_id: str, units: Sequence[str]
) -> dict[str, Any]:
    """Record what THIS invocation intends to process, before it starts.

    Kept in the state file rather than the registration because it varies per
    invocation -- ``--slice 00`` and a later ``--slice 01`` are the same run
    identity but different planned sets -- while the registration is immutable
    for the life of the run id. ``status`` subtracts the recorded units from
    this to report what is still PENDING, which is the one number an operator
    watching a 256-unit run actually wants.
    """
    state = read_state(out)
    state["run_id"] = run_id
    state.setdefault("state_version", 1)
    planned = list(state.get("planned_units") or [])
    for unit in units:
        if unit not in planned:
            planned.append(unit)
    state["planned_units"] = planned
    write_state(out, state)
    return state


def record_unit(
    out: Path,
    *,
    run_id: str,
    unit: str,
    entry: Mapping[str, Any],
) -> dict[str, Any]:
    """Record one unit's state, re-reading and rewriting the file atomically.

    Read-modify-write rather than keeping the state in memory and flushing at
    the end: the file is the only thing a resume has, and a run that held 256
    units' outcomes in a list until it finished would lose all of them to the
    kill it is designed to survive.
    """
    state = read_state(out)
    state["run_id"] = run_id
    state.setdefault("state_version", 1)
    units = dict(state.get("units") or {})
    units[unit] = dict(entry)
    state["units"] = units
    order = list(state.get("unit_order") or [])
    if unit not in order:
        order.append(unit)
    state["unit_order"] = order
    write_state(out, state)
    return state


# --------------------------------------------------------------------------
# Shard-directory hygiene: delete the UNPUBLISHED, never the published
# --------------------------------------------------------------------------


def published_files(unit_dir: Path) -> set[Path]:
    """Every batch file ``<unit_dir>/run.json`` names, resolved.

    A published batch is FINAL -- ``publish_unit`` appends it to the inventory
    only once the whole unit has been sharded, and the ledger may already
    record it as committed in the lake. These are the files nothing may
    delete.
    """
    run = cs.read_run_manifest(unit_dir)
    out: set[Path] = set()
    for batch in run.get("batches", []):
        name = batch.get("file")
        if name:
            out.add((Path(unit_dir) / str(name)).resolve())
    return out


def delete_unpublished(unit_dir: Path) -> list[str]:
    """Delete only the parquet files ``run.json`` does NOT name.

    This is the whole of "redo an unfinished unit from its start". A unit
    killed mid-shard leaves batch files that no inventory names: they may be
    truncated, they have no recorded sha256 to check them against, and nothing
    will ever ingest them -- so they are the partial shards a resume must
    clear. Anything the inventory DOES name is published, is possibly already
    in the lake, and is reused rather than regenerated (the ledger's exact
    reconcile makes re-ingesting it safe and free).

    Returns:
        The relative paths deleted, for the envelope.
    """
    unit_dir = Path(unit_dir)
    if not unit_dir.is_dir():
        return []
    keep = published_files(unit_dir)
    deleted: list[str] = []
    for path in sorted(unit_dir.rglob("*.parquet")):
        if path.resolve() in keep:
            continue
        path.unlink()
        deleted.append(str(path.relative_to(unit_dir)))
    return deleted


def unit_dir_for(out: Path, unit: str) -> Path:
    """Where one unit's shards and its own ``run.json`` live.

    One directory per unit, each with its OWN inventory, because a unit must
    be ingestible and verifiable on its own: ``ingest_shards`` binds an
    execution to one shard run, so a unit that shared an inventory with 255
    others could not be loaded without them.
    """
    return Path(out).expanduser() / "units" / _safe_name(unit)


def resolve_out(out: str | None, run_id: str) -> Path:
    """``--out``, or the default ``~/clearinghouse-shards/<run_id>/``.

    The default is derived from the run id so that ``--resume --run-id R``
    finds the shards of run ``R`` without the operator having to remember
    where they went.
    """
    if out:
        return Path(out).expanduser()
    return Path(DEFAULT_OUT_ROOT).expanduser() / run_id


# --------------------------------------------------------------------------
# Deciding a unit's state
# --------------------------------------------------------------------------


def unit_outcome(
    unit_dir: Path,
    *,
    verification: Mapping[str, Any],
    limit: int | None,
) -> tuple[str, list[str]]:
    """Decide a processed unit's state and the reasons for it.

    The ORDER is the point:

    1. A verification discrepancy is ``incomplete``, whatever else is true. A
       unit whose rows cannot be proven to have landed is not finished, and
       ``--limit`` does not excuse it.
    2. An adapter-reported RETRYABLE FAILURE is ``incomplete`` next, and this
       is the case neither of the other signals can express. An adapter that
       walked its whole selection but could not fetch three of the genome
       objects it needed is ``exhausted`` (its loop ran off the end) and
       every batch it wrote verifies -- so by rules 1 and 3 the unit is
       ``done``, and it is not: three records are missing and a retry would
       get them. See
       :meth:`clearinghouse_shard.SourceAdapter.incomplete_reasons`.
    3. Otherwise, EXHAUSTION decides. Every source outcome in the unit's own
       ``run.json`` must report ``exhausted`` true. That flag is set only when
       the adapter's loop ran off the end of its source, so it is the one
       signal that distinguishes a complete unit from a stopped one.
    4. A non-exhausted unit under ``--limit`` is ``partial`` -- expected, and
       reprocessed by a later run without ``--limit``.
    5. A non-exhausted unit with NO ``--limit`` is ``incomplete``: something
       stopped the read and nothing asked it to.

    Returns:
        ``(state, reasons)``.
    """
    discrepancies = [str(line) for line in verification.get("discrepancies") or []]
    if discrepancies:
        return UNIT_INCOMPLETE, discrepancies

    run = cs.read_run_manifest(unit_dir)
    outcomes = list(run.get("sources") or [])
    if not outcomes:
        return UNIT_INCOMPLETE, [
            f"{unit_dir}/run.json records no source outcomes at all, so "
            "nothing can say whether this unit's sources were drained. A unit "
            "is not done on the strength of an empty report."
        ]
    retryable = [
        f"source {outcome.get('source')!r} -> {outcome.get('table')}: {reason}"
        for outcome in outcomes
        for reason in (outcome.get("incomplete_reasons") or [])
    ]
    if retryable:
        return UNIT_INCOMPLETE, [
            "a source reported a RETRYABLE failure, so records are missing "
            "from a window that otherwise looks covered. --limit does not "
            "excuse it and exhaustion does not contradict it: the read "
            "reached the end of its selection, and some of what it selected "
            "could not be fetched. A later --resume retries exactly these:",
            *retryable,
        ]
    unexhausted = [
        f"source {outcome.get('source')!r} -> {outcome.get('table')}: "
        f"exhausted={outcome.get('exhausted')!r} after "
        f"{outcome.get('emitted')} emitted row(s)"
        for outcome in outcomes
        if outcome.get("exhausted") is not True
    ]
    if not unexhausted:
        return UNIT_DONE, []
    if limit is not None:
        return UNIT_PARTIAL, [
            f"--limit {limit} stopped this unit before its sources were "
            "drained; the rows that landed are correct and stay, and a later "
            "run without --limit processes this unit again (fill-only "
            "discards what is already there).",
            *unexhausted,
        ]
    return UNIT_INCOMPLETE, [
        "a source stopped short of exhaustion and no --limit was in force, so "
        "the read was cut off by something that was not asked for:",
        *unexhausted,
    ]


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------


def status_report(
    *,
    run_id: str,
    out: Path,
) -> dict[str, Any]:
    """Merge the state file, each unit's ``run.json`` and each unit's LEDGER.

    THREE RECORDS, THREE DIFFERENT QUESTIONS, and an operator needs all of
    them in one answer:

    - ``backfill-state.json`` -- which units the DRIVER finished, and in what
      state. It is the only thing that knows a unit is ``partial``.
    - each unit's ``run.json`` -- what the SHARDER produced and what it
      dropped (per-reason skip counts) and whether each source was drained.
    - each unit's LEDGER -- what actually reached the LAKE, per batch, with
      the row count it recorded.

    Rows are reported per table across all SIXTEEN tables, iterated from
    :func:`schema.table_configs` -- including
    :data:`schema.PARAMETER_SET_TABLE`, the registry, which is not produced by
    ``table_name()`` and is in neither ``ENTITY_TYPES`` nor the union views.
    Iterating ``ENTITY_TYPES x KINDS`` would report fifteen tables and silently
    omit the registry, whose rows are the only thing that makes a result row's
    parameters nameable.
    """
    out = Path(out).expanduser()
    state = read_state(out)
    registration = read_registration(run_id)
    known_tables = [str(config["name"]) for config in schema.table_configs()]

    units: list[dict[str, Any]] = []
    per_table_rows: dict[str, int] = dict.fromkeys(known_tables, 0)
    per_table_batches: dict[str, int] = dict.fromkeys(known_tables, 0)
    skips: dict[str, int] = {}
    watermarks: dict[str, Any] = {}
    unexpected_tables: list[str] = []
    totals = {
        UNIT_DONE: 0,
        UNIT_PARTIAL: 0,
        UNIT_INCOMPLETE: 0,
        UNIT_PENDING: 0,
    }

    order = list(state.get("unit_order") or sorted((state.get("units") or {})))
    for unit in order:
        entry = unit_record(state, unit)
        unit_state = str(entry.get("state") or UNIT_PENDING)
        totals[unit_state] = totals.get(unit_state, 0) + 1
        unit_dir = unit_dir_for(out, unit)
        run = cs.read_run_manifest(unit_dir)
        row: dict[str, Any] = {
            "unit": unit,
            "state": unit_state,
            "reasons": list(entry.get("reasons") or []),
            "execution_id": entry.get("execution_id"),
            "shard_run_id": run.get("run_id"),
            "batches": len(run.get("batches") or []),
            "rows_sharded": sum(
                int(batch.get("rows") or 0) for batch in run.get("batches") or []
            ),
            "recorded_at": entry.get("recorded_at"),
        }
        for skip in run.get("skipped") or []:
            reason = str(skip.get("reason"))
            skips[reason] = skips.get(reason, 0) + int(skip.get("count") or 0)
        # The adapter WATERMARK: whatever the adapter chose to persist through
        # the run context. Reported as-is -- the driver does not define its
        # shape, which is the point of a durable-state callback.
        for outcome in run.get("sources") or []:
            key = f"{unit}::{outcome.get('source')}"
            watermarks.setdefault(key, {}).update(
                {
                    "emitted": outcome.get("emitted"),
                    "accepted": outcome.get("accepted"),
                    "exhausted": outcome.get("exhausted"),
                }
            )
        execution_id = entry.get("execution_id")
        rows_ingested = 0
        if execution_id:
            ledger = ci.ExecutionLedger(ci.execution_state_dir(str(execution_id)))
            for record in ledger.records().values():
                if str(record.get("state")) != ci.LEDGER_COMMITTED:
                    continue
                table = str(record.get("table") or "")
                count = int(record.get("rows_ingested") or 0)
                rows_ingested += count
                if table in per_table_rows:
                    per_table_rows[table] += count
                    per_table_batches[table] += 1
                elif table and table not in unexpected_tables:
                    unexpected_tables.append(table)
        row["rows_ingested"] = rows_ingested
        units.append(row)

    for stem, value in _adapter_states(run_id).items():
        watermarks.setdefault(stem, {}).update({"adapter_state": value})

    planned = list(state.get("planned_units") or [])
    pending = [unit for unit in planned if unit not in order]
    totals[UNIT_PENDING] = totals.get(UNIT_PENDING, 0) + len(pending)

    return {
        "run_id": run_id,
        "out": str(out),
        "registration": registration or None,
        "units": units,
        "pending_units": pending,
        "totals": totals,
        "tables": [
            {
                "name": name,
                "rows_ingested": per_table_rows[name],
                "batches": per_table_batches[name],
                "is_registry": name == schema.PARAMETER_SET_TABLE,
            }
            for name in known_tables
        ],
        "unexpected_tables": unexpected_tables,
        "skipped": [
            {"reason": reason, "count": count}
            for reason, count in sorted(skips.items())
        ],
        "watermarks": watermarks,
    }


def _adapter_states(run_id: str) -> dict[str, dict[str, Any]]:
    """Every durable adapter-state file a run persisted, keyed by file stem."""
    root = ci.execution_state_dir(run_id) / "adapter-state"
    out: dict[str, dict[str, Any]] = {}
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):  # pragma: no cover - defensive
            continue
        if isinstance(value, dict):
            out[path.stem] = value
    return out


# --------------------------------------------------------------------------
# The driver
# --------------------------------------------------------------------------


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def shard_units(
    *,
    manifest: cm.Manifest,
    manifest_path: Path,
    plan: UnitPlan,
    out: Path,
    run_id: str,
    limit: int | None,
    resume: bool,
    tenant: str,
    dataset: str,
    retry_failed_archives: bool = False,
    shard_fn: Any = None,
    target_bytes: int | None = None,
) -> list[dict[str, Any]]:
    """Shard every planned unit into ``<out>/units/<unit>/`` -- the SHARD half.

    This is what ``backfill shard`` runs, and what ``backfill run`` calls per
    unit. ``--resume`` NEVER deletes a published batch: a unit whose
    ``run.json`` already exists has finished sharding and is left exactly as
    it is, and a unit with no inventory has only unpublished (possibly
    truncated) files, which are cleared before it is redone.
    """
    shard = shard_fn or cs.shard_manifest
    reports: list[dict[str, Any]] = []
    state_dir = ci.execution_state_dir(run_id)

    for unit in plan.units:
        unit_dir = unit_dir_for(out, unit)
        existing = cs.read_run_manifest(unit_dir)
        if existing and resume:
            reports.append(
                {
                    "unit": unit,
                    "action": "reused",
                    "shard_run_id": existing.get("run_id"),
                    "batches": len(existing.get("batches") or []),
                    "deleted": [],
                }
            )
            continue
        deleted = delete_unpublished(unit_dir) if resume else []
        if existing and not resume:
            raise BackfillError(
                f"backfill shard: {unit_dir} already holds a published run "
                f"inventory ({len(existing.get('batches') or [])} batch(es)). "
                "A published batch is final and is never rewritten, so this "
                "refuses rather than appending a second unit's batches into "
                "it. Pass --resume to reuse what is published and regenerate "
                "only what is not, or shard into a fresh --out."
            )
        unit_dir.mkdir(parents=True, exist_ok=True)
        context = BackfillRunContext(
            run_id=run_id,
            unit=unit,
            unit_kind=plan.kind,
            out_dir=unit_dir,
            state_dir=state_dir,
            tenant=tenant,
            dataset=dataset,
            limit=limit,
            retry_failed_archives=retry_failed_archives,
        )
        unit_manifest = manifest_for_unit(manifest, plan, unit)
        kwargs: dict[str, Any] = {
            "run_id": run_id,
            "slice_label": unit if plan.kind == UNIT_KIND_SLICE else None,
            "manifest_path": manifest_path,
            "run_context": context,
            "adapter_unit": unit if plan.kind == UNIT_KIND_ADAPTER else None,
        }
        if target_bytes is not None:
            kwargs["target_bytes"] = target_bytes
        shard(unit_manifest, unit_dir, **kwargs)
        published = cs.read_run_manifest(unit_dir)
        reports.append(
            {
                "unit": unit,
                "action": "sharded",
                "shard_run_id": published.get("run_id"),
                "batches": len(published.get("batches") or []),
                "deleted": deleted,
            }
        )
    return reports


def run_backfill(
    cap: Any,
    *,
    manifest: cm.Manifest,
    manifest_path: Path,
    plan: UnitPlan,
    out: Path,
    run_id: str,
    limit: int | None,
    resume: bool,
    tenant: str,
    dataset: str,
    retry_failed_archives: bool = False,
    shard_fn: Any = None,
    target_bytes: int | None = None,
) -> dict[str, Any]:
    """Shard, ingest, verify and record EACH UNIT IN TURN. The one-command path.

    The order inside a unit is the contract, and it is ``shard -> ingest ->
    verify -> record``. The record is written LAST and only on a verified
    unit, so the state file never claims a unit that the lake cannot prove. A
    kill anywhere before the record leaves the unit unrecorded and a
    ``--resume`` redoes it EXACTLY ONCE -- re-ingesting a published batch is
    free (fill-only finds every key present and commits without a write), so
    the redo is cheap rather than merely safe.

    A unit's INGEST EXECUTION ID is recorded BEFORE the ingest, not after. The
    ledger lives under that id, so an id lost to a kill would strand the only
    record of what the interrupted ingest wrote.

    An ``incomplete`` unit does NOT stop the run. The remaining units are
    independent and there is no reason 255 of them should wait on one; the
    command exits non-zero at the end instead, with every incomplete unit and
    its reasons in the envelope.
    """
    shard = shard_fn or cs.shard_manifest
    state_dir = ci.execution_state_dir(run_id)
    unit_reports: list[dict[str, Any]] = []

    for unit in plan.units:
        entry = unit_record(read_state(out), unit)
        if resume and entry.get("state") == UNIT_DONE:
            unit_reports.append(
                {
                    "unit": unit,
                    "state": UNIT_DONE,
                    "action": "skipped-done",
                    "execution_id": entry.get("execution_id"),
                    "reasons": [],
                }
            )
            continue

        unit_dir = unit_dir_for(out, unit)
        # The execution id is STICKY across resumes: the ledger lives under
        # it, so reusing it is what makes the reconcile pass able to decide a
        # batch the kill left mid-append.
        execution_id = str(entry.get("execution_id") or ci.new_execution_id())
        record_unit(
            out,
            run_id=run_id,
            unit=unit,
            entry={
                **entry,
                "state": entry.get("state") or UNIT_PENDING,
                "execution_id": execution_id,
                "started_at": _iso_now(),
            },
        )

        published = cs.read_run_manifest(unit_dir)
        reused = bool(published) and resume
        deleted: list[str] = []
        if not reused:
            # RECONCILE BEFORE DELETE. Anything this unit already published may
            # be in the lake with a `started` ledger line; the reconcile pass
            # inside ingest_shards decides those by an exact count BEFORE
            # anything is removed, and delete_unpublished then clears only the
            # files no inventory names.
            if published and resume:  # pragma: no cover - reused covers this
                cap.ingest_shards(
                    str(unit_dir),
                    run_id=execution_id,
                    reconcile=True,
                    tenant=tenant,
                    dataset=dataset,
                )
            deleted = delete_unpublished(unit_dir) if resume else []
            unit_dir.mkdir(parents=True, exist_ok=True)
            context = BackfillRunContext(
                run_id=run_id,
                unit=unit,
                unit_kind=plan.kind,
                out_dir=unit_dir,
                state_dir=state_dir,
                tenant=tenant,
                dataset=dataset,
                limit=limit,
                retry_failed_archives=retry_failed_archives,
            )
            kwargs: dict[str, Any] = {
                "run_id": run_id,
                "slice_label": unit if plan.kind == UNIT_KIND_SLICE else None,
                "manifest_path": manifest_path,
                "run_context": context,
                "adapter_unit": unit if plan.kind == UNIT_KIND_ADAPTER else None,
            }
            if target_bytes is not None:
                kwargs["target_bytes"] = target_bytes
            shard(manifest_for_unit(manifest, plan, unit), unit_dir, **kwargs)

        ingest = cap.ingest_shards(
            str(unit_dir),
            run_id=execution_id,
            reconcile=resume,
            tenant=tenant,
            dataset=dataset,
        )
        verification = cap.verify_run(str(unit_dir), run_id=execution_id)
        state_value, reasons = unit_outcome(
            unit_dir, verification=verification, limit=limit
        )
        recorded = {
            "state": state_value,
            "reasons": reasons,
            "execution_id": execution_id,
            "shard_run_id": cs.read_run_manifest(unit_dir).get("run_id"),
            "source_run_id": ingest.get("source_run_id"),
            "namespace": ingest.get("namespace"),
            "recorded_at": _iso_now(),
            "reused_shards": reused,
            "deleted_unpublished": deleted,
        }
        record_unit(out, run_id=run_id, unit=unit, entry=recorded)
        if state_value == UNIT_DONE:
            # ONLY NOW may a source-level watermark become real. An adapter
            # that read a complete, uncapped window wrote a CANDIDATE
            # checkpoint, deliberately unreadable, because whether the rows
            # reached the lake is not something an adapter can know. This is
            # the point at which it is known: ingested and verified, with the
            # unit recorded `done`. A capped, incomplete or killed unit never
            # gets here and its candidate is simply never promoted.
            cs.promote_arc_checkpoints()
        unit_reports.append(
            {
                "unit": unit,
                "state": state_value,
                "action": "reused-shards" if reused else "sharded",
                "execution_id": execution_id,
                "reasons": reasons,
                "batches": len(ingest.get("tables") or []),
                "rows_ingested": sum(
                    int(table.get("rows_ingested") or 0)
                    for table in ingest.get("tables") or []
                ),
                "deleted_unpublished": deleted,
                "verified": not verification.get("discrepancies"),
            }
        )
    return {"units": unit_reports}


def exit_states(unit_reports: Sequence[Mapping[str, Any]]) -> list[str]:
    """The unit labels whose state makes the command exit NON-ZERO.

    Only ``incomplete``. ``partial`` is an ASKED-FOR outcome -- the operator
    passed ``--limit`` -- so exiting non-zero on it would make every sampling
    run look like a failure and train an operator to ignore the exit code.
    ``incomplete`` is a retryable failure that nobody asked for, and it exits
    non-zero even when every batch that WAS emitted verified.
    """
    return [
        str(report.get("unit"))
        for report in unit_reports
        if report.get("state") == UNIT_INCOMPLETE
    ]


__all__ = [
    "BACKFILL_STATE_FILE",
    "DEFAULT_OUT_ROOT",
    "RUN_REGISTRATION_FILE",
    "UNIT_DONE",
    "UNIT_INCOMPLETE",
    "UNIT_KIND_ADAPTER",
    "UNIT_KIND_SINGLE",
    "UNIT_KIND_SLICE",
    "UNIT_PARTIAL",
    "UNIT_PENDING",
    "BackfillError",
    "BackfillRunContext",
    "Preset",
    "RunRegistration",
    "UnitPlan",
    "available_presets",
    "delete_unpublished",
    "describe_preset",
    "exit_states",
    "find_run_for_out",
    "manifest_for_unit",
    "plan_units",
    "preset_root",
    "published_files",
    "read_registration",
    "read_state",
    "record_planned_units",
    "record_unit",
    "register_run",
    "registration_path",
    "resolve_manifest_path",
    "resolve_out",
    "run_backfill",
    "shard_units",
    "source_identities",
    "state_path",
    "status_report",
    "unit_dir_for",
    "unit_outcome",
    "unit_record",
    "write_state",
]
