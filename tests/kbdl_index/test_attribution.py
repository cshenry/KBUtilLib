"""Offline tests for KBDL job attribution."""
import os
from types import SimpleNamespace as NS

from kbutillib.kbdl_index.attribution import (
    Attribution, build_attributor, load_existing_rows, scan_arc_tree_mentions)

J1, J2, J3, J4 = ('%032x' % i for i in (1, 2, 3, 4))
P = ('p', 'a1')
Q = ('p', 'a2')
R = ('p', 'a3')


def test_submission_beats_all():
    a = build_attributor({J1: P}, {J1: Q}, {J1: {R}})
    assert a.attribute(J1) == Attribution('p', 'a1', 'submission_stamp')


def test_existing_row_beats_tree():
    a = build_attributor({}, {J1: Q}, {J1: {R}})
    assert a.attribute(J1) == Attribution('p', 'a2', 'existing_row')


def test_tree_mention_alone():
    a = build_attributor({}, {}, {J1: {R}})
    assert a.attribute(J1) == Attribution('p', 'a3', 'arc_tree_mention')


def test_multi_arc_attributes_to_neither_and_is_counted():
    a = build_attributor({}, {}, {J1: {P, Q}, J2: {R}})
    assert a.attribute(J1) is None
    assert a.attribute(J2).source == 'arc_tree_mention'
    assert a.multi_arc_discarded == 1


def test_disagreement_resolves_high_and_counts():
    a = build_attributor({J1: P, J2: P}, {J1: Q, J2: P}, {J3: {R}})
    assert a.attribute(J1).arc_slug == 'a1'
    assert a.disagreeing_jobs == 1


def test_none_without_evidence():
    assert build_attributor({}, {}, {}).attribute(J4) is None


def test_scan_nested_and_ignores_non_job_hex(tmp_path):
    d = tmp_path / 'p' / 'arcs' / 'a1' / 'sub' / 'deep'
    d.mkdir(parents=True)
    other = '%032x' % 999
    (d / 'x.json').write_text('{"job": "%s", "other": "%s"}' % (J1, other))
    (d / 'bin.dat').write_bytes(b'\xff\x00' + J2.encode() + b'\x00')
    d2 = tmp_path / 'p' / 'arcs' / 'a2'
    d2.mkdir(parents=True)
    (d2 / 'y.txt').write_text('see ' + J2)
    (d2 / 'long.txt').write_text('0' * 40 + J1 + 'f')  # part of a longer hex run
    (tmp_path / 'p' / 'loose.txt').write_text(J3)  # outside arcs/
    got = scan_arc_tree_mentions(tmp_path, [J1, J2, J3])
    assert got == {J1: {('p', 'a1')}, J2: {('p', 'a1'), ('p', 'a2')}}
    assert other not in got


def test_scan_unreadable_is_failsoft(tmp_path):
    d = tmp_path / 'p' / 'arcs' / 'a1'
    d.mkdir(parents=True)
    f = d / 'x.txt'
    f.write_text(J1)
    f.chmod(0)
    try:
        got = scan_arc_tree_mentions(tmp_path, [J1])
    finally:
        f.chmod(0o644)
    assert got == {} or os.geteuid() == 0 or got == {J1: {('p', 'a1')}}


def test_load_existing_rows_uses_public_api():
    class Store:
        def list_projects(self):
            return [NS(name='p')]

        def list_arcs(self, project):
            return [NS(slug='a1')]

        def list_analyses(self, project, arc):
            return [NS(run_uid=J1), NS(run_uid=J2)]

    assert load_existing_rows(Store()) == {J1: ('p', 'a1'), J2: ('p', 'a1')}
