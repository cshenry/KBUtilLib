"""Subject/identity helpers for KBDL job results."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

PREFIX = re.compile(r'^(.*?)_\d+$')


def locus_prefix(result):
    """Shared locus-tag prefix of an annotation result's feature keys, or None."""
    if not isinstance(result, dict):
        return None
    for key in result:
        m = PREFIX.match(str(key))
        if m:
            return m.group(1)
    return None


@dataclass
class Subject:
    """What a KBDL job is about."""
    name: str
    object_id: Optional[str] = None
    genome_id: Optional[str] = None
    taxonomy: Optional[str] = None


class SubjectResolver:
    """Pure lookup over tables built once per run; resolve() does no I/O.

    Prefixes are matched for EQUALITY against object ids that exist in the
    listing; a name is never inferred from a string. Anything that cannot be
    resolved unambiguously yields None. Ambiguous jobs (prefix collision or a
    multi-genome BuildGenome result) are recorded in ``ambiguities`` as
    (job_id, reason) tuples.
    """

    def __init__(self, names, prefixes, genomes):
        self._names = names        # object_id -> name
        self._prefixes = prefixes  # object_id[:16] -> [object_id, ...]
        self._genomes = genomes    # fasta_object_id -> {genome_id, genome_object_id, taxonomy}
        self.ambiguities = []

    def resolve(self, job_type, job_id, result):
        if job_type == 'KBDLBuildGenome':
            return self._resolve_build_genome(job_id, result)
        if job_type == 'KBDLGenomeAnnotation':
            return self._resolve_annotation(job_id, result)
        return None

    def _resolve_build_genome(self, job_id, result):
        genomes = result.get('genomes') if isinstance(result, dict) else None
        if not isinstance(genomes, dict) or not genomes:
            return None
        if len(genomes) > 1:
            self.ambiguities.append((job_id, 'multiple genomes: %d' % len(genomes)))
            return None
        key, rec = next(iter(genomes.items()))
        if not isinstance(rec, dict):
            return None
        fasta = rec.get('fasta_object_id')
        name = self._names.get(fasta) or key or rec.get('genome_id')
        if not name:
            return None
        return Subject(name=name, object_id=fasta, genome_id=rec.get('genome_id'),
                       taxonomy=rec.get('taxonomy'))

    def _resolve_annotation(self, job_id, result):
        prefix = locus_prefix(result)
        if prefix is None:
            return None
        matches = self._prefixes.get(prefix, [])
        if len(matches) > 1:
            self.ambiguities.append((job_id, 'prefix %s matches %d objects' % (prefix, len(matches))))
            return None
        if not matches:
            return None
        oid = matches[0]
        name = self._names.get(oid)
        if not name:
            return None
        info = self._genomes.get(oid, {})
        return Subject(name=name, object_id=oid, genome_id=info.get('genome_id'),
                       taxonomy=info.get('taxonomy'))


def build_resolver(objects, build_genome_results):
    """Build a SubjectResolver from list_objects() output and swept BuildGenome results."""
    names, prefixes, genomes = {}, {}, {}
    for obj in objects or []:
        oid, name = obj.get('object_id'), obj.get('name')
        if not oid:
            continue
        names[oid] = name
        bucket = prefixes.setdefault(oid[:16], [])
        if oid not in bucket:
            bucket.append(oid)
    for res in build_genome_results or []:
        for rec in ((res or {}).get('genomes') or {}).values():
            if isinstance(rec, dict) and rec.get('fasta_object_id'):
                genomes[rec['fasta_object_id']] = {
                    'genome_id': rec.get('genome_id'),
                    'genome_object_id': rec.get('genome_object_id'),
                    'taxonomy': rec.get('taxonomy'),
                }
    return SubjectResolver(names, prefixes, genomes)
