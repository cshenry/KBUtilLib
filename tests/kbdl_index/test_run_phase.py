"""Offline tests: the index run, the sweep-then-index loop phase, and the CLI verb.

Everything uses tmp dirs and a stubbed KBDL client; no live calls.
"""
import argparse
import gzip
import json
import os

import pytest
from click.testing import CliRunner

from kbutillib.interfaces.cli import main
from kbutillib.kbdl_index import sweep
from kbutillib.kbdl_index.indexer import PRODUCER
from kbutillib.kbdl_index.run import (
    fetch_finished_at, normalize_job_types, run_index)
from kbutillib.koros_arc_store import AnalysisRecord, CONTRACT_VERSION, KorosArcStore

FASTA = 'bbbbbbbbbbbbbbbb' + '2222'
JB, JA = '%032x' % 1, '%032x' % 2
SUB = '2026-10-22T16:25:00.586091+00:00'
FIN = '2026-10-22T17:00:01+00:00'


class FakeKBDL:
    instances = []

    def __init__(self, token=None, **kw):
        self.token = token
        self.checked = []
        FakeKBDL.instances.append(self)

    def list_jobs(self):
        return [{'job_id': JB, 'job_type': 'KBDLBuildGenome', 'state': 'completed',
                 'submitted_at': SUB},
                {'job_id': JA, 'job_type': 'KBDLGenomeAnnotation', 'state': 'completed',
                 'submitted_at': SUB}]

    def list_objects(self):
        return [{'object_id': FASTA, 'name': 'g.fasta'}]

    def check_job(self, jid):
        self.checked.append(jid)
        if jid == JA:
            raise RuntimeError('boom')
        return {'job_id': jid, 'finished_at': FIN}

    def get_job_result(self, jid):                         # for the sweep phase
        return {'bbbbbbbbbbbbbbbb_00001': {}, 'bbbbbbbbbbbbbbbb_00002': {}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    runs = tmp_path / 'runs'
    (runs / 'proj' / 'arcs' / 'arc1').mkdir(parents=True)
    root = tmp_path / 'results'
    for jtype, jid, res in (
            ('KBDLBuildGenome', JB, {'genomes': {'G1': {'fasta_object_id': FASTA,
                                                         'genome_id': 'g1'}}}),
            ('KBDLGenomeAnnotation', JA, {'bbbbbbbbbbbbbbbb_00001': {},
                                           'bbbbbbbbbbbbbbbb_00002': {}})):
        (root / jtype).mkdir(parents=True)
        with gzip.open(root / jtype / (jid + '.json.gz'), 'wt') as fh:
            json.dump(res, fh)
    ledger = tmp_path / 'ledger.jsonl'
    ledger.write_text(json.dumps({'job_id': JB, 'job_type': 'KBDLBuildGenome',
                                  'submitted_at': SUB, 'outcome': 'saved'}) + '\n')
    db = tmp_path / 'runs.sqlite'
    monkeypatch.setenv('KOROS_HOME', str(tmp_path))
    monkeypatch.setenv('KBDL_RUN_DB', str(db))
    monkeypatch.setenv('KBDL_RUN_DB_ENABLED', '1')
    monkeypatch.setattr(sweep, 'ROOT', str(root))
    monkeypatch.setattr(sweep, 'LEDGER', str(ledger))
    monkeypatch.setattr(sweep, 'KBDLServiceUtils', FakeKBDL)
    FakeKBDL.instances.clear()
    store = KorosArcStore(runs_root=runs, db_path=db, db_enabled=True)
    return dict(store=store, root=str(root), ledger=str(ledger), runs=str(runs), db=db)


def rows(store):
    return store.list_analyses(None, None)


def test_normalize_job_types():
    assert normalize_job_types(['BuildGenome', 'KBDLGenomeAnnotation']) == [
        'KBDLBuildGenome', 'KBDLGenomeAnnotation']
    assert normalize_job_types([]) is None
    with pytest.raises(ValueError):
        normalize_job_types(['SKANI'])


def test_dry_run_writes_nothing_and_job_meta_dates_file_only_jobs(env):
    res = run_index(env['store'], FakeKBDL(), env['ledger'], env['root'], env['runs'],
                    dry_run=True)
    assert rows(env['store']) == []
    assert res.report.would_write == 2 and res.report.records_written == 0
    assert res.report.from_files_only == 1            # JA has no ledger line
    srcs = {r.run_uid: r.provenance['created_at_source'] for _p, _a, r in res.captured}
    assert srcs == {JB: 'submitted_at', JA: 'submitted_at'}


def test_real_run_writes_rows_and_job_type_filter(env):
    run_index(env['store'], FakeKBDL(), env['ledger'], env['root'], env['runs'],
              job_types=['KBDLBuildGenome'])
    got = rows(env['store'])                           # queried, not reported
    assert [r.run_uid for r in got] == [JB]
    assert got[0].producer == PRODUCER


def test_fetch_finished_at_is_bounded_and_fail_soft():
    k, meta = FakeKBDL(), {}
    n = fetch_finished_at(k, meta, [JB, JA], log=lambda *_: None)
    assert n == 1 and meta[JB]['finished_at'] == FIN and JA not in meta
    k2 = FakeKBDL()
    fetch_finished_at(k2, {}, [JB, JA], limit=1)
    assert k2.checked == [JB]


def test_default_run_makes_no_check_job_calls(env):
    k = FakeKBDL()
    run_index(env['store'], k, env['ledger'], env['root'], env['runs'], dry_run=True)
    assert k.checked == []


def test_loop_pass_sweeps_then_indexes(env, capsys):
    args = argparse.Namespace(types=list(sweep.DEFAULT_TYPES), dry_run=False, max=0,
                              no_mirror=True, loop=0, index=True)
    sweep.run_pass(args)
    assert {r.run_uid for r in rows(env['store'])} == {JB, JA}
    assert 'index: ' in capsys.readouterr().out


def test_loop_pass_without_index_flag_writes_nothing(env):
    args = argparse.Namespace(types=list(sweep.DEFAULT_TYPES), dry_run=False, max=0,
                              no_mirror=True, loop=0)
    sweep.run_pass(args)
    assert rows(env['store']) == []


def test_index_failure_does_not_raise_out_of_pass(env, monkeypatch, capsys):
    def boom(args):
        raise RuntimeError('x')
    monkeypatch.setattr(sweep, 'index_phase', boom)
    args = argparse.Namespace(types=[], dry_run=True, max=0, no_mirror=True, loop=0,
                              index=True)
    sweep.run_pass(args)
    assert 'INDEX FAILED RuntimeError' in capsys.readouterr().out


def test_cli_dry_run_overlap_replace_vs_duplicate(env, tmp_path, monkeypatch):
    import kbutillib.domains.external.kbdl_service_utils as mod
    monkeypatch.setattr(mod, 'KBDLServiceUtils', FakeKBDL)
    tok = tmp_path / 'tok'
    tok.write_text('SECRET-TOKEN\n')
    # Pre-existing row for JA under another producer -> DUPLICATE (different record_id)
    old = AnalysisRecord(
        record_id='f' * 32, analysis_id='e' * 32, run_uid=JA,
        kind='kbdl.genome_annotation', created_at='2026-10-01T00:00:00.000000Z',
        producer='other', producer_version='1', subject='old-name', status='ok',
        artifacts={}, payload={}, trust_tier='hypothesis',
        provenance={'bridge_kind': 'id_join', 'metric': {'name': 'm', 'value': 1}},
        contract_version=CONTRACT_VERSION)
    env['store'].record_analysis('proj', 'arc1', old)
    assert len(env['store'].list_analyses('proj', 'arc1')) == 1
    before = env['db'].stat().st_size
    result = CliRunner().invoke(main, ['kbdl-index', '--dry-run', '--json',
                                       '--no-skip-existing',
                                       '--token-file', str(tok),
                                       '--results-root', env['root'],
                                       '--ledger', env['ledger']])
    assert result.exit_code == 0, result.output
    assert 'SECRET-TOKEN' not in result.output
    assert FakeKBDL.instances[-1].token == 'SECRET-TOKEN'
    out = json.loads(result.output)
    assert out['overlap']['duplicate'] == 1 and out['overlap']['replace'] == 0
    assert out['report']['would_write'] == 2
    assert out['dated_from'] == {'submitted_at': 2}
    assert env['db'].stat().st_size == before
    assert rows(env['store']) == []


def test_cli_rejects_non_indexable_type(env):
    result = CliRunner().invoke(main, ['kbdl-index', '--dry-run', '--job-type', 'SKANI'])
    assert result.exit_code != 0


def test_overlap_after_real_index_is_all_replace(env):
    from kbutillib.kbdl_index.run import overlap_vs_existing
    run_index(env['store'], FakeKBDL(), env['ledger'], env['root'], env['runs'])
    res = run_index(env['store'], FakeKBDL(), env['ledger'], env['root'], env['runs'],
                    dry_run=True)
    ov = overlap_vs_existing(env['store'], res.captured)
    assert ov['replace'] == 2 and ov['duplicate'] == 0


# ---- disabled run DB guard ---------------------------------------------------

GATE = 'arc-store run DB is disabled (set KBDL_RUN_DB_ENABLED=1, or KB_POD)'


@pytest.fixture
def disabled_env(env, monkeypatch):
    for var in ('KBDL_RUN_DB_ENABLED', 'KB_POD', 'KBDL_POD'):
        monkeypatch.delenv(var, raising=False)
    env['store'] = KorosArcStore(runs_root=env['runs'], db_path=env['db'])
    assert env['store'].db.enabled is False
    return env


def _db_count(path):
    import sqlite3
    if not path.exists():
        return 0                                           # never created: zero rows
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute('select count(*) from runs').fetchone()[0]
    finally:
        conn.close()


def test_real_index_against_disabled_store_raises_and_writes_nothing(disabled_env):
    from kbutillib.kbdl_index.indexer import StoreDisabledError
    env = disabled_env
    with pytest.raises(StoreDisabledError, match='refusing a real index'):
        run_index(env['store'], FakeKBDL(), env['ledger'], env['root'], env['runs'])
    assert _db_count(env['db']) == 0
    assert rows(env['store']) == []


def test_dry_run_against_disabled_store_still_works(disabled_env):
    env = disabled_env
    res = run_index(env['store'], FakeKBDL(), env['ledger'], env['root'], env['runs'],
                    dry_run=True)
    assert res.report.would_write > 0 and res.report.records_written == 0


def test_cli_real_index_against_disabled_store_exits_nonzero(disabled_env):
    env = disabled_env
    result = CliRunner().invoke(main, ['kbdl-index', '--runs-root', env['runs'],
                                       '--results-root', env['root'],
                                       '--ledger', env['ledger']])
    assert result.exit_code != 0
    assert GATE in result.output
    assert 'Traceback' not in result.output
    assert _db_count(env['db']) == 0


def test_enabled_real_index_count_matches_row_delta(env):
    before = _db_count(env['db'])
    res = run_index(env['store'], FakeKBDL(), env['ledger'], env['root'], env['runs'])
    assert res.report.records_written == 2
    assert _db_count(env['db']) - before == res.report.records_written


def test_loop_pass_with_disabled_db_logs_index_failure_and_continues(
        disabled_env, capsys, monkeypatch):
    monkeypatch.setenv('KOROS_HOME', os.path.dirname(disabled_env['runs']))
    args = argparse.Namespace(types=list(sweep.DEFAULT_TYPES), dry_run=False, max=0,
                              no_mirror=True, loop=0, index=True)
    sweep.run_pass(args)                                   # must not raise
    out = capsys.readouterr().out
    assert 'INDEX FAILED StoreDisabledError' in out and GATE in out
    assert _db_count(disabled_env['db']) == 0
