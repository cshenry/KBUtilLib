#!/usr/bin/env python
"""Re-derive the NCBI contig check stored in cdm_contig_ncbi_fixture.json.

Downloads NZ_CP010888.1 from NCBI efetch, hashes it with the v2.0 contig
rule (``entity_hash("gene", ...)``), and compares against the stored sha256
and length (and, with --berdl, against kbase.genomes.contig.hash for CDM
contig CDM:cntg-96f59e30-94c6-44d9-8aa2-cd4e7bc6f63f). Needs network; the
test-suite never runs this.

    python scripts/identity/check_ncbi_contig_hash.py [--berdl]
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

from kbutillib.domains.identity.standardizers import (
    entity_hash,
    parse_fasta_contigs,
)

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "tests" / "domains" / "fixtures" / "cdm_contig_ncbi_fixture.json"
)
URL = (
    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    "?db=nuccore&id={acc}&rettype=fasta&retmode=text"
)


def main() -> int:
    fx = json.loads(FIXTURE.read_text())
    ncbi = fx["ncbi_contig"]
    with urllib.request.urlopen(URL.format(acc=ncbi["accession"]), timeout=120) as r:
        text = r.read().decode("utf-8")
    (contig,) = parse_fasta_contigs(text)
    got = entity_hash("gene", contig)
    length = len("".join(contig.split()))
    ok = got == ncbi["sha256"] and length == ncbi["length"]
    print(f"{ncbi['accession']}: sha256={got} length={length} match_fixture={ok}")
    if "--berdl" in sys.argv:
        from kbutillib import KBBERDLUtils

        res = KBBERDLUtils().query(
            "SELECT hash, length FROM kbase.genomes.contig "
            f"WHERE contig_id = '{ncbi['cdm_contig_id']}'"
        )
        row = res["data"][0]
        berdl_ok = row["hash"] == got and int(row["length"]) == length
        print(f"BERDL hash={row['hash']} length={row['length']} match={berdl_ok}")
        ok = ok and berdl_ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
