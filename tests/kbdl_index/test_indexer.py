"""Offline tests for the KBDL job indexer, against a real KorosArcStore on a tmpdir.

Assertions are on queried row counts: store.record_analysis soft-fails, so a
return value or the absence of an exception proves nothing.
"""
import gzip
import json
import os

import pytest

from kbutillib.kbdl_index.attribution import build_attributor
from kbutillib.kbdl_index.indexer import (
    EXPIRED_STATUS, index_swept_jobs, normalize_timestamp)
from kbutillib.kbdl_index.subject import build_resolver
from kbutillib.koros_arc_store import KorosArcStore

A = 'aaaaaaaaaaaaaaaa' + '1111'
J = ['%032x' % i for i in range(1, 12)]
SUB = '2026-10-22T16:25:00.586091+00:00'
FIN = '2026-10-22T17:00:01.000002+00:00'
ARC = ('proj', 'arc1')


@pytest.fixture
def store(tmp_path):
    root = tmp_path / 'runs'
    (root / 'proj' / 'arcs' / 'arc1').mkdir(parents=True)
    return KorosArcStore(runs_root=root, db_path=tmp_path / 'runs.sqlite', db_enabled=True)


@pytest.fixture
def env(tmp_path):
    return {'root': tmp_path / 'results', 'ledger': tmp_path / 'ledger.jsonl'}


def resolver():
    return build_resolver([{'object_id': A, 'name': 'g.fasta'}], [])


def ann():
    return {'aaaaaaaaaaaaaaaa_00001': {'rast': {}}, 'aaaaaaaaaaaaaaaa_00002': {'rast': {}}}


def put(env, jtype, jid, result):
    d = env['root'] / jtype
    d.mkdir(parents=True, exist_ok=True)
    with gzip.open(d / (jid + '.json.gz'), 'wt') as fh:
        json.dump(result, fh)


def ledger(env, *entries):
    with open(env['ledger'], 'w') as fh:
        for e in entries:
            fh.write(json.dumps(e) + '\n')


def led(jid, jtype='KBDLGenomeAnnotation', outcome='saved', sub=SUB):
    return {'job_id': jid, 'job_type': jtype, 'submitted_at': sub,
            'swept_at': '2026-10-23T00:00:00Z', 'outcome': outcome}


def run(store, env, attributor=None, **kw):
    return index_swept_jobs(store, resolver(), attributor or build_attributor({}, {}, {}),
                            env['ledger'], env['root'], **kw)


def rows(store, p=None, a=None):
    return store.list_analyses(p, a)


def test_reindex_same_job_leaves_one_row(store, env):
    put(env, 'KBDLGenomeAnnotation', J[0], ann())
    ledger(env, led(J[0]))
    run(store, env)
    run(store, env)
    assert len(rows(store)) == 1
    assert rows(store)[0].run_uid == J[0]


def test_expired_entry_has_expired_state_and_no_artifact(store, env):
    ledger(env, led(J[0], outcome='expired'))
    rep = run(store, env)
    got = rows(store)
    assert len(got) == 1 and rep.expired == 1
    assert got[0].status == EXPIRED_STATUS   # store has no 'expired' status; see module docs
    assert got[0].payload['result_state'] == 'expired'
    assert got[0].artifacts == {}


def test_created_at_is_finished_at_not_wall_clock(store, env):
    put(env, 'KBDLGenomeAnnotation', J[0], ann())
    ledger(env, led(J[0]))
    run(store, env, job_meta={J[0]: {'finished_at': FIN, 'submitted_at': SUB}})
    r = rows(store)[0]
    assert r.created_at == '2026-10-22T17:00:01.000002Z'
    assert r.provenance['created_at_source'] == 'finished_at'


def test_upload_object_entries_yield_zero_rows(store, env):
    ledger(env, led(J[0], 'KBDLUploadObject'), led(J[1], 'KBDLUploadObject'))
    rep = run(store, env)
    assert len(rows(store)) == 0
    assert rep.skipped_by_type == {'KBDLUploadObject': 2}


def test_malformed_result_does_not_abort(store, env):
    put(env, 'KBDLGenomeAnnotation', J[0], ann())
    d = env['root'] / 'KBDLGenomeAnnotation'
    (d / (J[1] + '.json.gz')).write_bytes(b'not gzip')
    ledger(env, led(J[0]), led(J[1]))
    rep = run(store, env)
    assert len(rows(store)) == 1
    assert rep.unresolved == 1


