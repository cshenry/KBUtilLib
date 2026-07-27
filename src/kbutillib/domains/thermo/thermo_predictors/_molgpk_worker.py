#!/usr/bin/env python
"""KBUtilLib worker for the OPAM2 (MolGpKa-on-ModelSEED) pKa / protonation tool.

This script is shipped *inside* the KBUtilLib package (``thermo_predictors/
_molgpk_worker.py``) and is launched by the molGPK backend with the OPAM2 repo
as its working directory and ``OPAM2_SRC`` pointing at that repo, so it can
import OPAM2's prediction modules — whose internal imports are *bare*
(``from utils.descriptor import mol2vec``, ``from predict_pka import
predict_for_protonate``) and therefore require the repo's ``src/`` directory on
``sys.path``. A repo-local copy named ``kbutillib_molgpk_worker.py``, if present,
takes precedence over this bundled one.

Protocol
--------
Reads a single JSON request object from stdin and writes a single JSON response
object to stdout. KBUtilLib never imports torch / torch-geometric / OPAM2 into
its own process; all of that lives behind this subprocess boundary.

The request is EITHER a single compound or a batch. The batch form loads the
(heavy) model exactly once and answers every compound, which is how callers
should annotate a whole compound set — the per-call model load (several seconds)
is paid once instead of once per compound.

Single request (stdin)::

    {
      "smiles": "CC(=O)O",      # required: SMILES or InChI of the compound
      "ph": 7.0,                  # optional, default 7.0
      "tph": 1.0,                 # optional borderline window, default 1.0
      "acid_model": null,         # optional path override
      "base_model": null,         # optional path override
      "microspecies_format": "smiles"   # optional: smiles|inchi
    }

Single response (stdout, success)::

    {
      "ok": true,
      "pka_values": [2.01],            # sorted, all acidic+basic sites
      "acid_pka": {"7": 2.01},          # atom-index -> pKa
      "base_pka": {},
      "major_microspecies": "CC(=O)[O-]",   # dominant protonation state at pH
      "microspecies": ["CC(=O)[O-]"],       # all enumerated states
      "ph": 7.0,
      "model": "modelseed"
    }

Single response (stdout, per-compound failure)::

    {"ok": false, "error": "..."}

Batch request (stdin)::

    {
      "compounds": [ {"smiles": "...", "ph": 7.0, ...}, ... ],
      "ph": 7.0,            # optional batch-wide defaults, overridden per item
      "tph": 1.0,
      "microspecies_format": "smiles"
    }

Batch response (stdout)::

    {"ok": true, "results": [ <single-response object>, ... ]}

Each element of ``results`` is exactly a single-response object (``ok`` true or
false), positionally aligned with the input ``compounds`` list, so one bad
compound never sinks the rest of the batch.

A non-zero exit / empty stdout signals a harder failure (bad interpreter,
missing deps) which the backend reports as unavailable.
"""

import json
import os
import sys


def _fail(msg):
    sys.stdout.write(json.dumps({"ok": False, "error": str(msg)}))
    sys.stdout.flush()
    sys.exit(0)


def _setup_path():
    """Put OPAM2's src/ on sys.path so its bare imports resolve."""
    roots = []
    env_root = os.environ.get("OPAM2_SRC")
    if env_root:
        roots.append(os.path.abspath(env_root))
    here = os.path.abspath(os.path.dirname(__file__))
    roots.extend([here, os.getcwd()])
    for root in roots:
        for cand in (os.path.join(root, "src"), root):
            if os.path.isdir(cand) and cand not in sys.path:
                sys.path.insert(0, cand)


def _predict_one(req, predict_pka, protonate, Chem):
    """Compute pKa + microspecies for one compound; return a response dict.

    Never raises: any per-compound problem is captured as ``{"ok": false}`` so a
    single bad structure cannot abort a batch.
    """
    smiles = req.get("smiles")
    if not smiles:
        return {"ok": False, "error": "no 'smiles' provided"}
    ph = float(req.get("ph", 7.0))
    tph = float(req.get("tph", 1.0))
    acid_model = req.get("acid_model") or None
    base_model = req.get("base_model") or None
    fmt = req.get("microspecies_format", "smiles")

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        mol = Chem.MolFromInchi(smiles)
    if mol is None:
        return {"ok": False, "error": f"could not parse structure: {smiles!r}"}

    try:
        base_dict, acid_dict = predict_pka.predict(
            mol, acid_model=acid_model, base_model=base_model
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"pKa prediction failed: {exc}"}

    acid_pka = {str(k): float(v) for k, v in acid_dict.items()}
    base_pka = {str(k): float(v) for k, v in base_dict.items()}
    pka_values = sorted(list(acid_pka.values()) + list(base_pka.values()))

    microspecies = []
    major = None
    try:
        microspecies = protonate.Opam_protonate_mol(
            smiles,
            ph=ph,
            tph=tph,
            acid_model=acid_model,
            base_model=base_model,
            InChi_SMILES_Mol=("inchi" if fmt == "inchi" else "smiles"),
        )
        # The protonation enumeration lists the all-stable (no borderline
        # ambiguity) state first; that k=0 case is the dominant microspecies.
        if microspecies:
            major = microspecies[0]
    except Exception as exc:  # noqa: BLE001
        # pKa values are still useful even if microspecies enumeration trips.
        microspecies = []
        major = None
        sys.stderr.write(f"microspecies enumeration warning: {exc}\n")

    return {
        "ok": True,
        "pka_values": pka_values,
        "acid_pka": acid_pka,
        "base_pka": base_pka,
        "major_microspecies": major,
        "microspecies": microspecies,
        "ph": ph,
        "model": "modelseed",
    }


def main():
    _setup_path()

    try:
        raw = sys.stdin.read()
        req = json.loads(raw) if raw.strip() else {}
    except Exception as exc:  # noqa: BLE001
        _fail(f"invalid request JSON: {exc}")

    # Import (and implicitly load model on first predict) once for the whole
    # request — this is what makes the batch path cheap.
    try:
        import predict_pka
        import protonate
        from rdkit import Chem
    except Exception as exc:  # noqa: BLE001
        # Hard dependency failure: non-zero exit so the backend treats the tool
        # as unavailable rather than as a per-compound miss.
        sys.stderr.write(f"OPAM2 import failed: {exc}\n")
        sys.exit(3)

    if isinstance(req.get("compounds"), list):
        # Batch: apply batch-wide defaults under each item's own values.
        defaults = {
            k: req[k]
            for k in ("ph", "tph", "microspecies_format", "acid_model", "base_model")
            if k in req
        }
        results = []
        for item in req["compounds"]:
            merged = dict(defaults)
            if isinstance(item, dict):
                merged.update(item)
            else:
                merged["smiles"] = item
            results.append(_predict_one(merged, predict_pka, protonate, Chem))
        sys.stdout.write(json.dumps({"ok": True, "results": results}))
        sys.stdout.flush()
        return

    # Single compound.
    out = _predict_one(req, predict_pka, protonate, Chem)
    if not out.get("ok"):
        _fail(out.get("error", "unknown error"))
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
