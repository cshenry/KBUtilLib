"""Render layer for the Models and Analyses app (levels 2 and 3).

This module wires the app's two deepest levels to the Escher renderers that
already live in :mod:`kbutillib.domains.notebook.escher_utils`. It BUILDS NO
RENDERING of its own: every pixel is produced by
:meth:`EscherUtils.create_fitness_dashboard`,
:meth:`EscherUtils.create_map_html2` and :meth:`EscherUtils.list_available_maps`.
This layer only

  * resolves a record's artifacts to something those methods can consume,
  * chooses the map deterministically,
  * caches the generated HTML on disk keyed by ``(record_id, input mtimes)``,
  * runs generation off the request thread behind a poll token, and
  * surfaces trust-tier / arc provenance into the dashboard header text.

Like :mod:`~kbutillib.models_and_analyses.service`, NOTHING here imports a web
framework — the FastAPI wiring in :mod:`~kbutillib.models_and_analyses.app`
imports these functions. ``EscherUtils`` itself is imported LAZILY (inside the
functions that call it) so importing this module never drags in the heavy
scientific stack; tests mock the ``EscherUtils`` calls.

BLOB DISCIPLINE (binding): the render endpoints are the ONLY levels allowed to
call :meth:`store.read_detail`. The lower endpoints in ``service.py`` never do.

MCC SIGNIFICANCE THRESHOLD (binding S20): measured-fitness significance is
``measured < -1.0``, defined inline at
``kbutillib/domains/notebook/fitness_dashboard.py:169`` inside ``_concordance``.
This module does NOT redefine it — the dashboard applies it internally when it
builds the concordance tab, and no second copy of the number appears here. See
:data:`_MCC_THRESHOLD_CITATION` for the greppable citation.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..koros_arc_store.records import AnalysisRecord
from .prefixes import MODEL_BUILD_KINDS
from .service import resolve_app_state_dir

__all__ = [
    "ArtifactUnavailable",
    "GenerationFailed",
    "RecordUnknown",
    "MapUnavailable",
    "ModelNotFound",
    "ResolvedArtifact",
    "DEFAULT_INLINE_ESCHER",
    "MAP_PREFERENCE",
    "POLL_TOKEN_RE",
    "resolve_artifact",
    "find_record",
    "locate_analysis",
    "find_paired_model_build",
    "select_default_map",
    "list_maps_for_record",
    "cache_key",
    "cached_html_path",
    "invalidate_record_cache",
    "dashboard_cache_lookup",
    "escher_cache_lookup",
    "read_or_generate_dashboard",
    "read_or_generate_escher",
    "new_poll_token",
    "write_poll_token",
    "read_poll_token",
    "gc_poll_tokens",
]


# ── configuration ────────────────────────────────────────────────────────────────

# inline_escher is a TOGGLE, not a constant (binding). The default is False for the
# IN-APP path: the app serves the Escher bundle once over HTTP, so inlining a
# multi-megabyte copy into every generated page (which is create_fitness_dashboard's
# own default of True) would bloat the on-disk cache by that bundle PER MODEL. But
# the inlined path stays reachable via this toggle, because an air-gapped pod with
# inline_escher=False renders a BLANK map — indistinguishable from the bloat problem
# it avoids. Callers pass this value straight through to create_fitness_dashboard.
DEFAULT_INLINE_ESCHER: bool = False

# Deterministic default-map order (binding): modelseed_core, then modelseed_global,
# then the first entry list_available_maps returns. A non-deterministic default is
# untestable.
MAP_PREFERENCE: Tuple[str, ...] = ("modelseed_core", "modelseed_global")

# Poll token schema (binding S18): pt-<uuid4>.
POLL_TOKEN_RE = re.compile(
    r"^pt-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

# Poll tokens older than this are garbage-collected at startup (binding S18).
POLL_TOKEN_TTL_SECONDS = 24 * 60 * 60


# MCC SIGNIFICANCE THRESHOLD — CITED, NEVER REDEFINED (binding S20).
# The measured-fitness significance rule (``measured < -1.0``) lives ONLY inside
# ``_concordance`` at kbutillib/domains/notebook/fitness_dashboard.py:169. The
# dashboard applies it when it builds the concordance tab; this render layer
# delegates to create_fitness_dashboard and therefore never needs — and must not
# carry — a second copy of the number. This comment is the greppable citation a
# reviewer confirms; there is no ``-1.0`` used as a threshold anywhere in this file.
_MCC_THRESHOLD_CITATION = (
    "kbutillib/domains/notebook/fitness_dashboard.py:169 (measured < -1.0)"
)


# ── typed errors ─────────────────────────────────────────────────────────────────


class RecordUnknown(Exception):
    """No run record exists for a requested ``record_id``."""


class ModelNotFound(Exception):
    """No paired ``kbdl.model_build`` exists in the same arc for a fitness record."""


class MapUnavailable(Exception):
    """``list_available_maps`` returned an empty list — nothing to render."""


class ArtifactUnavailable(Exception):
    """An input artifact could not be resolved or read on this machine.

    Raised for the DEGRADE path (binding): an object-store artifact whose store
    is unreachable, a file-path artifact that is missing, or a file that is
    present but unreadable (a permission problem, a half-synced Dropbox file).
    The message NAMES the artifact so a reader can tell an unavailable input
    apart from a model with no results.
    """


class GenerationFailed(Exception):
    """The renderer raised while generating; the message names the failing artifact."""


# ── artifact resolution ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ResolvedArtifact:
    """A record artifact resolved to something a renderer can consume.

    ``kind`` is ``"path"`` for a local file (``value`` is the absolute path and
    ``mtime`` is its modification time) — create_fitness_dashboard accepts a path
    directly. ``kind`` is ``"obj"`` only when an object-store resolver was
    supplied and succeeded, in which case ``value`` is the resolved local path it
    produced. There is no in-memory ``dict`` variant: everything is resolved to a
    path so the cache key can key on its mtime.
    """

    key: str
    kind: str
    value: str
    mtime: float


# A resolver takes an ``obj://<id>`` object id (without the scheme) and returns a
# local filesystem path to the fetched content, or None when the object store is
# not reachable on this machine. It is INJECTED (default None) so object-store
# availability is never a hard requirement — on a pod with no object store the
# render layer degrades to file-path artifacts and, failing that, ArtifactUnavailable.
ObjectResolver = Callable[[str], Optional[str]]


def resolve_artifact(
    key: str,
    uri: str,
    *,
    object_resolver: Optional[ObjectResolver] = None,
) -> ResolvedArtifact:
    """Resolve one artifact URI to a readable local path, or raise.

    Artifacts may be KBDL object-store ids (``obj://<id>``), ``file://`` URIs, or
    plain absolute paths. Resolution DEGRADES (binding):

      * ``file://`` / absolute path → the path, probed for readability.
      * ``obj://<id>`` → ``object_resolver(id)`` if one was supplied AND it
        returns a path; otherwise :class:`ArtifactUnavailable` naming the
        artifact. Object-store availability is never a hard requirement.

    Readability is PROBED, not assumed (binding): a present-but-unreadable file
    (permission, half-synced Dropbox) raises :class:`ArtifactUnavailable` naming
    it, because letting the generator run on it produces an EMPTY page that looks
    identical to a model with no results.
    """
    if uri.startswith("obj://"):
        obj_id = uri[len("obj://") :]
        local: Optional[str] = None
        if object_resolver is not None:
            try:
                local = object_resolver(obj_id)
            except Exception as exc:  # noqa: BLE001 - degrade, never surface a trace
                raise ArtifactUnavailable(
                    f"artifact {key!r} ({uri}) could not be fetched from the "
                    f"object store on this machine: {exc}"
                ) from exc
        if not local:
            raise ArtifactUnavailable(
                f"artifact {key!r} ({uri}) is an object-store id and the object "
                f"store is not reachable on this machine"
            )
        path = local
    elif uri.startswith("file://"):
        path = uri[len("file://") :]
    elif uri.startswith("/"):
        path = uri
    else:
        raise ArtifactUnavailable(
            f"artifact {key!r} has an unrecognised URI {uri!r}"
        )

    p = Path(path)
    if not p.exists():
        raise ArtifactUnavailable(
            f"artifact {key!r} ({uri}) is not present on this machine at {path}"
        )
    # Probe readability explicitly — an unreadable file must ERROR, not silently
    # yield an empty dashboard.
    try:
        with open(p, "rb") as fh:
            fh.read(1)
    except OSError as exc:
        raise ArtifactUnavailable(
            f"artifact {key!r} ({uri}) is present but unreadable at {path}: {exc}"
        ) from exc
    return ResolvedArtifact(key=key, kind="path", value=str(p), mtime=p.stat().st_mtime)


# ── record and model resolution ──────────────────────────────────────────────────


def find_record(store: Any, record_id: str) -> Tuple[AnalysisRecord, Optional[str], Optional[str]]:
    """Locate a record by ``record_id``, returning it with its ``(project, arc)``.

    The stores expose no ``get_record(record_id)``; :meth:`list_analyses` is
    keyed on ``(project, arc)``. So this scans every project's arcs plus the
    unattributed bucket and returns the first record whose id matches, together
    with the project and arc it was found under (both ``None`` for unattributed).
    Raises :class:`RecordUnknown` when nothing matches.

    This walks the store, not the filesystem — the store stays the only door.
    """
    # Unattributed first (cheap, single call).
    for rec in store.list_analyses(None, None):
        if rec.record_id == record_id:
            return rec, None, None
    for project in store.list_projects():
        for arc in store.list_arcs(project.name):
            for rec in store.list_analyses(project.name, arc.slug):
                if rec.record_id == record_id:
                    return rec, project.name, arc.slug
    raise RecordUnknown(f"no run record with id {record_id!r}")


def locate_analysis(
    store: Any, analysis_id: str
) -> Optional[Tuple[Optional[str], Optional[str]]]:
    """Return the ``(project, arc)`` bucket holding ``analysis_id``, or None.

    The stores key :meth:`list_analyses` on ``(project, arc)`` — there is no
    ``analysis_id``-only query — so to expand or count the runs of one analysis the
    app must first learn which bucket it lives in. Every run of one analysis_id
    lives in the SAME bucket (analysis_id is derived from kind+subject+params, all
    fixed within an arc), so the first bucket containing any matching run is THE
    bucket. Returns None when no run of that analysis_id exists anywhere (the
    endpoint maps that to 404, never an empty 200).

    Walks the store, not the filesystem — the store stays the only door — and
    scans the same order as :func:`find_record` (unattributed first, then every
    project's arcs).
    """
    for rec in store.list_analyses(None, None):
        if rec.analysis_id == analysis_id:
            return None, None
    for project in store.list_projects():
        for arc in store.list_arcs(project.name):
            for rec in store.list_analyses(project.name, arc.slug):
                if rec.analysis_id == analysis_id:
                    return project.name, arc.slug
    return None


def _model_uri_of(record: AnalysisRecord) -> Optional[str]:
    """Return a record's ``artifacts.model_id`` value, or None."""
    uri = (record.artifacts or {}).get("model_id")
    return uri if isinstance(uri, str) and uri else None


def find_paired_model_build(
    store: Any,
    fitness_record: AnalysisRecord,
    project: Optional[str],
    arc: Optional[str],
) -> AnalysisRecord:
    """Return the model build paired with a fitness-analysis record.

    Binding: the paired model is the MOST RECENT ``kbdl.model_build`` sharing the
    same ``artifacts.model_id`` IN THE SAME ARC. If there is none, raise
    :class:`ModelNotFound` (the endpoint maps it to 404 model-not-found).
    ``create_fitness_dashboard`` must never be called with a guessed counterpart.
    """
    target_uri = _model_uri_of(fitness_record)
    candidates = [
        rec
        for rec in store.list_analyses(project, arc)
        if rec.kind in MODEL_BUILD_KINDS and _model_uri_of(rec) == target_uri
    ]
    if not candidates:
        raise ModelNotFound(
            f"no paired {sorted(MODEL_BUILD_KINDS)} record with model_id "
            f"{target_uri!r} in arc {arc!r} of project {project!r}"
        )
    # list_analyses is newest-first (S25 tie-break), so the first is the most recent.
    return candidates[0]


# ── map selection ────────────────────────────────────────────────────────────────


def list_maps_for_record(escher: Any, model: Any) -> List[Dict[str, Any]]:
    """Surface :meth:`EscherUtils.list_available_maps` for a model.

    This delegates entirely; the render layer enumerates no maps itself.
    """
    return escher.list_available_maps(model)


def select_default_map(available: List[Dict[str, Any]]) -> str:
    """Choose the default map deterministically (binding).

    Order: ``modelseed_core``, then ``modelseed_global``, then the first entry
    from :meth:`list_available_maps`. An empty list raises :class:`MapUnavailable`
    (the endpoint maps it to 404 no-map) — a non-deterministic default is
    untestable.
    """
    if not available:
        raise MapUnavailable("list_available_maps returned no maps for this model")
    names = [str(entry.get("name")) for entry in available]
    for preferred in MAP_PREFERENCE:
        if preferred in names:
            return preferred
    return names[0]


# ── on-disk HTML cache ───────────────────────────────────────────────────────────


def _cache_dir() -> Path:
    """Return the render cache directory under the app state dir, creating it."""
    d = resolve_app_state_dir() / "render-cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_key(record_id: str, artifacts: List[ResolvedArtifact], *, suffix: str = "") -> str:
    """Build the cache key for a generated page.

    Keyed on ``record_id`` plus the mtimes of the resolved input artifacts, so an
    unchanged model is not regenerated and a moved mtime invalidates the entry
    (binding). ``suffix`` distinguishes variants that share inputs (e.g. the
    selected map for the escher endpoint) so two views of one record do not
    collide. Deterministic: artifacts are sorted by key before hashing.
    """
    import hashlib

    parts = [record_id]
    for art in sorted(artifacts, key=lambda a: a.key):
        # mtime carried to nanosecond-ish resolution; repr keeps float precision.
        parts.append(f"{art.key}={art.value}@{art.mtime!r}")
    if suffix:
        parts.append(f"suffix={suffix}")
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:32]
    return digest


def cached_html_path(key: str) -> Path:
    """Return the on-disk path a cache key maps to (may not exist yet)."""
    return _cache_dir() / f"{key}.html"


# ── cache index (record_id → cache keys) for delete-time invalidation ────────────
#
# A cache key is a hash of ``record_id`` plus input mtimes plus a variant suffix,
# so a record_id maps to MANY keys over its life (a moved mtime, two maps). At
# delete time all the app holds is the record_id, and it CANNOT recompute those
# hashes (it no longer has the artifacts) nor walk the cache directory to find them
# (the app package is forbidden the directory-traversal verbs — see
# ``tests/models_and_analyses/test_layering.py``). So, exactly as the poll tokens
# do with their name index, every generated page records its key against its
# record_id here, and invalidation reads this index to know which files to unlink.


def _cache_index_path() -> Path:
    """Return the cache index file mapping ``record_id`` → the keys it produced."""
    return _cache_dir() / "_index.json"


def _read_cache_index() -> Dict[str, List[str]]:
    """Return the record_id→keys map from the index, or {} when absent/corrupt."""
    path = _cache_index_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        rid: [k for k in keys if isinstance(k, str)]
        for rid, keys in data.items()
        if isinstance(rid, str) and isinstance(keys, list)
    }