def test_dry_run_writes_nothing_but_reports(store, env):
    put(env, 'KBDLGenomeAnnotation', J[0], ann())
    ledger(env, led(J[0]))
    rep = run(store, env, dry_run=True)
    assert len(rows(store)) == 0
    assert rep.jobs_seen == 1 and rep.would_write == 1 and rep.records_written == 0
    assert rep.attributed_by_source['none'] == 1


def test_attributed_job_written_under_arc(store, env):
    put(env, 'KBDLGenomeAnnotation', J[0], ann())
    ledger(env, led(J[0]))
    rep = run(store, env, build_attributor({J[0]: ARC}, {}, {}))
    assert len(rows(store, *ARC)) == 1
    assert len(rows(store, None, None)) == 0
    assert rows(store, *ARC)[0].provenance['attribution_source'] == 'submission_stamp'
    assert rep.attributed_by_source['submission_stamp'] == 1


def test_reindex_after_attribution_moves_row(store, env):
    put(env, 'KBDLGenomeAnnotation', J[0], ann())
    ledger(env, led(J[0]))
    run(store, env)
    assert len(rows(store, None, None)) == 1
    run(store, env, build_attributor({J[0]: ARC}, {}, {}))
    assert len(rows(store, *ARC)) == 1
    assert len(rows(store, None, None)) == 0


def test_existing_row_attribution_keeps_row_in_arc(store, env):
    put(env, 'KBDLGenomeAnnotation', J[0], ann())
    ledger(env, led(J[0]))
    run(store, env, build_attributor({J[0]: ARC}, {}, {}))
    rep = run(store, env, build_attributor({}, {J[0]: ARC}, {}))
    assert len(rows(store, *ARC)) == 1
    assert rows(store, *ARC)[0].provenance['attribution_source'] == 'existing_row'
    assert rep.attributed_by_source['existing_row'] == 1


def test_file_only_job_is_indexed_ok(store, env):
    put(env, 'KBDLGenomeAnnotation', J[0], ann())
    ledger(env)
    rep = run(store, env, job_meta={J[0]: {'finished_at': FIN}})
    got = rows(store)
    assert len(got) == 1 and got[0].status == 'ok'
    assert got[0].artifacts['result'].endswith(J[0] + '.json.gz')
    assert rep.from_files_only == 1 and rep.from_ledger == 0


def test_undated_job_is_skipped_and_counted(store, env):
    put(env, 'KBDLGenomeAnnotation', J[0], ann())
    ledger(env)     # file-only, and no job_meta: no date anywhere
    rep = run(store, env)
    assert len(rows(store)) == 0
    assert rep.undated == 1


def test_meta_finished_at_beats_ledger_submitted_at(store, env):
    put(env, 'KBDLGenomeAnnotation', J[0], ann())
    ledger(env, led(J[0]))
    run(store, env, job_meta={J[0]: {'finished_at': FIN}})
    assert rows(store)[0].created_at == '2026-10-22T17:00:01.000002Z'
    run(store, env)   # ledger-only precedence for a fresh job
    put(env, 'KBDLGenomeAnnotation', J[1], ann())
    ledger(env, led(J[1]))
    run(store, env)
    r = [x for x in rows(store) if x.run_uid == J[1]][0]
    assert r.created_at == '2026-10-22T16:25:00.586091Z'
    assert r.provenance['created_at_source'] == 'submitted_at'


def test_meta_state_failed_gives_failed_status(store, env):
    put(env, 'KBDLGenomeAnnotation', J[0], ann())
    ledger(env, led(J[0]))
    run(store, env, job_meta={J[0]: {'state': 'failed'}})
    assert rows(store)[0].status == 'failed'
    assert 'result_state' not in rows(store)[0].payload


def test_normalize_timestamp():
    assert normalize_timestamp(SUB) == '2026-10-22T16:25:00.586091Z'
    assert normalize_timestamp('2026-10-22T16:25:00Z') == '2026-10-22T16:25:00.000000Z'
    assert normalize_timestamp('garbage') is None
    assert normalize_timestamp(None) is None


def test_build_genome_is_verified_tier(store, env):
    put(env, 'KBDLBuildGenome', J[0], {'genomes': {'k': {
        'genome_id': 'g', 'fasta_object_id': A, 'genome_object_id': 'go', 'taxonomy': 'B'}}})
    ledger(env, led(J[0], 'KBDLBuildGenome'))
    run(store, env)
    r = rows(store)[0]
    assert (r.kind, r.trust_tier, r.subject) == ('kbdl.build_genome', 'verified', 'g.fasta')


