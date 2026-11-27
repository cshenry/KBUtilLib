#!/usr/bin/env python
"""Real-data dry run of the lw-3 ontology loader. Writes nothing to the lake.

    python scripts/clearinghouse_ontology_wave_dry_run.py \
        --ko-path .../kegg-ko-list-2026-11-24.tsv --cog-path .../cog-24.def.tab \
        [--snapshot-id N | --unpinned-check] [--no-store] [--out-dir DIR]

``--no-store`` reads only the pinned KO / COG files. ``--unpinned-check`` reads
the store WITHOUT a snapshot clause (a sanity check only; method names then
carry the placeholder snapshot 0). Prints the dry-run summary as JSON.
"""
from __future__ import annotations

import argparse
import json
import sys

from kbutillib.domains.kbase.berdl import clearinghouse_ontology_wave as ow


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ko-path", required=True)
    ap.add_argument("--ko-sha256", default=ow.KO_FILE_SHA256)
    ap.add_argument("--cog-path")
    ap.add_argument("--cog-sha256", default=ow.COG_FILE_SHA256)
    ap.add_argument("--snapshot-id")
    ap.add_argument("--unpinned-check", action="store_true")
    ap.add_argument("--no-store", action="store_true")
    ap.add_argument("--only", help="comma-separated ONTOLOGIES names to read from the store (check runs)")
    ap.add_argument("--out-dir")
    args = ap.parse_args()

    ko = ow.load_ko_file(args.ko_path, args.ko_sha256)
    cog = ow.load_cog_file(args.cog_path, args.cog_sha256) if args.cog_path else None
    only = args.only.split(",") if args.only else None
    statements: list = []
    snapshot = args.snapshot_id
    if not args.no_store:
        from kbutillib.domains.kbase.kb_berdl_utils import KBBERDLUtils

        query = ow.berdl_query_fn(KBBERDLUtils())
        if args.unpinned_check:
            snapshot = "0"
            statements = ow.fetch_statements(query, snapshot, as_of=False, only=only)
        else:
            snapshot = snapshot or ow.resolve_snapshot_id(query)
            statements = ow.fetch_statements(query, snapshot, only=only)
    snapshot = snapshot or "0"
    result = ow.build_ontology_wave(
        statements, snapshot_id=snapshot, ko_entries=ko, ko_sha256=args.ko_sha256,
        cog_entries=cog, cog_sha256=args.cog_sha256 if cog else None,
    )
    if args.out_dir:
        ow.write_term_function_map(result.term_function_map, f"{args.out_dir}/term_function_map.tsv")
    json.dump(result.dry_run_summary(), sys.stdout, indent=2, sort_keys=True)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
