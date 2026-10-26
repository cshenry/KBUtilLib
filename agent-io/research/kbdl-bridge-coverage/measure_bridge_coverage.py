#!/usr/bin/env python
"""One-off, READ-ONLY measurement of KBDL -> arc-store bridge coverage.

Run from the KBUtilLib repo root:
    /home/chenry/venvs/kbu-modeling/bin/python \
        agent-io/research/kbdl-bridge-coverage/measure_bridge_coverage.py

Writes report.md next to itself. Writes no arc-store record and calls no
submit_* method. Answers four questions (see report.md).
"""
import argparse
import collections
import gzip
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]  # <repo>/agent-io/research/<dir> -> repo root
sys.path.insert(0, str(ROOT / "src"))

import kbutillib  # noqa: E402
from kbutillib.koros_arc_store import KorosArcStore  # noqa: E402
from kbutillib.domains.external.kbdl_service_utils import KBDLServiceUtils  # noqa: E402

HOME = Path.home()
LEDGER = HOME / ".kbdl" / "sweep_ledger.jsonl"
RESULTS = HOME / ".kbdl" / "job-results"
ap = argparse.ArgumentParser()
ap.add_argument("--token-file", default=str(HOME / ".kbase" / "token"),
                help="file holding the KBase token passed explicitly to KBDLServiceUtils "
                     "(the pod env var is stale and would otherwise win); never printed")
ARGS = ap.parse_args()
HEX32 = re.compile(rb"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")
INDEXABLE = ("KBDLBuildGenome", "KBDLGenomeAnnotation")
out = []


def log(s=""):
    print(s, flush=True)
    out.append(s)


log("# KBDL bridge coverage report")
log()
log(f"Generated {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} on kbhub by "
    "`measure_bridge_coverage.py` (read-only).")
log(f"kbutillib imported from: `{kbutillib.__file__}`")
log()

# ---- ledger + result files -------------------------------------------------
ledger = {}  # job_id -> (job_type, outcome)
ledger_lines = 0
by_type_entries = collections.Counter()
for line in open(LEDGER):
    line = line.strip()
    if not line:
        continue
    ledger_lines += 1
    r = json.loads(line)
    ledger[r["job_id"]] = (r["job_type"], r.get("outcome"))
    by_type_entries[r["job_type"]] += 1

files = {}  # job_type -> {job_id}
for d in sorted(RESULTS.iterdir()):
    if d.is_dir():
        files[d.name] = {p.name[: -len(".json.gz")] for p in d.glob("*.json.gz")}

log("## Populations")
log()
log(f"Ledger: {ledger_lines} lines, {len(ledger)} distinct job ids.")
log()
log("| job type | ledger entries (distinct ids) | result files on disk | union of ids |")
log("|---|---|---|---|")
ledger_ids = collections.defaultdict(set)
for j, (t, _o) in ledger.items():
    ledger_ids[t].add(j)
union_ids = {}
for t in sorted(set(ledger_ids) | set(files)):
    u = ledger_ids.get(t, set()) | files.get(t, set())
    union_ids[t] = u
    log(f"| {t} | {len(ledger_ids.get(t, ()))} | {len(files.get(t, ()))} | {len(u)} |")
log()

# ---- Q1 / Q2: object store ------------------------------------------------
log("## Q1 COVERAGE (annotation locus prefix -> object store)")
log()
t0 = time.time()
objs = None
list_error = None
try:
    objs = KBDLServiceUtils(token=Path(ARGS.token_file).read_text().strip()).list_objects()
except Exception as e:  # noqa: BLE001
    list_error = f"{type(e).__name__}: {e}"

ann_dir = RESULTS / "KBDLGenomeAnnotation"
file_prefix = {}  # job_id -> locus prefix (first key[:16])
bad = []
inconsistent = 0
for p in sorted(ann_dir.glob("*.json.gz")):
    try:
        with gzip.open(p, "rt") as fh:
            data = json.load(fh)
    except Exception as e:  # noqa: BLE001
        bad.append((p.name, repr(e)))
        continue
    keys = list(data) if isinstance(data, dict) else []
    if not keys:
        bad.append((p.name, "empty or non-dict result"))
        continue
    if len({k[:16] for k in keys}) != 1:
        inconsistent += 1
    file_prefix[p.name[: -len(".json.gz")]] = keys[0][:16]
