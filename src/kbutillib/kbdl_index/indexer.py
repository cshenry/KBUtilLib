"""Index swept KBDL job results into the KOROS arc store.

One AnalysisRecord per KBDL job, keyed so that re-indexing REPLACES rather than
duplicates: ``run_uid`` is the KBDL job id, ``record_id`` derives from it.

Population (conductor amendment A): the sweep ledger is not a complete record of
what was swept, so the indexed set is the UNION of (a) every ledger entry of an
indexable type and (b) every ``<results_root>/<JobType>/<job_id>.json.gz`` of an
indexable type with no ledger entry (a "recovered" result, status 'ok').

Timestamps (amendment B): ``created_at`` comes from caller-supplied ``job_meta``
(finished_at, then submitted_at) and then the ledger's submitted_at. A job with
no date anywhere is NOT written and is counted as ``undated``; the wall clock and
file mtimes are never used. This module makes no network call.

Status (amendment C): 'failed' only when ``job_meta[job_id]['state']`` says so.

KNOWN CONTRACT CONFLICT: the task asks for status 'expired', but the arc store's
STATUS_VALUES is {'ok', 'failed', 'partial'} and rejects anything else (soft-fail,
so the row would silently never exist). ``EXPIRED_STATUS`` therefore maps to
'failed', and the expiry is carried in payload['result_state'] and
provenance['result_state'] == 'expired'. Widening the store's contract is a
decision outside this module; change the one constant if that happens.
"""
from __future__ import annotations

import gzip
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Collection, Dict, Mapping, Optional

from kbutillib.koros_arc_store import CONTRACT_VERSION, AnalysisRecord
from kbutillib.koros_arc_store.identity import derive_analysis_id, derive_record_id

PRODUCER = 'kbdl-job-indexer'
PRODUCER_VERSION = '1'

#: job_type -> (record kind, trust tier)
INDEXABLE = {
    'KBDLBuildGenome': ('kbdl.build_genome', 'verified'),
    'KBDLGenomeAnnotation': ('kbdl.genome_annotation', 'homology'),
}

#: See the module docstring: the store has no 'expired' status.
EXPIRED_STATUS = 'failed'

ATTRIBUTION_SOURCES = ('submission_stamp', 'existing_row', 'arc_tree_mention', 'none')


@dataclass
class IndexReport:
    jobs_seen: int = 0
    from_ledger: int = 0
    from_files_only: int = 0
    records_written: int = 0
    would_write: int = 0            # dry-run: records that would have been written
    unresolved: int = 0             # no subject, or malformed/missing result
    undated: int = 0                # no created_at available; not written
    expired: int = 0
    ledger_errors: int = 0          # ledger outcome neither saved nor expired, no file
    write_failed: int = 0           # store soft-failed (failed_write_count rose)
    skipped_existing: int = 0       # job already has a row from another producer
    skipped_by_type: Dict[str, int] = field(default_factory=dict)
    attributed_by_source: Dict[str, int] = field(
        default_factory=lambda: {s: 0 for s in ATTRIBUTION_SOURCES})
    disagreeing_jobs: int = 0
    multi_arc_discarded: int = 0
    subject_ambiguities: int = 0
    dry_run: bool = False

    @property
    def did_nothing(self) -> bool:
        return self.records_written == 0 and self.would_write == 0

    def summary(self) -> str:
        head = ('NOTHING INDEXED: ' if self.did_nothing else '') + (
            'dry-run ' if self.dry_run else '')
        return (f'{head}seen={self.jobs_seen} (ledger={self.from_ledger}, '
                f'files_only={self.from_files_only}) written={self.records_written} '
                f'would_write={self.would_write} unresolved={self.unresolved} '
                f'undated={self.undated} expired={self.expired} '
                f'ledger_errors={self.ledger_errors} write_failed={self.write_failed} '
                f'skipped_existing={self.skipped_existing} '
                f'skipped_by_type={self.skipped_by_type} '
                f'attributed={self.attributed_by_source} '
                f'disagreeing={self.disagreeing_jobs} '
                f'multi_arc_discarded={self.multi_arc_discarded} '
                f'ambiguities={self.subject_ambiguities}')


