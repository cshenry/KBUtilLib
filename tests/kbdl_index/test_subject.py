"""Offline fixture tests for the KBDL subject resolver."""
from kbutillib.kbdl_index.subject import Subject, build_resolver

A = 'aaaaaaaaaaaaaaaa' + '1111'
B = 'aaaaaaaaaaaaaaaa' + '2222'
C = 'cccccccccccccccc' + '3333'
OBJS = [{'object_id': A, 'name': 'NC_005966.1.fasta'},
        {'object_id': C, 'name': 'other.fasta'}]


def ann(prefix):
    return {prefix + '_00001': {'rast': {}}, prefix + '_00002': {'rast': {}}}


def bg(key, fasta, **kw):
    rec = {'genome_id': 'unk4863', 'prefix': 'p', 'fasta_object_id': fasta,
           'genome_object_id': 'g1', 'taxonomy': 'Bacteria'}
    rec.update(kw)
    return {'genomes': {key: rec}, 'errors': [], 'coordinate_convention': 'x'}


def test_annotation_unique_prefix():
    r = build_resolver(OBJS, [bg('k', A)])
    s = r.resolve('KBDLGenomeAnnotation', 'j1', ann('aaaaaaaaaaaaaaaa'))
    assert s == Subject(name='NC_005966.1.fasta', object_id=A,
                        genome_id='unk4863', taxonomy='Bacteria')
    assert r.ambiguities == []


def test_annotation_prefix_collision_is_ambiguous():
    r = build_resolver(OBJS + [{'object_id': B, 'name': 'dup'}], [])
    assert r.resolve('KBDLGenomeAnnotation', 'j1', ann('aaaaaaaaaaaaaaaa')) is None
    assert len(r.ambiguities) == 1 and r.ambiguities[0][0] == 'j1'


def test_annotation_prefix_no_match():
    r = build_resolver(OBJS, [])
    assert r.resolve('KBDLGenomeAnnotation', 'j1', ann('ffffffffffffffff')) is None
    assert r.ambiguities == []


def test_build_genome_uses_fasta_object_name():
    r = build_resolver(OBJS, [])
    s = r.resolve('KBDLBuildGenome', 'j1', bg('rec', A))
    assert s.name == 'NC_005966.1.fasta' and s.object_id == A
    assert s.genome_id == 'unk4863'


def test_build_genome_falls_back_to_dict_key():
    r = build_resolver(OBJS, [])
    s = r.resolve('KBDLBuildGenome', 'j1', bg('rec_key', 'z' * 20))
    assert s.name == 'rec_key'


def test_build_genome_multi_genome_is_ambiguous():
    res = bg('a', A)
    res['genomes']['b'] = dict(res['genomes']['a'], fasta_object_id=C)
    r = build_resolver(OBJS, [])
    assert r.resolve('KBDLBuildGenome', 'j1', res) is None
    assert len(r.ambiguities) == 1


def test_unknown_job_type():
    r = build_resolver(OBJS, [])
    assert r.resolve('KBDLOther', 'j1', ann('aaaaaaaaaaaaaaaa')) is None
