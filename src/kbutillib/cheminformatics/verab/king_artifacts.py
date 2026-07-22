"""KING coscientist artifact emission for the verAB O-demethylation workflow.

``emit_king_workflow`` writes a self-describing, reproducible run directory
that a KING coscientist session (or any human collaborator) can use to
reproduce or extend the verAB methoxy-aromatic Pickaxe rule-discovery
analysis.

Layout of the emitted directory::

    <outdir>/
        seeds.tsv              # id<TAB>smiles (Pickaxe load_compound_set input)
        seeds.csv              # id,smiles (mirror for Pickaxe CSV loader)
        discovered_rules.tsv   # operator<TAB>ec_hint<TAB>confidence<TAB>method<TAB>reaction_id
        target_transformation.txt  # VERAB_ODEMETHYLATION_SMARTS + human description
        prompt.md              # KING coscientist prompt (kbu verab discover workflow)
        manifest.json          # {tool, version, rule_set, generations, seeds,
                               #  operators, inputs, created, git_sha}

Design notes
------------
* **No top-level RDKit or minedatabase import.** This module is stdlib-only.
* Content is deterministic: dict keys are sorted, compound/operator lists are
  sorted alphabetically before writing.
* ``git_sha`` is obtained with a best-effort ``subprocess`` call; it defaults to
  ``"unknown"`` if git is unavailable or the repo has no commits.

Reuses the ``king_app/skill.md`` voice conventions:
* States the goal plainly.
* Gives the exact ``kbu verab discover`` command.
* Names the input files.
* Describes the expected output.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .smarts import SEED_COMPOUNDS, VERAB_ODEMETHYLATION_SMARTS

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_TOOL_NAME = "kbu verab"
_TOOL_VERSION = "0.1.0"

# Compound names (friendly display order) — matches SEED_COMPOUNDS order
_SEED_NAMES: List[str] = [s["name"] for s in SEED_COMPOUNDS]


def _best_effort_git_sha() -> str:
    """Return the current HEAD SHA (short) or ``"unknown"`` on any failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