def normalize_timestamp(value: Any) -> Optional[str]:
    """Return exactly YYYY-MM-DDTHH:MM:SS.ffffffZ (UTC), or None if unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(('Z', 'z')):
        text = text[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _stringify_floats(value: Any) -> Any:
    """significant_params rejects floats anywhere; format them as strings."""
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, dict):
        return {k: _stringify_floats(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_stringify_floats(v) for v in value]
    return value


def _read_ledger(ledger_path) -> Dict[str, dict]:
    """job_id -> last well-formed ledger line. A missing ledger is empty."""
    entries: Dict[str, dict] = {}
    if not ledger_path or not os.path.exists(ledger_path):
        return entries
    with open(ledger_path, encoding='utf-8') as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get('job_id') and rec.get('job_type'):
                entries[rec['job_id']] = rec
    return entries


def _file_only_jobs(results_root, ledger_ids) -> Dict[str, str]:
    """job_id -> job_type for indexable result files with no ledger entry."""
    found: Dict[str, str] = {}
    for jtype in INDEXABLE:
        d = os.path.join(results_root, jtype)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if name.endswith('.json.gz'):
                jid = name[:-len('.json.gz')]
                if jid not in ledger_ids:
                    found[jid] = jtype
    return found


def _load_result(path):
    with gzip.open(path, 'rt', encoding='utf-8') as fh:
        return json.load(fh)


class StoreDisabledError(RuntimeError):
    """A real (non-dry) index was requested against a disabled run DB."""


DISABLED_MESSAGE = ("arc-store run DB is disabled (set KBDL_RUN_DB_ENABLED=1, or KB_POD); "
                    "refusing a real index that would silently write nothing")


def require_store_enabled(store) -> None:
    """Raise StoreDisabledError if ``store`` has a run DB that is disabled.

    Stores without a ``.db`` (CaptureStore, in-memory fakes) always accept writes.
    """
    db = getattr(store, 'db', None)
    if db is not None and getattr(db, 'enabled', True) is False:
        raise StoreDisabledError(DISABLED_MESSAGE)


def index_swept_jobs(store, resolver, attributor, ledger_path, results_root,
                     dry_run: bool = False,
                     job_meta: Optional[Mapping[str, dict]] = None,
                     job_types: Optional[Collection[str]] = None,
                     skip_existing_run_uids: Optional[Collection[str]] = None
                     ) -> IndexReport:
    """Index swept KBDL jobs into ``store``. See the module docstring.

    ``job_types`` (optional) restricts indexing to those job types: ledger jobs
    of other types are counted in ``skipped_by_type``; file-only jobs of other
    types are not counted at all.

    ``skip_existing_run_uids`` (optional): job ids that already have a row written
    by another producer; those jobs are not written and are counted in
    ``skipped_existing``. The caller computes the set (this function never queries
    the store) and must leave out this indexer's own rows so re-attribution still
    upserts by record_id.
    """
    if not dry_run:
        require_store_enabled(store)
    report = IndexReport(dry_run=dry_run)
    job_meta = job_meta or {}
    ledger = _read_ledger(ledger_path)

    work = []   # (job_id, job_type, ledger_entry | None)
    for jid, ent in ledger.items():
        report.jobs_seen += 1
        report.from_ledger += 1
        if ent['job_type'] not in INDEXABLE or (
                job_types and ent['job_type'] not in job_types):
            report.skipped_by_type[ent['job_type']] = \
                report.skipped_by_type.get(ent['job_type'], 0) + 1
            continue
        work.append((jid, ent['job_type'], ent))
    for jid, jtype in _file_only_jobs(results_root, set(ledger)).items():
        if job_types and jtype not in job_types:
            continue
        report.jobs_seen += 1
        report.from_files_only += 1
        work.append((jid, jtype, None))

    skip_uids = frozenset(skip_existing_run_uids or ())
    for jid, jtype, ent in work:
        if jid in skip_uids:
            report.skipped_existing += 1
            continue
        kind, tier = INDEXABLE[jtype]
        meta = job_meta.get(jid) or {}
        path = os.path.join(results_root, jtype, jid + '.json.gz')
        has_file = os.path.isfile(path)
        outcome = (ent or {}).get('outcome', 'saved')

        # ---- created_at: finished_at > meta submitted_at > ledger submitted_at
        created_at = src = None
        for label, raw in (('finished_at', meta.get('finished_at')),
                           ('submitted_at', meta.get('submitted_at')),
                           ('submitted_at', (ent or {}).get('submitted_at'))):
            created_at = normalize_timestamp(raw)
            if created_at:
                src = label
                break
        if created_at is None:
            report.undated += 1
            continue

        failed = str(meta.get('state', '')).lower() == 'failed'
        result = None
        if has_file:
            try:
                result = _load_result(path)
            except Exception:                              # noqa: BLE001 - fail-soft
                report.unresolved += 1
                continue
            status = 'failed' if failed else 'ok'
            artifacts = {'result': path}
        elif failed:
            status, artifacts = 'failed', {}
        elif outcome == 'expired':
            status, artifacts = EXPIRED_STATUS, {}
        elif outcome == 'saved':                           # promised a file; none there
            report.unresolved += 1
            continue
        else:
            report.ledger_errors += 1
            continue
        is_expired = status == EXPIRED_STATUS and not has_file and not failed \
            and outcome == 'expired'

        # ---- subject
        subject = None
        if result is not None:
            try:
                subject = resolver.resolve(jtype, jid, result)
            except Exception:                              # noqa: BLE001 - fail-soft
                subject = None
            if subject is None:
                report.unresolved += 1
                continue
            subject_name = subject.name or subject.object_id
            if not subject_name:
                report.unresolved += 1
                continue
        else:
            # no result bytes to resolve from (expired / failed): the job id is
            # the only honest subject
            subject_name = 'kbdl-job:' + jid
        object_id = getattr(subject, 'object_id', None)

        params = _stringify_floats({'job_type': jtype, 'object_id': object_id})
        analysis_id = derive_analysis_id(kind, subject_name, params)

        attribution = attributor.attribute(jid)
        prov: Dict[str, Any] = {
            'job_id': jid, 'job_type': jtype,
            'population': 'ledger' if ent is not None else 'file_only',
            'created_at_source': src,
            'attribution_source': attribution.source if attribution else 'none',
        }
        payload: Dict[str, Any] = {'job_type': jtype, 'object_id': object_id,
                                   'genome_id': getattr(subject, 'genome_id', None)}
        if is_expired:
            prov['result_state'] = payload['result_state'] = 'expired'
        if tier == 'homology':
            prov['bridge_kind'] = 'sequence_homolog'
            prov['metric'] = {'name': 'feature_count',
                              'value': len(result) if hasattr(result, '__len__') else 0}

        record = AnalysisRecord(
            record_id=derive_record_id(analysis_id, jid), analysis_id=analysis_id,
            run_uid=jid, kind=kind, created_at=created_at, producer=PRODUCER,
            producer_version=PRODUCER_VERSION, subject=subject_name, status=status,
            artifacts=artifacts, payload=payload, trust_tier=tier, provenance=prov,
            contract_version=CONTRACT_VERSION)

        report.attributed_by_source[prov['attribution_source']] += 1
        if is_expired:
            report.expired += 1
        if dry_run:
            report.would_write += 1
            continue
        before = store.failed_write_count
        store.record_analysis(attribution.project if attribution else None,
                              attribution.arc_slug if attribution else None, record)
        db = getattr(store, 'db', None)
        if db is not None and getattr(db, 'enabled', True) is False:
            continue                                       # no-op write: never counted
        if store.failed_write_count > before:
            report.write_failed += 1
        else:
            report.records_written += 1

    report.disagreeing_jobs = getattr(attributor, 'disagreeing_jobs', 0)
    report.multi_arc_discarded = getattr(attributor, 'multi_arc_discarded', 0)
    report.subject_ambiguities = len(getattr(resolver, 'ambiguities', []))
    return report
