"""Live h100 end-to-end validation of DRAM2Utils id-remap fix.

Acceptance Criterion 16 of
KBUtilLib/agent-io/prds/dram2-input-genes-id-remap/fullprompt.md.

Loads b-prefixed Keio proteins from the GAA store on h100, configures
DRAM2Utils with the real on-disk DRAM2 install, runs annotate(..., databases=['kofam'])
under KBU_DRAM2_LIVE=1, and asserts:
  - the pipeline completes COMBINE_ANNOTATIONS without the
    'ValueError: invalid literal for int() with base 10: b0001' crash,
  - the returned AnnotationRecords carry b0001-style gene_ids.

Writes outputs to OUTPUT_DIR for the work-record:
  - command.txt (the nextflow command line),
  - run_summary.json (counts, parameters, kept dir, version, error if any),
  - sample_records.json (first 20 records),
  - all_records.tsv (all returned records, tab-separated),
  - protein_subset.json (the {locus_id: protein_seq} sent in),
  - gene_coords.json (the {locus_id: (start, stop, strand)} sent in).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

import duckdb

# Force live mode
os.environ["KBU_DRAM2_LIVE"] = "1"

# DRAM2 needs its NFS-resident temp/conda/nextflow homes, NOT local /tmp
# (/tmp is only 8GB on this host).  Match dram2-env.sh:
_DRAM2_ROOT = "/scratch1/fliu/hub_scratch/chenry/DRAM2"
_NXF_TMPDIR = "/scratch1/fliu/hub_scratch/chenry/dram2-id-remap-live-validation/tmp"
os.makedirs(_NXF_TMPDIR, exist_ok=True)
os.environ.setdefault("CONDA_ENVS_PATH", f"{_DRAM2_ROOT}/env")
os.environ.setdefault("CONDA_PKGS_DIRS", os.path.expanduser("~/.dram2_conda_pkgs"))
os.environ.setdefault("NXF_CONDA_CACHEDIR", f"{_DRAM2_ROOT}/nxf_conda")
os.environ.setdefault("MAMBA_ROOT_PREFIX", f"{_DRAM2_ROOT}/micromamba")
os.environ.setdefault("DRAM2_DB_DIR", f"{_DRAM2_ROOT}/databases")
os.environ.setdefault("NXF_HOME", f"{_DRAM2_ROOT}/nextflow")
os.environ["TMPDIR"] = _NXF_TMPDIR
os.environ["TMP"] = _NXF_TMPDIR
os.environ["TEMP"] = _NXF_TMPDIR

from kbutillib import dram2_utils  # noqa: E402
from kbutillib.dram2_utils import DRAM2Utils  # noqa: E402


# ---------- 1. Source the Keio proteins from the GAA store ----------

GAA_STORE = "/home/chenry/.local/share/gaa_store_h100"
KEIO_GENOME_ID = "fac9fa4e-530a-4406-8a3b-9349ead11f6b"
GENES_PARQUET = f"{GAA_STORE}/genes/*.parquet"
SEQS_PARQUET = f"{GAA_STORE}/sequences/*.parquet"


def load_keio_proteins(limit: int | None = None) -> tuple[dict[str, str], dict[str, tuple[int, int, int]]]:
    """Return ({locus_id: protein_seq}, {locus_id: (start, stop, strand)}) for b-prefixed loci."""
    # No length filter — include b0001 (21 aa) so the literal b0001 case from the
    # PRD's bug report is exercised end-to-end.
    q = f"""
      SELECT g.locus_id, s.sequence, g."start", g."end", g.strand
      FROM read_parquet('{GENES_PARQUET}') g
      JOIN read_parquet('{SEQS_PARQUET}') s ON g.protein_seq_hash = s.seq_hash
      WHERE g.genome_id='{KEIO_GENOME_ID}'
        AND s.seq_type='protein'
        AND g.locus_id LIKE 'b%'
        AND LENGTH(s.sequence) >= 5
      ORDER BY g.locus_id
    """
    if limit is not None:
        q += f" LIMIT {limit}"
    rows = duckdb.sql(q).fetchall()
    proteins: dict[str, str] = {}
    coords: dict[str, tuple[int, int, int]] = {}
    for locus_id, seq, start, end, strand_raw in rows:
        proteins[locus_id] = seq
        strand_num = 1 if strand_raw in ("+", 1, "1") else -1
        coords[locus_id] = (int(start), int(end), strand_num)
    return proteins, coords


# ---------- 2. Build the DRAM2 config dict ----------

DRAM2_ROOT = _DRAM2_ROOT


def build_config(work_root: str, keep_work: bool = True) -> dict:
    return {
        "dram2": {
            "launch_dir": DRAM2_ROOT,
            "pipeline": f"{DRAM2_ROOT}/repo/main.nf",
            "nextflow": f"{DRAM2_ROOT}/bin/nextflow-native",
            "nxf_ver": "24.10.5",
            # env_path: env_nf/bin gives java + nextflow; DRAM2/bin gives micromamba.
            "env_path": f"{DRAM2_ROOT}/env/env_nf/bin:{DRAM2_ROOT}/bin",
            "profile": "conda",
            # dram2.config: forces conda envs to build with micromamba on this host.
            "config": f"{DRAM2_ROOT}/dram2.config",
            "work_root": work_root,
            "keep_work": keep_work,
        }
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50,
                    help="Number of b-prefixed Keio proteins to send "
                         "(default: 50; pass 0 for ALL ~4493)")
    ap.add_argument("--out", type=str, required=True,
                    help="Output directory for run artifacts")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--databases", type=str, default="kofam",
                    help="Comma-separated DRAM2 dbs")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    limit = args.limit if args.limit > 0 else None
    print(f"[setup] Loading Keio b-prefixed proteins (limit={limit}) ...", flush=True)
    proteins, coords = load_keio_proteins(limit=limit)
    print(f"[setup] Loaded {len(proteins)} proteins. First 3 ids: {list(proteins)[:3]}", flush=True)
    assert all(pid.startswith("b") for pid in proteins), "non-b ids leaked in"

    # Persist what we sent
    (out_dir / "protein_subset.json").write_text(json.dumps(proteins, indent=2))
    (out_dir / "gene_coords.json").write_text(json.dumps({k: list(v) for k, v in coords.items()}, indent=2))

    work_root = str(out_dir / "scratch")
    Path(work_root).mkdir(parents=True, exist_ok=True)

    cfg = build_config(work_root=work_root, keep_work=True)
    print(f"[setup] DRAM2 config: {json.dumps(cfg, indent=2)}", flush=True)

    print(f"[setup] kbutillib.dram2_utils file: {dram2_utils.__file__}", flush=True)

    utils = DRAM2Utils(
        config=cfg, config_file=False, token_file=None, kbase_token_file=None,
    )
    print(f"[setup] is_available: {utils.is_available()}", flush=True)
    if not utils.is_available():
        print("[fatal] DRAM2 not available — bailing.", file=sys.stderr)
        return 2

    databases = tuple(d.strip() for d in args.databases.split(",") if d.strip())
    print(f"[run] Calling annotate(... databases={databases}, threads={args.threads}) ...", flush=True)

    error_info: dict | None = None
    result = None
    try:
        result = utils.annotate(
            proteins,
            databases=databases,
            gene_coords=coords,
            threads=args.threads,
        )
    except Exception as exc:  # noqa: BLE001 — we want everything
        error_info = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
        print(f"[error] annotate raised: {error_info['type']}: {error_info['message']}", file=sys.stderr, flush=True)

    summary: dict = {
        "n_proteins_sent": len(proteins),
        "first_3_caller_ids": list(proteins)[:3],
        "databases": list(databases),
        "threads": args.threads,
        "config": cfg,
        "kbutillib_path": str(Path(dram2_utils.__file__)),
        "error": error_info,
    }

    if result is not None:
        summary["tool"] = result.tool
        summary["tool_version"] = result.tool_version
        summary["db_version"] = result.db_version
        summary["run_id"] = result.run_id
        summary["command"] = result.command
        summary["parameters"] = result.parameters
        summary["n_records"] = len(result.records)
        summary["unique_gene_ids_in_records"] = len({r.gene_id for r in result.records})
        (out_dir / "command.txt").write_text(result.command + "\n")
        sample = result.records[:20]
        sample_payload = [
            {
                "gene_id": r.gene_id,
                "n_terms": len(r.terms),
                "term_namespaces": sorted({t.namespace for t in r.terms if t.namespace}),
                "first_terms": [
                    {
                        "namespace": t.namespace,
                        "id": t.id,
                        "value": t.value,
                        "evidence": t.evidence,
                    }
                    for t in r.terms[:5]
                ],
            }
            for r in sample
        ]
        (out_dir / "sample_records.json").write_text(json.dumps(sample_payload, indent=2))

        all_lines = ["gene_id\tn_terms\tterm_namespaces\tfirst_term"]
        for r in result.records:
            ns = sorted({t.namespace for t in r.terms if t.namespace})
            first_term = ""
            if r.terms:
                t0 = r.terms[0]
                first_term = f"{t0.namespace}:{t0.id}={t0.value or ''}"
            all_lines.append(f"{r.gene_id}\t{len(r.terms)}\t{','.join(ns)}\t{first_term}")
        (out_dir / "all_records.tsv").write_text("\n".join(all_lines) + "\n")

        b_ids = [r.gene_id for r in result.records if r.gene_id.startswith("b")]
        summary["n_records_with_b_prefixed_gene_id"] = len(b_ids)
        summary["first_5_b_prefixed_gene_ids_in_records"] = b_ids[:5]
        summary["all_gene_ids_are_b_prefixed"] = (
            len(b_ids) == len(result.records) and len(result.records) > 0
        )
    else:
        summary["n_records"] = 0

    # Capture nextflow log if reachable for execution trace evidence
    nxf_log_candidates = [
        Path(DRAM2_ROOT) / ".nextflow.log",
        out_dir / "scratch" / ".nextflow.log",
    ]
    for cand in nxf_log_candidates:
        if cand.is_file():
            try:
                # Copy last 200 lines of the launch-dir log
                tail = cand.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
                (out_dir / "nextflow.log.tail.txt").write_text("\n".join(tail) + "\n")
                break
            except Exception:
                continue

    (out_dir / "run_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(f"[done] wrote summary to {out_dir / 'run_summary.json'}", flush=True)

    if error_info is not None:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