total_files = len(list(ann_dir.glob("*.json.gz")))
examined = len(file_prefix)
log(f"Annotation result files on disk: {total_files}; examined (parsed, non-empty): {examined}; "
    f"unreadable/empty: {len(bad)}; files whose keys carry >1 distinct 16-char prefix: {inconsistent}; "
    f"distinct locus prefixes among examined files: {len(set(file_prefix.values()))}.")
log()
pref = collections.defaultdict(set)
if objs is None:
    log(f"**BLOCKED: `KBDLServiceUtils(token=<file>).list_objects()` failed after {time.time()-t0:.1f}s: `{list_error}`.**")
    log("The KBase token at `~/.kbase/token` (mtime 2026-10-23) is rejected by the KBase auth service. "
        "Q1 and Q2 therefore have NO object-store numbers; rerun the script with a valid token "
        "(no other change needed).")
    log()
    log("## Q2 COLLISION")
    log()
    log("**BLOCKED** for the same reason (needs the full object listing).")
    log()
else:
    log(f"`list_objects()` returned {len(objs)} objects in {time.time()-t0:.1f}s.")
    oid_key = None
    for k in ("object_id", "id", "sha256"):
        if objs and k in objs[0]:
            oid_key = k
            break
    assert oid_key, f"no object id key in {objs[:1]}"
    log(f"Object id field: `{oid_key}`. Example record keys: {sorted(objs[0])}.")
    for o in objs:
        pref[o[oid_key][:16]].add(o[oid_key])
    log()
    exactly1 = multi = none = 0
    for pf in file_prefix.values():
        n = len(pref.get(pf, ()))
        if n == 1:
            exactly1 += 1
        elif n > 1:
            multi += 1
        else:
            none += 1
    log("| resolution | count |")
    log("|---|---|")
    log(f"| resolved to exactly one object | {exactly1} |")
    log(f"| resolved to more than one object (collision) | {multi} |")
    log(f"| resolved to none | {none} |")
    log(f"| **sum** | **{exactly1+multi+none}** |")
    log()
    if bad:
        log("Unexamined files: " + "; ".join(f"{n} ({e})" for n, e in bad[:10]))
        log()
    log(f"Fraction resolved to exactly one: {exactly1}/{examined} = {exactly1/max(examined,1):.4f}.")
    log()
    log("## Q2 COLLISION (16-hex prefixes shared by >=2 object ids)")
    log()
    coll = {k: v for k, v in pref.items() if len(v) > 1}
    log(f"Distinct object ids: {sum(len(v) for v in pref.values())}; distinct 16-char prefixes: {len(pref)}.")
    log(f"**Colliding 16-hex prefixes: {len(coll)}**")
    for k, v in list(coll.items())[:10]:
        log(f"- `{k}`: {sorted(v)}")
    log()

# ---- Q3: overlap with arc store -------------------------------------------
log("## Q3 OVERLAP (existing arc-store rows keyed on an indexable KBDL job id)")
log()
store = KorosArcStore(runs_root=os.environ.get("KOROS_HOME_RUNS") or (HOME / "Dropbox" / "Science" / "runs"))
runs_root = store.runs_root
log(f"Runs root: `{runs_root}`; run database: `{store.db.db_path}`.")
indexable_ledger = {j for j, (t, _o) in ledger.items() if t in INDEXABLE}
indexable_union = set().union(*(union_ids.get(t, set()) for t in INDEXABLE))
rows = []
seen = set()
pairs = [(None, None)]
for proj in store.list_projects():
    for arc in store.list_arcs(proj.name):
        pairs.append((proj.name, arc.slug))
for proj, arc in pairs:
    for rec in store.list_analyses(proj, arc):
        if rec.record_id not in seen:
            seen.add(rec.record_id)
            rows.append(rec)
# cross-check total row count with a read-only connection (no write, no schema init)
con = sqlite3.connect(f"file:{store.db.db_path}?mode=ro", uri=True)
db_total = con.execute("SELECT count(*) FROM runs").fetchone()[0]
con.close()
log(f"Rows in `runs` table (read-only count): {db_total}; rows enumerated via "
    f"`KorosArcStore.list_analyses` over {len(pairs)} (project, arc) pairs incl. unattributed: {len(rows)}.")
