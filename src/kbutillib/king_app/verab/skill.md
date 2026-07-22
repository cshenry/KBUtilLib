# KBUtilLib verAB O-Demethylation (kbu verab)

You have a `kbu` CLI on PATH.  Its `kbu verab` verb group gives you the full
five-step verAB pipeline: seed discovery → O-demethylation rule identification
→ methoxy-aromatic compound enumeration → product screening → KING coscientist
artifact emission.  This text is injected into your orientation because your
session has no `Skill` tool — treat it as your onboarding for this capability.

`kbu verab` requires no extra setup for basic rule discovery: the standard `kbu`
interpreter already has kbutillib importable and the five canonical verAB seed
compounds are embedded in the package.  RDKit and minedatabase (MINE-Database /
Pickaxe) are **optional**: the pipeline degrades gracefully without them (text-
only operator matching at confidence 0.5) and reports a clear warning rather
than crashing.

## The canonical arc

```
seeds (built-in) → discover → enumerate → screen → emit-king
```

Run the stages in that order.  Each verb supports `--json` for stable,
parseable output — always pass `--json` when you intend to consume the result
programmatically rather than read it as a human summary.

```
kbu verab discover  [--generations N] [--rule-set RULE_SET] [--json]
kbu verab enumerate [--limit N] [--json]
kbu verab screen    [--operators OP,OP,...] [--generations N] [--json]
kbu verab emit-king [--outdir PATH] [--json]
```

---

### `discover` — expand seeds and identify O-demethylation operators

Expands the 5 canonical verAB seed compounds (vanillate, isovanillate, guaiacol,
4-methoxybenzoate, veratrate) via Pickaxe and identifies rule operators that
catalyse aromatic O-demethylation (EC 1.14.13.82).

- **Requires** a configured `network_expansion` backend (Pickaxe / minedatabase)
  for live expansion.  Without it the verb reports a backend-unavailable error.
- **RDKit** improves match confidence from 0.5 (text-only SMARTS match) to 1.0
  (substructure-confirmed transform).  Absent RDKit is reported as a warning,
  not a crash.
- `--generations` (default 1): number of Pickaxe expansion generations.
- `--rule-set` (default `mechinformed`): Pickaxe rule-set identifier.
  The `mechinformed` set uses Pate et al. 2026 mechanism-informed operators
  (stefanpate/coarse-grain-rxns).  If the TSV is not found locally, it
  automatically falls back to the bundled `metacyc_intermediate` set and
  logs a clear warning explaining how to obtain the Pate TSV.
- Exit 0 if ≥1 operator was found; exit 1 if no verAB match was identified.

```
kbu verab discover --generations 1 --rule-set mechinformed --json
```

`--json` emits:
```json
{
  "rule_set": "mechinformed",
  "generations": 1,
  "operators": ["ruleXXXX", ...],
  "matches": [...],
  "warnings": [],
  "expansion_summary": {"n_compounds": 42, "n_reactions": 17}
}
```

---

### `enumerate` — scan biochem DB for methoxy-aromatic compounds

Scans the ModelSEED biochemistry database for all compounds that contain a
methoxy-aromatic substructure using SMARTS-based substructure matching.

- **Requires** RDKit (substructure match) and a configured `biochem` dependency.
  Raises a clear error message if either is unavailable.
- `--limit N`: stop after N hits (default: full DB scan).

```
kbu verab enumerate --limit 100 --json
```

`--json` emits:
```json
{"n_compounds": 87, "compounds": [{"id": "cpd00156", "name": "...", "smiles": "..."}, ...]}
```

---

### `screen` — cross-reference predicted products with the four Phase-2 questions

For each methoxy-aromatic product predicted by Pickaxe expansion, answers:

- **(a) Reaction in DB?** — stoichiometry-hash match against `biochem.search_reactions`.
- **(b) Product in DB?** — InChIKey first-block / SMILES match via `biochem.search_compounds`.
- **(c) Downstream pathway?** — reactions consuming the product MSCPD.
- **(d) In metabolic models?** — membership test over `model.model.reactions` mapped
  through `reaction_id_to_msid`.

Optionally cross-references genomes for degradation potential when a genome/
annotation layer is configured.

- `--operators OP,OP,...`: comma-separated operator ids from `discover` (default:
  run expansion from seeds with no operator filter).
- `--generations N` (default 1): expansion generations for screening.

```
kbu verab screen --operators ruleXXXX --json
```

`--json` emits a `ScreeningReport.to_dict()` envelope with `records`, `warnings`,
`n_source_compounds`, and optional `genome_predictions`.

---

### `emit-king` — write a reproducible KING coscientist workflow directory

Writes a self-contained set of artifacts that a KING coscientist session can
consume to reproduce or extend the verAB rule-discovery workflow.  These are
**reproducible-run inputs + a ready-to-run prompt** — not a KING bundle themselves.