def _discovery_to_rule_rows(discovery: Any) -> List[Dict[str, str]]:
    """Convert VerabDiscoveryResult.matches into sorted TSV-ready dicts."""
    rows: List[Dict[str, str]] = []
    for m in discovery.matches:
        # Additive: include pipe-joined operators list last; keep existing columns intact.
        op_list: list = list(getattr(m, "operators", None) or [])
        rows.append(
            {
                "operator": m.operator,
                "ec_hint": m.ec_hint or "",
                "confidence": str(m.confidence),
                "method": m.method,
                "reaction_id": m.reaction_id,
                "backend": m.backend,
                "operators": "|".join(op_list),
            }
        )
    # Deterministic order: sort by operator, then reaction_id
    rows.sort(key=lambda r: (r["operator"], r["reaction_id"]))
    return rows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def emit_king_workflow(
    outdir: Any,
    discovery: Any,
    seeds: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Write a reproducible KING coscientist input directory for the verAB workflow.

    Parameters
    ----------
    outdir:
        Destination directory (``str`` or :class:`pathlib.Path`).  Created if
        it does not exist.
    discovery:
        A :class:`~kbutillib.cheminformatics.verab.models.VerabDiscoveryResult`
        (or any object with ``.rule_set``, ``.generations``, ``.seeds``,
        ``.matches``, ``.operators``, ``.warnings`` attributes).
    seeds:
        Seed compound rows to write to ``seeds.tsv`` / ``seeds.csv``.  Each
        entry must have at least ``"id"`` and ``"smiles"`` keys.  Defaults to
        :data:`~kbutillib.cheminformatics.verab.smarts.SEED_COMPOUNDS` when
        ``None``.

    Returns
    -------
    dict
        A summary with keys:

        ``outdir``
            Absolute path of the emitted directory.
        ``files``
            Mapping of artifact name → absolute path.
        ``n_operators``
            Number of unique firing operators written.
        ``n_seeds``
            Number of seed rows written.
    """
    if seeds is None:
        seeds = SEED_COMPOUNDS

    seeds = list(seeds)
    out = Path(outdir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    written: Dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # 1. seeds.tsv
    # ------------------------------------------------------------------ #
    seeds_tsv = out / "seeds.tsv"
    lines = ["id\tsmiles"]
    for s in seeds:
        lines.append(f"{s['id']}\t{s['smiles']}")
    seeds_tsv.write_text("\n".join(lines) + "\n", encoding="utf-8")
    written["seeds.tsv"] = str(seeds_tsv)

    # ------------------------------------------------------------------ #
    # 2. seeds.csv
    # ------------------------------------------------------------------ #
    seeds_csv = out / "seeds.csv"
    lines_csv = ["id,smiles"]
    for s in seeds:
        # Escape SMILES that contain commas (rare but possible)
        smiles = s["smiles"].replace('"', '""')
        lines_csv.append(f'{s["id"]},"{smiles}"')
    seeds_csv.write_text("\n".join(lines_csv) + "\n", encoding="utf-8")
    written["seeds.csv"] = str(seeds_csv)

    # ------------------------------------------------------------------ #
    # 3. discovered_rules.tsv
    # ------------------------------------------------------------------ #
    rules_tsv = out / "discovered_rules.tsv"
    rule_rows = _discovery_to_rule_rows(discovery)
    tsv_headers = ["operator", "ec_hint", "confidence", "method", "reaction_id", "backend", "operators"]
    rule_lines = ["\t".join(tsv_headers)]
    for row in rule_rows:
        rule_lines.append("\t".join(row[h] for h in tsv_headers))
    rules_tsv.write_text("\n".join(rule_lines) + "\n", encoding="utf-8")
    written["discovered_rules.tsv"] = str(rules_tsv)

    # ------------------------------------------------------------------ #
    # 4. target_transformation.txt
    # ------------------------------------------------------------------ #
    transform_txt = out / "target_transformation.txt"
    transform_content = (
        "# verAB O-demethylation target transformation\n"
        "#\n"
        "# Enzyme:  vanillate/isovanillate monooxygenase (EC 1.14.13.82)\n"
        "# Reaction class: aryl methyl ether O-demethylation\n"
        "# Overall chemistry:\n"
        "#   Ar-OCH3 + O2 + NADH  ->  Ar-OH + HCHO + NAD+\n"
        "#\n"
        f"# Reaction SMARTS (VERAB_ODEMETHYLATION_SMARTS):\n"
        f"{VERAB_ODEMETHYLATION_SMARTS}\n"
        "#\n"
        "# Human description:\n"
        "# The aromatic methoxy group ([c]-O-CH3) on the reactant is cleaved.\n"
        "# The oxygen-linked methyl is released as formaldehyde (HCHO / C=O),\n"
        "# and the aromatic ring acquires a free hydroxyl group (-OH), yielding\n"
        "# the corresponding phenol.  In the canonical verAB system, the two\n"
        "# subunits VanA (oxygenase) and VanB (reductase) together catalyze\n"
        "# the NADH-dependent, O2-consuming demethylation of vanillate to\n"
        "# protocatechuate and of isovanillate to 3,4-dihydroxybenzoate.\n"
        "#\n"
        "# Key identifiers:\n"
        "#   EC:        1.14.13.82\n"
        "#   MetaCyc:   VANILLATE-MONOOX-RXN\n"
        "#   Reference: Pate 2026, JCIM doi:10.1021/acs.jcim.6c01595\n"
    )
    transform_txt.write_text(transform_content, encoding="utf-8")
    written["target_transformation.txt"] = str(transform_txt)

    # ------------------------------------------------------------------ #
    # 5. prompt.md   (reproducible inputs + ready-to-run prompt)
    # ------------------------------------------------------------------ #
    prompt_md = out / "prompt.md"
    compound_bullet_lines = "\n".join(
        f"- **{s['name']}** (`{s['id']}`): `{s['smiles']}`" for s in seeds
    )
    operator_lines = (
        "\n".join(f"- `{op}`" for op in sorted(discovery.operators))
        if discovery.operators
        else "*(none found — run `kbu verab discover` to populate)*"
    )
    # Derive the rule set actually used (may differ from the user-facing label
    # when a graceful fallback fired, e.g. "mechinformed" → "metacyc_intermediate").
    # discover_verab_rules already stores actual_rule_set in both discovery.rule_set
    # and discovery.expansion_summary["rule_set_used"]; we prefer the expansion_summary
    # key (explicit), falling back to discovery.rule_set for backward compat with
    # hand-built VerabDiscoveryResult objects in tests.
    _exp_summary = getattr(discovery, "expansion_summary", None)
    rule_set_used = (
        _exp_summary.get("rule_set_used", discovery.rule_set)
        if isinstance(_exp_summary, dict)
        else discovery.rule_set
    )
    prompt_content = (
        "# verAB O-Demethylation Rule Discovery — Reproducible Inputs + Ready-to-Run Prompt\n\n"
        "You have a `kbu` CLI on PATH.  The `kbu verab` verb group implements\n"
        "verAB methoxy-aromatic Pickaxe rule discovery and genome screening.  This\n"
        "directory was generated by `kbu verab emit-king` and contains all inputs\n"
        "needed to reproduce or extend the analysis.\n\n"
        "**Note:** This directory contains *reproducible-run inputs* (seeds, rules,\n"
        "manifest) and a *ready-to-run prompt* — it is NOT itself a KING bundle.\n"
        "To install the kbu verab KING bundle, see `kbu king install`.\n\n"
        "---\n\n"
        "## Goal\n\n"
        "Reproduce the verAB O-demethylation rule discovery for the five canonical\n"
        "seed compounds listed in `seeds.tsv` and identify the Pickaxe rule\n"
        "operator(s) that fire on the target transformation:\n\n"
        "```\n"
        f"{VERAB_ODEMETHYLATION_SMARTS}\n"
        "```\n\n"
        "(Full biochemical description: `target_transformation.txt`)\n\n"
        "---\n\n"
        "## Seed compounds\n\n"
        f"{compound_bullet_lines}\n\n"
        "The five compounds cover the canonical verAB substrates:\n"
        "**vanillate**, **isovanillate**, **guaiacol**, **4-methoxybenzoate**,\n"
        "and **veratrate**.  All five are methoxy-aromatic O-demethylation\n"
        "substrates for the EC 1.14.13.82 enzyme family.\n\n"
        "---\n\n"
        "## How to reproduce\n\n"
        "Run the following command to re-execute rule discovery from scratch:\n\n"
        "```\n"
        "kbu verab discover --json\n"
        "```\n\n"
        "The default rule set is `mechinformed` (Pate 2026 mechanism-informed\n"
        "operators, stefanpate/coarse-grain-rxns).  When the TSV is not found\n"
        "locally, `discover` falls back to the bundled `metacyc_intermediate`\n"
        "set automatically and logs a warning.\n\n"
        "To specify a rule set explicitly:\n"
        "```\n"
        "kbu verab discover \\\n"
        "    --rule-set mechinformed \\\n"
        "    --generations 1 \\\n"
        "    --json\n"
        "```\n\n"
        f"This run used rule set: `{rule_set_used}`\n\n"
        "The command reads the built-in seed list (`kbu verab discover` defaults\n"
        "to the five compounds above) and outputs a JSON object containing\n"
        "`operators` (the firing rule ids), `matches`, and `warnings`.\n\n"
        "To emit a fresh artifact directory:\n"
        "```\n"
        "kbu verab emit-king --outdir ./king_run_verab --json\n"
        "```\n\n"
        "---\n\n"
        "## Expected output\n\n"
        "The discovery run should find at least one Pickaxe rule operator that\n"
        "realises the Ar-OCH3 → Ar-OH + HCHO transformation.  Previously\n"
        "discovered operator(s) (this run):\n\n"
        f"{operator_lines}\n\n"
        "These operators are also listed in `discovered_rules.tsv`.\n\n"
        "---\n\n"
        "## Decision rule: verified verb vs. exec\n\n"
        "- Use `kbu verab discover --json` when you want to re-run the full\n"
        "  five-seed expansion and get the firing operator id(s).\n"
        "- Use `kbu verab enumerate --json` to scan the biochem DB for\n"
        "  additional methoxy-aromatic substrates beyond the five seeds.\n"
        "- Use `kbu verab screen --json` for Phase-2 cross-referencing\n"
        "  (reaction-in-DB, product-in-DB, genome degradation prediction).\n"
        "- If you need something the verbs don't cover, write a script and run\n"
        "  it via `kbu model exec` or a direct Python invocation — the\n"
        "  `kbutillib.cheminformatics.verab` API is fully accessible.\n\n"
        "---\n\n"
        "## Input files in this directory\n\n"
        "| File | Purpose |\n"
        "|------|---------|\n"
        "| `seeds.tsv` | `id<TAB>smiles` — Pickaxe `load_compound_set` format |\n"
        "| `seeds.csv` | `id,smiles` — alternative CSV loader format |\n"
        "| `discovered_rules.tsv` | Operator ids with ec_hint, confidence, method |\n"
        "| `target_transformation.txt` | Reaction SMARTS + biochemical description |\n"
        "| `manifest.json` | Run provenance (version, rule_set_used, git SHA, timestamps) |\n\n"
        "---\n\n"
        "## Boundaries\n\n"
        "This skill covers `kbu verab` only.  For metabolic reconstruction,\n"
        "gapfilling, FBA, or FVA use `kbu model`.  For thermodynamics use\n"
        "`kbu thermo`.  These verbs are local-only and require no KBase token\n"
        "or network connection unless you pass a KBase workspace reference.\n"
    )
    prompt_md.write_text(prompt_content, encoding="utf-8")
    written["prompt.md"] = str(prompt_md)

    # ------------------------------------------------------------------ #
    # 6. manifest.json
    # ------------------------------------------------------------------ #
    manifest_json = out / "manifest.json"
    # rule_set_used was already computed above for prompt.md; reuse it here.
    manifest: Dict[str, Any] = {
        "tool": _TOOL_NAME,
        "version": _TOOL_VERSION,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "rule_set": discovery.rule_set,
        "rule_set_used": rule_set_used,
        "generations": discovery.generations,
        "seeds": sorted(
            [{"id": s["id"], "name": s.get("name", ""), "smiles": s["smiles"]} for s in seeds],
            key=lambda x: x["id"],
        ),
        "operators": sorted(discovery.operators),
        "inputs": {
            "seeds_tsv": "seeds.tsv",
            "seeds_csv": "seeds.csv",
            "rules_tsv": "discovered_rules.tsv",
            "target_transformation": "target_transformation.txt",
            "prompt": "prompt.md",
        },
        "created": datetime.now(tz=timezone.utc).isoformat(),
        "git_sha": _best_effort_git_sha(),
        "warnings": list(discovery.warnings),
    }
    manifest_json.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    written["manifest.json"] = str(manifest_json)

    return {
        "outdir": str(out),
        "files": written,
        "n_operators": len(discovery.operators),
        "n_seeds": len(seeds),
    }