ov_ledger = [r for r in rows if r.run_uid in indexable_ledger]
ov_union = [r for r in rows if r.run_uid in indexable_union]
log()
log(f"**Overlap (run_uid in ledger with job_type BuildGenome/GenomeAnnotation): {len(ov_ledger)}**")
log(f"Overlap against the wider ledger-union-result-file population: {len(ov_union)}.")
log(f"Rows whose run_uid is ANY ledger job id: {sum(1 for r in rows if r.run_uid in ledger)}.")
log()
log("Wider-population overlap by producer: " + json.dumps(dict(collections.Counter(r.producer for r in ov_union))))
log("Wider-population overlap by kind: " + json.dumps(dict(collections.Counter(r.kind for r in ov_union))))
log("Overlap by kind: " + json.dumps(dict(collections.Counter(r.kind for r in ov_ledger))))
log("Overlap by producer: " + json.dumps(dict(collections.Counter(r.producer for r in ov_ledger))))
log("Overlap by (project, arc): " + json.dumps(
    {f"{k[0]}/{k[1]}": v for k, v in collections.Counter((r.project, r.arc) for r in ov_ledger).items()}))
log()
log("### Example rows (up to 5): fields a mechanical indexer could not reconstruct")
log()
for r in ov_ledger[:5]:
    jt = ledger[r.run_uid][0]
    log(f"- run_uid `{r.run_uid}` ({jt}); project/arc `{r.project}/{r.arc}`; kind `{r.kind}`; "
        f"producer `{r.producer}` {r.producer_version}; trust_tier `{r.trust_tier}`; status `{r.status}`; "
        f"subject `{r.subject}`")
    log(f"  - provenance: `{json.dumps(r.provenance, sort_keys=True)[:700]}`")
    log(f"  - payload keys: `{sorted(r.payload) if r.payload else None}`; artifacts: `{json.dumps(r.artifacts)[:300]}`")
log()

# ---- Q4: attribution coverage ---------------------------------------------
log("## Q4 ATTRIBUTION COVERAGE (job ids mentioned in arcs of the runs tree)")
log()
all_ids = set(ledger) | set().union(*union_ids.values())
mentions = collections.defaultdict(set)  # job_id -> {(project, arc)}
nfiles = nbytes = nbig = nerr = outside = 0
t0 = time.time()
for dirpath, dirnames, filenames in os.walk(runs_root, followlinks=False):
    rel = Path(dirpath).relative_to(runs_root).parts
    for fn in filenames:
        fp = os.path.join(dirpath, fn)
        if os.path.islink(fp) or not os.path.isfile(fp):
            continue
        # arc membership: <project>/arcs/<slug>/...
        if len(rel) >= 3 and rel[1] == "arcs":
            arc = (rel[0], rel[2])
        else:
            arc = None
        try:
            with open(fp, "rb") as fh:
                data = fh.read()
        except OSError:
            nerr += 1
            continue
        nfiles += 1
        nbytes += len(data)
        if arc is None:
            outside += 1
            continue
        for m in set(HEX32.findall(data)):
            s = m.decode()
            if s in all_ids:
                mentions[s].add(arc)
log(f"Scanned {nfiles} regular files ({nbytes/1e9:.2f} GB) in {time.time()-t0:.0f}s; "
    f"{outside} of them lie outside `<project>/arcs/<slug>/` and are not attributed to any arc; "
    f"{nerr} unreadable.")
log()


def table(title, pop):
    log(f"### {title}")
    log()
    log("| job type | jobs in population | in exactly 1 arc | in >1 arc | in no arc |")
    log("|---|---|---|---|---|")
    for t in sorted(pop):
        ids = pop[t]
        one = sum(1 for j in ids if len(mentions.get(j, ())) == 1)
        more = sum(1 for j in ids if len(mentions.get(j, ())) > 1)
        zero = len(ids) - one - more
        log(f"| {t} | {len(ids)} | {one} | {more} | {zero} |")
    log()


table("Population A: ledger job ids only (distinct ids per ledger job_type)", dict(ledger_ids))
table("Population B: union of ledger ids and result-file stems", union_ids)
log("Hand-measurement to confirm or correct: 70/835 GenomeAnnotation and 49/4654 BuildGenome "
    "ledger jobs mentioned, zero multi-arc. Compare against Population A rows above "
    "(mentioned = exactly 1 + more than 1).")
log()
(HERE / "report.md").write_text("\n".join(out) + "\n")