| File | Contents |
|------|----------|
| `seeds.tsv` | `id\tsmiles` rows for all 5 canonical seed compounds |
| `seeds.csv`  | Same in CSV format |
| `discovered_rules.tsv` | `operator\tec_hint\tconfidence\tmethod\treaction_id` rows |
| `target_transformation.txt` | VERAB_ODEMETHYLATION_SMARTS + EC reference |
| `prompt.md` | Reproducible-run inputs + ready-to-run prompt for a KING session |
| `manifest.json` | Tool provenance: version, rule_set, rule_set_used, generations, seeds, operators, git_sha |

No prior `discover` run is required; `emit-king` runs the built-in seed list and
reports discovery details inside the artifacts.

- `--outdir PATH` (default `king_verab`): directory to write artifacts (created if absent).

```
kbu verab emit-king --outdir king_verab --json
```

`--json` emits:
```json
{"outdir": "king_verab", "files": {"seeds.tsv": "...", ...}, "n_operators": 1, "n_seeds": 5}
```

---

## Decision rule: which verb to use

| Goal | Verb |
|------|------|
| Identify which Pickaxe operators catalyse verAB O-demethylation | `discover` |
| List all methoxy-aromatic compounds in ModelSEED biochem | `enumerate` |
| Cross-reference products against DB / models / genomes | `screen` |
| Produce a reproducible KING coscientist workflow bundle | `emit-king` |

---

## Scientific background

The **verAB** genes encode a two-component monooxygenase (EC 1.14.13.82) that
catalyses the O-demethylation of methoxy-aromatic compounds (vanillate,
isovanillate, guaiacol, 4-methoxybenzoate, veratrate) in aromatic catabolism
pathways.  The verAB Pickaxe workflow:

1. Expands the 5 canonical methoxy-aromatic seed compounds via Pickaxe
   (minedatabase) using mechanism-informed reaction operators (Pate 2026,
   stefanpate/coarse-grain-rxns) by default, falling back to the bundled
   MetaCyc intermediate set when the Pate TSV is not locally available.
2. Identifies operators whose predicted transformations remove a methoxy
   group from an aromatic ring and produce a phenol product + formaldehyde.
3. Cross-references predicted products with ModelSEED biochemistry and
   genome-scale metabolic models to assess biological relevance.
4. Emits reproducible artifacts for downstream KING coscientist analysis.

The pipeline is implemented as a thin facade over:
- `kbutillib.cheminformatics.verab` — SMARTS constants, data models,
  substructure filter, rule discovery, screening, artifact emission.
- `kbutillib.verab_utils.VerabUtils` — the unified facade accessed via
  `Toolkit().verab`.

---

## Manual KING install

The KING app installer does **not** auto-discover multiple bundle directories.
To register this verAB bundle with a local KING session, run:

```
kbu king install --bundle-dir "$(python -c "import kbutillib, pathlib; \
  print(pathlib.Path(kbutillib.__file__).parent / 'king_app' / 'verab')")"
```

Or, from Python:

```python
from pathlib import Path
import kbutillib
from kbutillib import king_install

bundle_dir = Path(kbutillib.__file__).parent / "king_app" / "verab"
king_install.install(bundle_dir)
```

If `kbu king install` does not yet accept a `--bundle-dir` flag exposing
arbitrary dirs, call `king_install.install(bundle_dir)` directly from a
`kbu model exec` script.

---

## API reference (read before writing an exec script)

Before writing a `kbu model exec` script that uses the verAB API directly:

- `src/kbutillib/cheminformatics/verab/` — SMARTS constants (`smarts.py`),
  data models (`models.py`), substructure filter (`substructure.py`),
  rule discovery (`rule_discovery.py`), screening (`screening.py`),
  KING artifact emission (`king_artifacts.py`).
- `src/kbutillib/verab_utils.py` — the `VerabUtils` / `VerabUtilsImpl` facade.
- `src/kbutillib/cli/verab.py` — the CLI thin wrapper; reading it shows
  exactly which facade methods are called per verb.

Resolve the install path programmatically if uncertain:

```
kbu model exec - <<'PY'
import kbutillib, pathlib
print(pathlib.Path(kbutillib.__file__).parent)
PY
```

---

## Boundaries

- This skill covers `kbu verab` only.  It does not describe `kbu model`
  (metabolic modeling), `kbu king` (installer verbs), or other `kbu` verb groups.
- This text is injected via `KING_CONTEXT` only; it is **not** registered as a
  Claude Code skill (no `~/.claude/skills/` entry).
- The verAB verbs are intended **local-only**.  If `kbu king status` warns that
  KING's LLM route is non-local, treat any genome/model data you handle as
  something you should not casually paste into that route.
- RDKit and minedatabase are **not** hard requirements; missing either degrades
  gracefully with a warning rather than crashing.
- The Pate 2026 mechanism-informed TSV (stefanpate/coarse-grain-rxns) carries
  no license and is **not redistributed** by KBUtilLib; it is resolved at
  runtime from a local clone.  When absent, `discover` falls back to the
  bundled `metacyc_intermediate` set automatically.
