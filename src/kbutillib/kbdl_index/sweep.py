"""Sweep completed KBDL job results to durable local storage before they expire.

KBDL expires job results. Measured 2026-10-16: results from 2026-08-14..2026-09-02
return KBDLNotFoundError ("result for job <id> is no longer available"). 16 genome
annotations were lost that way before anything referenced them.

Writes <job_id>.json.gz under ~/.kbdl/job-results/<JobType>/ -- the SAME path
arc-store's artifacts.result already points at, so a swept job is indistinguishable
from one saved at submission time and existing references keep resolving.

Design notes:
  OLDEST FIRST   -- nearest the expiry cliff goes first, so a truncated run loses
                    the least recoverable work.
  RESUMABLE      -- skips any job whose file already exists. Safe to re-run at any
                    cadence; a no-op run costs one list_jobs call.
  FAIL-SOFT      -- an expired or erroring job is recorded and the sweep continues.
                    Expiry is expected, not exceptional.
  LEDGERED       -- append-only JSONL, one line per job, so loss is auditable rather
                    than inferred from a gap.
  BRIDGE CAPTURE -- records each annotation result's locus-tag prefix (arc-store's
                    `subject` proxy). arc-store records carry either the arc or the
                    data path but never both; this ledger is the join that was never
                    written.

Adopted into KBUtilLib from the untracked ~/kbdl_sweep/sweep.py; run via
``python -m kbutillib.kbdl_index.sweep`` or scripts/kbdl_sweep_launcher.py.

Run with --types to restrict, --dry-run to count only, --max to cap one run.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import time

from kbutillib.domains.external.kbdl_service_utils import KBDLServiceUtils
from kbutillib.kbdl_index.subject import locus_prefix

ROOT = os.path.expanduser('~/.kbdl/job-results')
LEDGER = os.path.expanduser('~/.kbdl/sweep_ledger.jsonl')

#: Off-pod mirror. The primary lives under ~/.kbdl (the home directory survives pod
#: re-provisioning, and arc-store's artifacts.result already points there), but that
#: is a SINGLE copy on third-party-operated infrastructure with no uptime guarantee.
#: Dropbox syncs to all four fleet machines, which is the cheapest real redundancy.
#: Job RESULTS only -- they are receipts and payloads measured in hundreds of MB.
#: Genome/GenomeFasta OBJECTS are ~104 GB and GenomeArchive ~4.3 TB; those must never
#: be mirrored here, it would blow up sync on every machine.
MIRROR = os.path.expanduser('~/Dropbox/Science/kbdl-job-results')

#: Swept by default. SKANI and UploadObject are included deliberately -- they are
#: ~1KB and ~91B per job respectively, so the whole corpus is ~115MB, and being
#: selective costs more in judgement than it saves in bytes.
DEFAULT_TYPES = (
    'KBDLGenomeAnnotation',      # the function/ontology payload
    'KBDLBuildGenome',           # genome name -> object ids + taxonomy (the bridge)
    'KBDLModelReconstruction',
    'KBDLFitnessModelAnalysis',
    'KBDLFitnessProp',
    'KBDLHorizyn',
    'KBDLCheckM2',
    'KBDLBuildSKANIDB',
    'KBDLSKANI',
    'KBDLUploadObject',
)


def genome_map(result):
    """For a BuildGenome result, the genome -> object-id/taxonomy map it carries."""
    if not isinstance(result, dict):
        return None
    genomes = result.get('genomes')
    if not isinstance(genomes, dict):
        return None
    return {
        name: {
            'genome_object_id': info.get('genome_object_id'),
            'fasta_object_id': info.get('fasta_object_id'),
            'ncbi_taxid': (info.get('taxonomy') or {}).get('ncbi_taxid'),
        }
        for name, info in genomes.items()
        if isinstance(info, dict)
    }


def mirror_to_dropbox():
    """Copy any result file missing from the Dropbox mirror. Copy-if-absent.

    Deliberately NOT a two-way sync and never a delete: the mirror is a safety
    copy, so the only operation it performs is adding a file that is not there.
    A file already present is left untouched -- results are immutable once
    written (named by job_id, content-addressed by the job), so "present" is
    sufficient and no checksum pass is needed.

    Returns (copied, copied_bytes, skipped). Failures are counted, not raised:
    a full or unmounted Dropbox must not take down the sweep that is protecting
    the primary copy.
    """
    copied = skipped = failed = 0
    nbytes = 0
    for src_dir, _dirs, files in os.walk(ROOT):
        rel = os.path.relpath(src_dir, ROOT)
        dst_dir = os.path.join(MIRROR, rel) if rel != '.' else MIRROR
        for name in files:
            if not name.endswith('.json.gz'):
                continue                                   # skip .part leftovers
            dst = os.path.join(dst_dir, name)
            if os.path.exists(dst):
                skipped += 1
                continue
            try:
                os.makedirs(dst_dir, exist_ok=True)
                src = os.path.join(src_dir, name)
                tmp = dst + '.part'
                shutil.copy2(src, tmp)
                os.replace(tmp, dst)                       # atomic into place
                copied += 1
                nbytes += os.path.getsize(dst)
            except Exception as exc:                       # noqa: BLE001
                failed += 1
                if failed <= 3:
                    print(f'MIRROR FAIL {name}: {type(exc).__name__}: {exc}', flush=True)
    print(f'mirror: copied={copied} ({nbytes/1e6:.0f}MB) present={skipped} failed={failed}',
          flush=True)
    return copied, nbytes, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--types', nargs='*', default=list(DEFAULT_TYPES))
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--max', type=int, default=0, help='cap jobs fetched this run')
    ap.add_argument('--no-mirror', action='store_true',
                    help='skip the Dropbox mirror pass')
    ap.add_argument('--loop', type=int, default=0, metavar='SECONDS',
                    help='daemon mode: sweep, sleep SECONDS, repeat forever. '
                         'A no-op pass costs one list_jobs call, so a short '
                         'interval is cheap; 21600 (6h) is ample against a '
                         'retention window measured in weeks.')
    ap.add_argument('--index', action='store_true',
                    help='after each sweep pass, index swept BuildGenome/GenomeAnnotation '
                         'results into the arc store (writes to the run DB; opt-in). '
                         'Single-threaded, same process, same schedule.')
    args = ap.parse_args()

    if args.loop:
        while True:
            run_pass(args)
            time.sleep(args.loop)
    else:
        sweep_once(args)
        if args.index:
            index_phase(args)


def run_pass(args):
    """One loop pass: sweep, then index what was swept. Each phase is fail-soft
    on its own, so an index failure never stops the next sweep and a sweep
    failure still lets already-swept results be indexed."""
    try:
        sweep_once(args)
    except Exception as exc:                               # noqa: BLE001
        print(f'PASS FAILED {type(exc).__name__}: {exc}', flush=True)
    if getattr(args, 'index', False):
        try:
            index_phase(args)
        except Exception as exc:                           # noqa: BLE001
            print(f'INDEX FAILED {type(exc).__name__}: {exc}', flush=True)


def index_phase(args):
    """Index swept results into the arc store; inputs are built fresh each run.

    Single-threaded by design: the run DB is multi-writer, and be836b9 (silent
    record loss under concurrent writers) is the only thing making that safe.
    """
    from kbutillib.kbdl_index.run import run_index
    from kbutillib.koros_arc_store import KorosArcStore

    store = KorosArcStore()
    res = run_index(store, KBDLServiceUtils(), LEDGER, ROOT, str(store.runs_root),
                    dry_run=bool(getattr(args, 'dry_run', False)))
    print('index: ' + res.report.summary(), flush=True)
    return res


def dead_job_ids():
    """Job ids the ledger has already proven unrecoverable.

    WITHOUT THIS THE DAEMON IS A PERPETUAL RETRY LOOP. Resumability is by file
    existence, and an expired job never produces a file -- so every pass would
    re-request every already-expired job forever. Measured 2026-10-16: that is
    145,634 futile HTTP calls per pass, ~29 minutes of pointless load on
    poplar, every 6 hours.

    Expiry is TERMINAL -- a result that is gone does not come back -- so
    skipping permanently is correct rather than merely an optimisation. Only
    'expired' counts as dead; a transient 'error' is left to retry, because
    that one genuinely might succeed on a later pass.
    """
    dead = set()
    if not os.path.exists(LEDGER):
        return dead
    with open(LEDGER) as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get('outcome') == 'expired' and rec.get('job_id'):
                dead.add(rec['job_id'])
    return dead


def sweep_once(args):
    u = KBDLServiceUtils()
    jobs = [
        x for x in u.list_jobs()
        if x.get('state') == 'completed' and x['job_type'] in set(args.types)
    ]
    jobs.sort(key=lambda x: x['submitted_at'])            # oldest first

    dead = dead_job_ids()
    skipped_dead = 0
    todo = []
    for job in jobs:
        if job['job_id'] in dead:
            skipped_dead += 1
            continue
        dest = os.path.join(ROOT, job['job_type'], job['job_id'] + '.json.gz')
        if not os.path.exists(dest):
            todo.append((job, dest))
    if args.max:
        todo = todo[:args.max]

    print(f'completed in scope : {len(jobs)}', flush=True)
    print(f'known-expired skip : {skipped_dead}', flush=True)
    print(f'already on disk    : {len(jobs) - len(todo) - skipped_dead}', flush=True)
    print(f'to sweep           : {len(todo)}', flush=True)
    if args.dry_run:
        return
    if not todo:
        if not args.no_mirror:
            mirror_to_dropbox()
        return

    counts = {'saved': 0, 'expired': 0, 'error': 0}
    written = 0
    t0 = time.time()

    with open(LEDGER, 'a') as led:
        for i, (job, dest) in enumerate(todo, 1):
            jid, jtype, sub = job['job_id'], job['job_type'], job['submitted_at']
            rec = {'job_id': jid, 'job_type': jtype, 'submitted_at': sub,
                   'swept_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
            try:
                result = u.get_job_result(jid)
            except Exception as exc:                       # noqa: BLE001 - fail-soft
                kind = 'expired' if 'no longer available' in str(exc) else 'error'
                counts[kind] += 1
                rec.update(outcome=kind, error=f'{type(exc).__name__}: {str(exc)[:200]}')
                led.write(json.dumps(rec) + '\n')
                led.flush()
                continue

            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + '.part'
            with gzip.open(tmp, 'wt') as fh:                # write-then-rename
                json.dump(result, fh)
            os.replace(tmp, dest)
            size = os.path.getsize(dest)
            written += size
            counts['saved'] += 1
            rec.update(outcome='saved', bytes=size,
                       entries=len(result) if hasattr(result, '__len__') else None)
            if jtype == 'KBDLGenomeAnnotation':
                rec['locus_prefix'] = locus_prefix(result)
            elif jtype == 'KBDLBuildGenome':
                rec['genomes'] = genome_map(result)
            led.write(json.dumps(rec) + '\n')
            led.flush()

            if i % 250 == 0 or i == len(todo):
                el = time.time() - t0
                rate = i / el if el else 0
                print(f'[{i}/{len(todo)}] {counts} {written/1e6:.0f}MB '
                      f'{rate:.0f} job/s ETA {(len(todo)-i)/rate/60 if rate else 0:.0f}m',
                      flush=True)

    print(f'DONE {counts} {written/1e6:.0f}MB in {(time.time()-t0)/60:.1f}m', flush=True)
    if not args.no_mirror:
        mirror_to_dropbox()


if __name__ == '__main__':
    main()