# ---- skip-existing (foreign-producer rows block a write) -------------------------

from kbutillib.kbdl_index.run import (  # noqa: E402
    existing_rows_by_run_uid, foreign_run_uids)


def foreign_row(store, jid, project=None, arc=None):
    from kbutillib.kbdl_index.indexer import (
        AnalysisRecord, CONTRACT_VERSION, derive_analysis_id, derive_record_id)
    aid = derive_analysis_id('annotation', 'ANA3', {'job_type': 'x'})
    rec = AnalysisRecord(
        record_id=derive_record_id(aid, jid), analysis_id=aid, run_uid=jid,
        kind='annotation', created_at='2026-10-01T00:00:00.000000Z', producer='koros.fitness-genome-analysis',
        producer_version='9', subject='ANA3', status='ok', artifacts={},
        payload={'curated': True}, trust_tier='verified',
        provenance={'hand': 'curated'}, contract_version=CONTRACT_VERSION)
    store.record_analysis(project, arc, rec)
    return rec


def skip_set(store):
    return foreign_run_uids(existing_rows_by_run_uid(store))


def test_foreign_row_blocks_write_and_is_untouched(store, env):
    put(env, 'KBDLGenomeAnnotation', J[0], ann())
    ledger(env, led(J[0]))
    foreign_row(store, J[0])
    before = [r.to_dict() if hasattr(r, 'to_dict') else repr(r) for r in rows(store)]
    assert len(before) == 1
    rep = run(store, env, skip_existing_run_uids=skip_set(store))
    assert rep.skipped_existing == 1 and rep.records_written == 0
    after = [r.to_dict() if hasattr(r, 'to_dict') else repr(r) for r in rows(store)]
    assert after == before
    assert rows(store)[0].producer == 'koros.fitness-genome-analysis'
    assert 'skipped_existing=1' in rep.summary()


def test_without_skip_the_duplicate_is_written(store, env):
    put(env, 'KBDLGenomeAnnotation', J[0], ann())
    ledger(env, led(J[0]))
    foreign_row(store, J[0])
    rep = run(store, env)
    assert rep.skipped_existing == 0 and rep.records_written == 1
    assert len(rows(store)) == 2


def test_own_row_not_in_skip_set_and_reattribution_still_moves(store, env):
    put(env, 'KBDLGenomeAnnotation', J[0], ann())
    ledger(env, led(J[0]))
    run(store, env)
    assert skip_set(store) == set()
    rep = run(store, env, build_attributor({J[0]: ARC}, {}, {}),
              skip_existing_run_uids=skip_set(store))
    assert rep.skipped_existing == 0
    assert len(rows(store, *ARC)) == 1
    assert len(rows(store, None, None)) == 0


def test_cli_dry_run_default_reports_duplicate_zero(store, env, tmp_path, monkeypatch):
    from click.testing import CliRunner
    from kbutillib.domains.external import kbdl_service_utils
    from kbutillib.interfaces.cli.kbdl_index import kbdl_index_cmd

    class Stub:
        def __init__(self, *a, **k):
            pass

        def list_jobs(self):
            return []

        def list_objects(self):
            return [{'object_id': A, 'name': 'g.fasta'}]

    monkeypatch.setattr(kbdl_service_utils, 'KBDLServiceUtils', Stub)
    monkeypatch.setenv('KBDL_RUN_DB', str(tmp_path / 'runs.sqlite'))
    monkeypatch.setenv('KBDL_RUN_DB_ENABLED', '1')
    put(env, 'KBDLGenomeAnnotation', J[0], ann())
    put(env, 'KBDLGenomeAnnotation', J[1], ann())
    ledger(env, led(J[0]), led(J[1]))
    foreign_row(store, J[0])
    args = ['--dry-run', '--json', '--results-root', str(env['root']),
            '--ledger', str(env['ledger']), '--runs-root', str(store.runs_root)]
    res = CliRunner().invoke(kbdl_index_cmd, args)
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out['overlap']['duplicate'] == 0
    assert out['skipped_existing'] == 1 and out['report']['would_write'] == 1
    res = CliRunner().invoke(kbdl_index_cmd, args + ['--no-skip-existing'])
    out = json.loads(res.output)
    assert out['overlap']['duplicate'] == 1 and out['skipped_existing'] == 0
    assert len(rows(store)) == 1       # dry run wrote nothing