def _write_cache_index(index: Dict[str, List[str]]) -> None:
    """Persist the record_id→keys map atomically (sorted for determinism)."""
    path = _cache_index_path()
    tmp = path.with_suffix(".json.tmp")
    ordered = {rid: sorted(set(index[rid])) for rid in sorted(index)}
    tmp.write_text(json.dumps(ordered), encoding="utf-8")
    tmp.replace(path)


def _record_cache_key(record_id: str, key: str) -> None:
    """Note that ``key`` is a cache file belonging to ``record_id`` (idempotent)."""
    index = _read_cache_index()
    keys = index.setdefault(record_id, [])
    if key not in keys:
        keys.append(key)
        _write_cache_index(index)


def invalidate_record_cache(record_id: str) -> int:
    """Unlink every generated-HTML cache file for ``record_id``; return the count.

    Part of the delete path (binding): deleting a run leaves a cache entry no row
    points at, and the next request for a re-created record_id could serve it. This
    reads the cache index (populated as pages are generated), unlinks each named
    file, and drops the record_id from the index. Missing files are treated as
    already-gone (counted, then pruned) rather than an error. Nothing here raises
    if the cache or index is absent — a delete must not fail because a run was
    never rendered.
    """
    index = _read_cache_index()
    keys = index.pop(record_id, None)
    if keys is None:
        return 0
    d = _cache_dir()
    removed = 0
    for key in keys:
        path = d / f"{key}.html"
        try:
            if path.exists():
                path.unlink()
            removed += 1
        except OSError:
            # Leave the entry out of the index regardless — a file we cannot
            # unlink is not worth keeping the record_id alive for.
            continue
    _write_cache_index(index)
    return removed


