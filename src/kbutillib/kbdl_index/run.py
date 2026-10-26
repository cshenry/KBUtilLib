"""One index run: build the inputs fresh, then call ``index_swept_jobs``.

Shared by the ``kbu kbdl-index`` verb and the sweep loop's index phase, so both
build the subject resolver and the attributor the same way. Single-threaded by
design: the run DB is multi-writer, and the index phase must add no concurrency.

Network use is bounded and explicit: one ``list_objects`` call (the resolver),
one ``list_jobs`` call (job_meta: submitted_at and state for every job), and,
only when ``fetch_finished_at`` is set, one ``check_job`` per INDEXABLE job.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from kbutillib.kbdl_index.attribution import (
    build_attributor, load_existing_rows, scan_arc_tree_mentions)
from kbutillib.kbdl_index.indexer import (
    INDEXABLE, PRODUCER, IndexReport, _file_only_jobs, _read_ledger, index_swept_jobs)
from kbutillib.kbdl_index.subject import build_resolver

FINISHED_KEYS = ('finished_at', 'completed_at')


def normalize_job_types(names) -> Optional[List[str]]:
    """'BuildGenome' / 'KBDLBuildGenome' -> 'KBDLBuildGenome'; empty -> None."""
    out = []
    for n in names or ():
        full = n if n.startswith('KBDL') else 'KBDL' + n
        if full not in INDEXABLE:
            raise ValueError(f'not an indexable job type: {n} '
                             f'(indexable: {sorted(INDEXABLE)})')
        out.append(full)
    return out or None


def job_meta_from_list_jobs(jobs) -> Dict[str, dict]:
    """job_id -> {job_type, state, submitted_at} from ONE list_jobs() result."""
    meta = {}
    for j in jobs or ():
        if isinstance(j, dict) and j.get('job_id'):
            meta[j['job_id']] = {k: j.get(k) for k in
                                 ('job_type', 'state', 'submitted_at')}
    return meta


def population_ids(ledger_path, results_root, job_types=None) -> Dict[str, str]:
    """job_id -> job_type for every job the indexer would consider."""
    ledger = _read_ledger(ledger_path)
    ids = {j: e['job_type'] for j, e in ledger.items()
           if e['job_type'] in INDEXABLE and (not job_types or e['job_type'] in job_types)}
    for j, t in _file_only_jobs(results_root, set(ledger)).items():
        if not job_types or t in job_types:
            ids[j] = t
    return ids


def fetch_finished_at(kbdl, job_meta, job_ids, limit=0, log: Callable = print) -> int:
    """Fill ``finished_at`` via check_job for ``job_ids`` only. Fail-soft.

    A job whose check_job raises, or carries no finished/completed timestamp, is
    left as it was. ``limit`` (>0) bounds the number of calls. Returns the
    number of jobs filled.
    """
    filled = calls = 0
    for jid in job_ids:
        if limit and calls >= limit:
            break
        calls += 1
        try:
            rec = kbdl.check_job(jid)
        except Exception as exc:                           # noqa: BLE001 - fail-soft
            if calls <= 3:
                log(f'check_job fail {jid}: {type(exc).__name__}')
            continue
        for key in FINISHED_KEYS:
            if isinstance(rec, dict) and rec.get(key):
                job_meta.setdefault(jid, {})['finished_at'] = rec[key]
                filled += 1
                break
    return filled


def _load_build_genome_results(results_root):
    import gzip
    import json
    d = os.path.join(results_root, 'KBDLBuildGenome')
    if not os.path.isdir(d):
        return
    for name in sorted(os.listdir(d)):
        if name.endswith('.json.gz'):
            try:
                with gzip.open(os.path.join(d, name), 'rt', encoding='utf-8') as fh:
                    yield json.load(fh)
            except Exception:                              # noqa: BLE001
                continue


def build_inputs(store, kbdl, ledger_path, results_root, runs_root, job_types=None):
    """Fresh resolver and attributor for one run."""
    resolver = build_resolver(kbdl.list_objects(),
                              _load_build_genome_results(results_root))
    ids = population_ids(ledger_path, results_root, job_types)
    attributor = build_attributor(
        store.list_submissions(), load_existing_rows(store),
        scan_arc_tree_mentions(runs_root, ids))
    return resolver, attributor


class CaptureStore:
    """Stands in for the store during a dry run: records, never persists."""
    failed_write_count = 0

    def __init__(self):
        self.captured = []      # (project, arc, AnalysisRecord)

    def record_analysis(self, project, arc, record, detail=None):
        self.captured.append((project, arc, record))


@dataclass
class RunResult:
    report: IndexReport
    job_meta: Dict[str, dict] = field(default_factory=dict)
    captured: list = field(default_factory=list)   # dry run only
    finished_at_filled: int = 0


def existing_rows_by_run_uid(store) -> Dict[str, list]:
    """Every existing row keyed by run_uid -> [(project, arc, record)].

    Reads only through the store's public list_* API: all projects/arcs, plus
    list_analyses(None, None) for unattributed rows.
    """
    existing: Dict[str, list] = {}
    pairs = [(None, None)]
    for proj in store.list_projects():
        for arc in store.list_arcs(proj.name):
            pairs.append((proj.name, arc.slug))
    for p, a in pairs:
        for rec in store.list_analyses(p, a):
            if rec.run_uid:
                existing.setdefault(rec.run_uid, []).append((p, a, rec))
    return existing


def foreign_run_uids(existing: Dict[str, list]) -> set:
    """run_uids with at least one row from a producer other than the indexer.

    The indexer's own rows are excluded so a re-index keeps upserting them by
    record_id (a job first indexed unattributed can still move into its arc).
    """
    return {uid for uid, rows in existing.items()
            if any(rec.producer != PRODUCER for _p, _a, rec in rows)}


def run_index(store, kbdl, ledger_path, results_root, runs_root, *, dry_run=False,
              job_types=None, fetch_finished=False, finished_limit=0,
              skip_existing=True, log: Callable = print) -> RunResult:
    """Run the index once. In a dry run nothing reaches ``store``: the indexer
    writes to a CaptureStore, so the exact records it WOULD write are available
    (needed for the replace-vs-duplicate overlap check), and the report is
    relabelled would_write. With ``skip_existing`` (default) jobs that already have
    a row from another producer are not written."""
    if not dry_run:
        from .indexer import require_store_enabled
        require_store_enabled(store)                       # before any network call
    meta = job_meta_from_list_jobs(kbdl.list_jobs())
    filled = 0
    if fetch_finished:
        filled = fetch_finished_at(
            kbdl, meta, [j for j in population_ids(ledger_path, results_root, job_types)],
            limit=finished_limit, log=log)
    resolver, attributor = build_inputs(store, kbdl, ledger_path, results_root,
                                        runs_root, job_types)
    skip = foreign_run_uids(existing_rows_by_run_uid(store)) if skip_existing else None
    if dry_run:
        cap = CaptureStore()
        rep = index_swept_jobs(cap, resolver, attributor, ledger_path, results_root,
                               dry_run=False, job_meta=meta, job_types=job_types,
                               skip_existing_run_uids=skip)
        rep.would_write, rep.records_written, rep.dry_run = rep.records_written, 0, True
        return RunResult(rep, meta, cap.captured, filled)
    rep = index_swept_jobs(store, resolver, attributor, ledger_path, results_root,
                           dry_run=False, job_meta=meta, job_types=job_types,
                               skip_existing_run_uids=skip)
    return RunResult(rep, meta, [], filled)


def created_at_sources(captured) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for _p, _a, rec in captured:
        k = (rec.provenance or {}).get('created_at_source', '?')
        out[k] = out.get(k, 0) + 1
    return out


def overlap_vs_existing(store, captured, population=None, examples=5,
                        existing=None) -> Dict[str, Any]:
    """For captured (would-be) records, compare against existing rows by run_uid.

    replace   = existing row with the same record_id (the write would overwrite it)
    duplicate = existing row for the same run_uid with a different record_id
    Reads only through the store's public list_* API.
    """
    if existing is None:
        existing = existing_rows_by_run_uid(store)
    replace, duplicate = [], []
    for np_, na, new in captured:
        for op, oa, old in existing.get(new.run_uid, ()):
            row = {'run_uid': new.run_uid,
                   'existing': {'producer': old.producer, 'kind': old.kind,
                                'subject': old.subject, 'project': op, 'arc': oa,
                                'record_id': old.record_id},
                   'new': {'producer': new.producer, 'kind': new.kind,
                           'subject': new.subject, 'project': np_, 'arc': na,
                           'record_id': new.record_id}}
            (replace if old.record_id == new.record_id else duplicate).append(row)
    jobs_with_existing = len({r['run_uid'] for r in replace + duplicate})
    captured_ids = {rec.run_uid for _p, _a, rec in captured}
    not_indexed = sorted(j for j in (population or ())
                         if j in existing and j not in captured_ids)
    return {'population_jobs_with_existing_row': len(
                [j for j in (population or ()) if j in existing]),
            'existing_but_indexer_would_not_write': len(not_indexed),'existing_rows_matched': len(replace) + len(duplicate),
            'jobs_with_existing': jobs_with_existing,
            'replace': len(replace), 'duplicate': len(duplicate),
            'replace_examples': replace[:examples],
            'duplicate_examples': duplicate[:examples]}
