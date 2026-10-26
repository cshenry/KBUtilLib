"""Attribute KBDL jobs to a KOROS (project, arc) -- pure, never guesses.

Precedence, first hit wins: submission stamp, existing run-DB row, arc-tree
mention. A job id mentioned in more than one arc contributes nothing from the
arc tree (never pick one). Existing rows rank above tree mentions so that
re-indexing is monotonic: the run DB upserts project/arc_slug, so weaker
evidence must never move an attributed row. Nothing here writes to the run DB
or the runs tree.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional, Set, Tuple

SUBMISSION = 'submission_stamp'
EXISTING_ROW = 'existing_row'
ARC_TREE = 'arc_tree_mention'

_HEX32 = re.compile(rb'(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])')
_CHUNK = 1 << 20
_OVERLAP = 32

Pair = Tuple[str, str]


@dataclass(frozen=True)
class Attribution:
    project: str
    arc_slug: str
    source: str


class Attributor:
    """Lookup built once; attribute() does no I/O.

    ``disagreeing_jobs``: jobs where two or more sources named different arcs
    (the higher-precedence one wins). ``multi_arc_discarded``: jobs whose
    arc-tree mentions were discarded because they span more than one arc.
    """

    def __init__(self, submissions, existing_rows, arc_tree_mentions):
        self._submissions = {k: tuple(v) for k, v in (submissions or {}).items()
                             if v and v[0] and v[1]}
        self._existing = {k: tuple(v) for k, v in (existing_rows or {}).items()
                          if v and v[0] and v[1]}
        self._tree: Dict[str, Pair] = {}
        self.multi_arc_discarded = 0
        for job, arcs in (arc_tree_mentions or {}).items():
            arcs = set(arcs)
            if len(arcs) == 1:
                self._tree[job] = next(iter(arcs))
            elif len(arcs) > 1:
                self.multi_arc_discarded += 1
        self.disagreeing_jobs = 0
        for job in set(self._submissions) | set(self._existing) | set(self._tree):
            values = {s[job] for s in (self._submissions, self._existing, self._tree)
                      if job in s}
            if len(values) > 1:
                self.disagreeing_jobs += 1

    def attribute(self, job_id: str) -> Optional[Attribution]:
        for table, source in ((self._submissions, SUBMISSION),
                              (self._existing, EXISTING_ROW),
                              (self._tree, ARC_TREE)):
            pair = table.get(job_id)
            if pair is not None:
                return Attribution(pair[0], pair[1], source)
        return None


def build_attributor(submissions, existing_rows, arc_tree_mentions) -> Attributor:
    return Attributor(submissions, existing_rows, arc_tree_mentions)


def load_existing_rows(store) -> Dict[str, Pair]:
    """run_uid -> (project, arc_slug) for rows already carrying both.

    Goes through the store's public read API (list_projects / list_arcs /
    list_analyses). Limitation, accepted for v1: rows filed under a
    (project, arc) pair that is absent from the runs tree are not reachable
    this way and are missed.
    """
    rows: Dict[str, Pair] = {}
    for project in store.list_projects():
        for arc in store.list_arcs(project.name):
            for rec in store.list_analyses(project.name, arc.slug):
                if rec.run_uid:
                    rows[rec.run_uid] = (project.name, arc.slug)
    return rows


def scan_arc_tree_mentions(runs_root, job_ids: Iterable[str]) -> Dict[str, Set[Pair]]:
    """job_id -> set of (project, arc_slug) whose files mention it. Read-only.

    Layout <project>/arcs/<slug>/...; files outside that layout are skipped.
    Files are streamed in 1 MiB chunks; unreadable files are skipped.
    """
    wanted = {j.encode('ascii') for j in job_ids if re.fullmatch(r'[0-9a-f]{32}', j)}
    found: Dict[str, Set[Pair]] = {}
    root = Path(runs_root)
    if not wanted or not root.is_dir():
        return found
    for path in root.rglob('*'):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            rel = path.relative_to(root).parts
            if len(rel) < 4 or rel[1] != 'arcs':
                continue
            pair = (rel[0], rel[2])
            with open(path, 'rb') as fh:
                tail = b''
                while True:
                    chunk = fh.read(_CHUNK)
                    if not chunk:
                        break
                    buf = tail + chunk
                    for m in _HEX32.finditer(buf):
                        if m.group(0) in wanted:
                            found.setdefault(m.group(0).decode('ascii'), set()).add(pair)
                    tail = buf[-_OVERLAP:]
        except (OSError, ValueError):
            continue
    return found