def dashboard_cache_lookup(
    *,
    record_id: str,
    model_artifact: ResolvedArtifact,
    fitness_artifact: ResolvedArtifact,
    map_name: str,
    extra_artifacts: Optional[List[ResolvedArtifact]] = None,
) -> Optional[Path]:
    """Return the cached dashboard path if it exists (non-empty), else None.

    A pure cache probe — it NEVER generates. The endpoint uses it to serve a 200
    immediately on a hit and hand generation to a background thread on a miss, so
    the request is never held through the seconds-long generation (binding: the
    iframe must not time out against the 45s ready probe).
    """
    inputs = [model_artifact, fitness_artifact] + list(extra_artifacts or [])
    key = cache_key(record_id, inputs, suffix=f"dashboard:{map_name}")
    out = cached_html_path(key)
    return out if out.exists() and out.stat().st_size > 0 else None


def escher_cache_lookup(
    *,
    record_id: str,
    model_artifact: ResolvedArtifact,
    map_name: str,
    flux_artifact: Optional[ResolvedArtifact] = None,
) -> Optional[Path]:
    """Return the cached single-map path if it exists (non-empty), else None."""
    inputs = [model_artifact]
    if flux_artifact is not None:
        inputs.append(flux_artifact)
    key = cache_key(record_id, inputs, suffix=f"escher:{map_name}")
    out = cached_html_path(key)
    return out if out.exists() and out.stat().st_size > 0 else None


