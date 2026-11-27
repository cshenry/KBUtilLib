#!/usr/bin/env python
"""Collect the CDM genome-hash fixture used by the v2.0 standardizer tests.

Queries BERDL ``kbase.genomes`` through ``KBBERDLUtils`` (token read from the
user's KBUtilLib config; never printed) for 15 contig collections with
<= 60 contigs, one genome at a time (the query API caps at ~100 rows), and
writes ``tests/domains/fixtures/cdm_genome_hash_fixture.json``:

    {"genomes": [{"contig_collection_id", "contig_collection_hash",
                  "contig_hashes": [...]}, ...]}

Run (network + BERDL token required; tests never run this):

    python scripts/identity/collect_cdm_genome_hash_fixture.py [out.json]

Selection is deterministic given the table order: it walks contig_collection
rows page by page and takes candidates until it has 15, guaranteeing a mix of
single-contig and multi-contig genomes (at least 8 with >= 2 contigs).
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from kbutillib import KBBERDLUtils

TARGET = 15
MIN_MULTI = 8
MAX_CONTIGS = 60
DEFAULT_OUT = (
    Path(__file__).resolve().parents[2]
    / "tests" / "domains" / "fixtures" / "cdm_genome_hash_fixture.json"
)


def main(out: Path) -> int:
    u = KBBERDLUtils()
    logging.disable(logging.CRITICAL)  # keep config/token chatter out of logs

    picked, multi, offset = [], 0, 0
    while len(picked) < TARGET and offset < 5000:
        page = u.query(
            "SELECT contig_collection_id, hash FROM kbase.genomes.contig_collection "
            "WHERE hash IS NOT NULL",
            limit=100, offset=offset,
        )
        if not page.get("success"):
            print("BERDL query failed:", str(page.get("error"))[:200])
            return 2
        rows = page["data"]
        if not rows:
            break
        offset += len(rows)
        for row in rows:
            if len(picked) >= TARGET:
                break
            cid = row["contig_collection_id"]
            res = u.query(
                "SELECT c.hash FROM kbase.genomes.contig_x_contig_collection x "
                "JOIN kbase.genomes.contig c ON c.contig_id = x.contig_id "
                f"WHERE x.contig_collection_id = '{cid}'",
                limit=MAX_CONTIGS + 1,
            )
            if not res.get("success"):
                print("contig query failed for", cid, str(res.get("error"))[:120])
                return 2
            hashes = [r["hash"] for r in res["data"]]
            if not hashes or len(hashes) > MAX_CONTIGS or None in hashes:
                continue
            need_multi = MIN_MULTI - multi
            remaining = TARGET - len(picked)
            if len(hashes) == 1 and need_multi >= remaining:
                continue  # reserve the remaining slots for multi-contig genomes
            multi += len(hashes) > 1
            picked.append({
                "contig_collection_id": cid,
                "contig_collection_hash": row["hash"],
                "contig_hashes": sorted(hashes),
            })
    if len(picked) < TARGET:
        print(f"only found {len(picked)} genomes")
        return 3
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"genomes": picked}, indent=1) + "\n")
    print(f"wrote {out} ({len(picked)} genomes, {multi} multi-contig)")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT))
