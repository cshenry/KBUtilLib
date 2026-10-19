"""Build the GO metadata table that :mod:`ontology_mapping` attaches to candidates.

The go-text models return bare GO accessions. A caller -- and above all the AI
council that adjudicates the ranked list -- cannot judge ``GO:0003688`` without
being told it means DNA replication origin binding, and the council-faithful
candidate cut needs each term's *height* in the GO tree. This script derives
both from ``go-basic.obo``.

It is a port of ``build_go_metadata.py`` from the 2026-09-10 GO council run
(``AIAssistant/go-council-run-2026-09-10/``), with one change: the output is
restricted to the GO ids the vendored go-text models can return (plus the
alt_ids that resolve onto them), so the vendored table stays small.

Depth and height are not interchangeable:

    depth   edges from the root, minimised over parents.
    height  edges to the furthest descendant. Height 0 is a leaf. A term can
            sit deep in the tree and still be generic -- "hydrolase activity"
            has depth 2 and height 7 -- which is why the council cut is on
            height, not depth.

Height is computed over the WHOLE live ontology before the subset is taken,
so restricting the output does not change any term's height.

Usage::

    python -m kbutillib.domains.genome.ontology_mapping.build_go_text_metadata \\
        --obo go-basic.obo
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path
from typing import Dict, Iterable, Set, Tuple

#: Definitions are truncated to this many characters, as the council input was.
DEF_MAX_CHARS = 260


def parse_obo(path: Path) -> Tuple[dict, dict, str]:
    """Return (live terms keyed by primary id, alt_id -> primary id, data-version)."""
    terms: Dict[str, dict] = {}
    alt: Dict[str, str] = {}
    version = ""
    cur = None

    def flush(c):
        if c and c.get("id"):
            terms[c["id"]] = c

    with path.open() as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            if not version and line.startswith("data-version:"):
                version = line.split(":", 1)[1].strip()
            if line == "[Term]":
                flush(cur)
                cur = {"id": None, "name": None, "ns": None, "parents": [],
                       "alts": [], "obsolete": False, "def": None}
            elif line.startswith("[") and line.endswith("]"):
                flush(cur)
                cur = None
            elif cur is not None:
                if line.startswith("id: GO:"):
                    cur["id"] = line[4:].strip()
                elif line.startswith("name: "):
                    cur["name"] = line[6:].strip()
                elif line.startswith("namespace: "):
                    cur["ns"] = line[11:].strip()
                elif line.startswith("is_a: "):
                    cur["parents"].append(line[6:].split("!")[0].strip())
                elif line.startswith("def: "):
                    body = line[5:]
                    if body.startswith('"'):
                        end = body.find('"', 1)
                        if end > 0:
                            cur["def"] = body[1:end]
                elif line.startswith("alt_id: GO:"):
                    cur["alts"].append(line[8:].strip())
                elif line.startswith("is_obsolete: true"):
                    cur["obsolete"] = True
    flush(cur)

    live = {k: v for k, v in terms.items() if not v["obsolete"]}
    for k, v in live.items():
        for a in v["alts"]:
            alt[a] = k
    return live, alt, version


def tree_stats(live: dict) -> Tuple[dict, dict]:
    """Return (depth, height) for every live term, over is_a edges."""
    kids = collections.defaultdict(list)
    for k, v in live.items():
        for p in v["parents"]:
            if p in live:
                kids[p].append(k)
    sys.setrecursionlimit(300000)
    depth: Dict[str, int] = {}
    height: Dict[str, int] = {}

    def d(n):
        if n in depth:
            return depth[n]
        depth[n] = 0  # guards against a cycle rather than recursing forever
        ps = [p for p in live[n]["parents"] if p in live]
        depth[n] = 0 if not ps else 1 + min(d(p) for p in ps)
        return depth[n]

    def h(n):
        if n in height:
            return height[n]
        height[n] = 0
        cs = kids.get(n, [])
        height[n] = 0 if not cs else 1 + max(h(c) for c in cs)
        return height[n]

    for n in live:
        d(n)
    for n in live:
        h(n)
    return depth, height


def build(obo: Path, keep_ids: Iterable[str]) -> dict:
    """Build the metadata table restricted to ``keep_ids`` (resolved via alt_id)."""
    live, alt, version = parse_obo(obo)
    depth, height = tree_stats(live)
    keep: Set[str] = {alt.get(g, g) for g in keep_ids}
    keep &= set(live)
    return {
        "schema_version": 1,
        "obo_version": version,
        "name": {k: live[k]["name"] for k in sorted(keep)},
        "ns": {k: live[k]["ns"] for k in sorted(keep)},
        "def": {k: (live[k]["def"] or "")[:DEF_MAX_CHARS] for k in sorted(keep) if live[k]["def"]},
        "depth": {k: depth[k] for k in sorted(keep)},
        "height": {k: height[k] for k in sorted(keep)},
        "alt_id": {a: p for a, p in sorted(alt.items()) if p in keep},
    }


def main() -> None:
    from .go_text import DEFAULT_ARTIFACT_ROOT, GO_METADATA_PATH, GoTextModel, UNION_ARTIFACTS

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--obo", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=GO_METADATA_PATH)
    args = ap.parse_args()

    ids: Set[str] = set()
    for artifact in UNION_ARTIFACTS:
        ids.update(GoTextModel(DEFAULT_ARTIFACT_ROOT / artifact).labels)
    table = build(args.obo, ids)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(table, sort_keys=True, separators=(",", ":")) + "\n")
    print(f"obo {table['obo_version']}: {len(ids)} model labels -> {len(table['name'])} live terms -> {args.out}")


if __name__ == "__main__":
    main()