def read_or_generate_dashboard(
    escher: Any,
    *,
    record_id: str,
    model_artifact: ResolvedArtifact,
    fitness_artifact: ResolvedArtifact,
    map_name: str,
    title: str,
    subtitle: str,
    inline_escher: bool = DEFAULT_INLINE_ESCHER,
    extra_artifacts: Optional[List[ResolvedArtifact]] = None,
) -> Path:
    """Return the cached dashboard HTML path, generating it once on a cache miss.

    The cache key covers ``record_id`` and the mtimes of every input artifact, so
    an unchanged model is served from disk and any moved mtime regenerates. On a
    miss this delegates to :meth:`EscherUtils.create_fitness_dashboard`, passing
    ``inline_escher`` straight through (defaulting to
    :data:`DEFAULT_INLINE_ESCHER`, i.e. False for the served in-app path). It
    builds no HTML itself.

    Raises :class:`GenerationFailed` naming the model/fitness artifacts if the
    renderer raises, and never returns an empty file: a zero-byte result from the
    renderer is treated as a generation failure (an empty page is
    indistinguishable from a model with no results).
    """
    inputs = [model_artifact, fitness_artifact] + list(extra_artifacts or [])
    key = cache_key(record_id, inputs, suffix=f"dashboard:{map_name}")
    out = cached_html_path(key)
    if out.exists() and out.stat().st_size > 0:
        # Ensure the index knows this key even on a hit — a delete must find it.
        _record_cache_key(record_id, key)
        return out

    try:
        escher.create_fitness_dashboard(
            model_artifact.value,
            fitness_artifact.value,
            map_name,
            str(out),
            title=title,
            subtitle=subtitle,
            inline_escher=inline_escher,
        )
    except Exception as exc:  # noqa: BLE001 - name the artifacts, don't leak a trace
        # Clean up a partial file so a failed run never poisons the cache.
        if out.exists():
            out.unlink()
        raise GenerationFailed(
            f"dashboard generation failed for record {record_id!r} "
            f"(model={model_artifact.value}, fitness={fitness_artifact.value}): {exc}"
        ) from exc

    if not out.exists() or out.stat().st_size == 0:
        if out.exists():
            out.unlink()
        raise GenerationFailed(
            f"dashboard generation produced an empty page for record "
            f"{record_id!r} (model={model_artifact.value}, "
            f"fitness={fitness_artifact.value})"
        )
    _record_cache_key(record_id, key)
    return out


def read_or_generate_escher(
    escher: Any,
    *,
    record_id: str,
    model_artifact: ResolvedArtifact,
    map_name: str,
    flux: Optional[Dict[str, float]] = None,
    flux_artifact: Optional[ResolvedArtifact] = None,
) -> Path:
    """Return the cached single-map Escher HTML path, generating on a miss.

    Delegates to :meth:`EscherUtils.create_map_html2` with the flux from the
    selected FBA/FVA record. The cache key covers ``record_id``, the map name and
    the mtimes of the model and (optional) flux artifacts. Raises
    :class:`GenerationFailed` naming the artifact on renderer error, and never
    returns an empty file.
    """
    inputs = [model_artifact]
    if flux_artifact is not None:
        inputs.append(flux_artifact)
    key = cache_key(record_id, inputs, suffix=f"escher:{map_name}")
    out = cached_html_path(key)
    if out.exists() and out.stat().st_size > 0:
        _record_cache_key(record_id, key)
        return out

    try:
        escher.create_map_html2(
            model_artifact.value,
            map_name,
            str(out),
            flux=flux,
        )
    except Exception as exc:  # noqa: BLE001 - name the artifact, don't leak a trace
        if out.exists():
            out.unlink()
        raise GenerationFailed(
            f"escher map generation failed for record {record_id!r} "
            f"(model={model_artifact.value}, map={map_name}): {exc}"
        ) from exc

    if not out.exists() or out.stat().st_size == 0:
        if out.exists():
            out.unlink()
        raise GenerationFailed(
            f"escher map generation produced an empty page for record "
            f"{record_id!r} (model={model_artifact.value}, map={map_name})"
        )
    _record_cache_key(record_id, key)
    return out


# ── poll tokens (binding S18) ────────────────────────────────────────────────────


def _poll_dir() -> Path:
    """Return the poll-token directory under the app state dir, creating it."""
    d = resolve_app_state_dir() / "poll"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _poll_index_path() -> Path:
    """Return the poll-token index file.

    Binding S18 requires ONE file PER token at ``poll/<token>.json``. To GC those
    without walking the directory (the app package is forbidden the traversal
    verbs ``glob``/``iterdir``/``listdir``/``scandir`` by
    ``tests/models_and_analyses/test_layering.py``), this sidecar records the set
    of live token names. GC reads it, stats each named file, and prunes both.
    """
    return _poll_dir() / "_index.json"


def _read_poll_index() -> List[str]:
    """Return the list of token names in the index, or [] when absent/corrupt."""
    path = _poll_index_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [t for t in data if isinstance(t, str)] if isinstance(data, list) else []


def _write_poll_index(tokens: List[str]) -> None:
    """Persist the token-name index atomically."""
    path = _poll_index_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sorted(set(tokens))), encoding="utf-8")
    tmp.replace(path)


def new_poll_token() -> str:
    """Return a fresh poll token of the form ``pt-<uuid4>`` (binding S18)."""
    return f"pt-{uuid.uuid4()}"


def _now_iso() -> str:
    """Return a UTC ISO-8601 timestamp with microseconds (matches S32 shape)."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def write_poll_token(
    token: str,
    *,
    status: str,
    record_id: str,
    map_name: Optional[str],
    error: Optional[str] = None,
    html_path: Optional[str] = None,
) -> Path:
    """Persist a poll token's state (binding S18 schema).

    Stored at ``resolve_app_state_dir()/poll/<token>.json`` with ``status``
    (``pending`` | ``ready`` | ``error``), ``record_id``, ``map`` and
    ``updated_at``. ``error`` is added when ``status == "error"`` so a failing
    generation names the artifact through the poll surface too. ``html_path`` is
    added when ``status == "ready"`` so the poll endpoint can serve the cached
    HTML directly (the token alone cannot reconstruct the cache key).
    """
    if status not in ("pending", "ready", "error"):
        raise ValueError(f"invalid poll status {status!r}")
    payload: Dict[str, Any] = {
        "status": status,
        "record_id": record_id,
        "map": map_name,
        "updated_at": _now_iso(),
    }
    if error is not None:
        payload["error"] = error
    if html_path is not None:
        payload["html_path"] = html_path
    path = _poll_dir() / f"{token}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)  # atomic swap so a poller never reads a half-written file
    # Record the token in the index so GC can find it without walking the dir.
    index = _read_poll_index()
    if token not in index:
        index.append(token)
        _write_poll_index(index)
    return path


def read_poll_token(token: str) -> Optional[Dict[str, Any]]:
    """Return a poll token's stored state, or None when the token is unknown.

    The endpoint maps None to 404-unknown-token.
    """
    if not POLL_TOKEN_RE.match(token):
        return None
    path = _poll_dir() / f"{token}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def gc_poll_tokens(now: Optional[float] = None) -> int:
    """Remove poll tokens older than 24h (binding S18); return the count removed.

    Called at app startup. Age is by file mtime; ``now`` is injectable for tests.
    Iterates the token-name INDEX rather than walking the directory, because the
    app package is forbidden the directory-traversal verbs (see
    ``tests/models_and_analyses/test_layering.py`` and :func:`_poll_index_path`).
    """
    cutoff = (now if now is not None else time.time()) - POLL_TOKEN_TTL_SECONDS
    removed = 0
    d = _poll_dir()
    survivors: List[str] = []
    for token in _read_poll_index():
        path = d / f"{token}.json"
        try:
            if not path.exists():
                # Stale index entry — drop it, nothing to unlink.
                removed += 1
                continue
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
                continue
        except OSError:
            survivors.append(token)
            continue
        survivors.append(token)
    _write_poll_index(survivors)
    return removed
