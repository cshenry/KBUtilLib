#!/usr/bin/env python
"""KBUtilLib worker for Andrew Freiburger's ModelSEED-fork of dGPredictor.

This script is shipped *inside* the KBUtilLib package
(``thermo_predictors/_dgpredictor_worker.py``) and is launched by the
dGPredictor backend with the dGPredictor repo as its working directory and
``DGPREDICTOR_REPO`` pointing at that repo, so it can import the repo's
prediction module. dGPredictor is research code whose modules use *bare*
imports (``from compound import Compound``) and a hard-coded
``sys.path.insert(0, 'CC')``; they therefore only resolve with the repo root on
``sys.path`` and the current working directory set to the repo root. A repo-local
copy named ``kbutillib_dg_worker.py``, if present, takes precedence over this
bundled one (the backend prefers the repo-local copy).

Why a subprocess
----------------
``dg_prediction_modelseed.ModelSEEDdGPredictor`` imports ``rdkit`` and
(transitively, via ``CC/compound.py``) ``openbabel`` at module top, and on
construction loads a joblib BayesianRidge model plus several MB of group-signature
JSON and a gzipped compound cache. Importing all of that into the KBUtilLib
process would pin those heavy deps onto every consumer and pollute
``sys.path``/cwd. KBUtilLib never imports dGPredictor or its dependencies; all of
it lives behind this subprocess boundary.

The predictor object is built ONCE per worker invocation (model + caches load is
the dominant cost) and then answers every reaction in the request, so a whole
reaction set should be sent as a single batch.

Identifiers, units, conditions
------------------------------
Andrew's fork is ModelSEED-native. A reaction can be specified two ways:

* ``rxn_id``  — a ModelSEED ``rxnNNNNN`` accession resolved against the repo's
  bundled ``data/modelseed_reaction_stoich.json``
  (``ModelSEEDdGPredictor.predict_reaction``).
* ``equation`` — a reaction string in **ModelSEED compound ids**, e.g.
  ``"1 cpd00002 + 1 cpd00001 <=> 1 cpd00008 + 1 cpd00009"``
  (``ModelSEEDdGPredictor.predict_from_equation``). The arrow defaults to
  ``"<=>"`` and may be overridden with ``arrow``.
* ``stoichiometry`` — a mapping of ModelSEED ``cpdNNNNN`` -> signed coefficient
  (negative = substrate); the worker renders it to an equation string and uses
  the ``predict_from_equation`` path. This is the namespace-agnostic form the
  KBUtilLib facade passes through.

The output is ``delta_r G'^0`` in **kJ/mol** at the requested pH / ionic strength
(the model applies the pH/I Legendre correction via the bundled compound cache),
with the BayesianRidge posterior standard deviation as the uncertainty.
Temperature is effectively fixed at 298.15 K by the model.

Protocol
--------
Reads one JSON request object from stdin, writes one JSON response to stdout.

Single request (stdin)::

    {
      "rid": "rxn00001",                  # label for the result
      "rxn_id": "rxn00001",               # OR
      "equation": "1 cpd00002 + ... <=> ...",   # OR
      "stoichiometry": {"cpd00002": -1, ...},   # OR
      "pH": 7.0, "I": 0.25, "T": 298.15,
      "arrow": "<=>"
    }

Single response (stdout, success)::

    {"ok": true, "dg_prime": -12.3, "uncertainty": 2.1,
     "pH": 7.0, "ionic_strength": 0.25, "units": "kJ/mol", "transformed": true}

Single response (per-reaction miss, e.g. undecomposable compound)::

    {"ok": false, "error": "..."}

Batch request (stdin)::

    {"reactions": [ {<single-request fields>}, ... ],
     "pH": 7.0, "I": 0.25, "T": 298.15}   # batch-wide defaults

Batch response (stdout)::

    {"ok": true, "results": [ <single-response object>, ... ]}

A non-zero exit / empty stdout signals a hard failure (bad interpreter, missing
deps, model artifact missing) which the backend reports as unavailable.
"""

import json
import os
import sys


def _fail_hard(msg, code=3):
    sys.stderr.write(str(msg) + "\n")
    sys.exit(code)


def _setup_path():
    """Put the dGPredictor repo root (and its CC/ dir) on sys.path so the
    module's bare imports resolve regardless of where this script lives."""
    roots = []
    env_root = os.environ.get("DGPREDICTOR_REPO")
    if env_root:
        roots.append(os.path.abspath(env_root))
    roots.append(os.getcwd())
    here = os.path.abspath(os.path.dirname(__file__))
    roots.append(here)
    for root in roots:
        if os.path.isdir(root) and root not in sys.path:
            sys.path.insert(0, root)
        cc = os.path.join(root, "CC")
        if os.path.isdir(cc) and cc not in sys.path:
            sys.path.insert(0, cc)


def _stoich_to_equation(stoich, arrow="<=>"):
    """Render a {cpd_id: signed_coeff} mapping into a ModelSEED equation string.

    Negative coefficients are substrates (left side), positive are products
    (right side). Coefficients are written as their absolute value.
    """
    subs, prods = [], []
    for cpd_id, coeff in stoich.items():
        try:
            c = float(coeff)
        except (TypeError, ValueError):
            continue
        if c < 0:
            subs.append(f"{abs(c):g} {cpd_id}")
        elif c > 0:
            prods.append(f"{c:g} {cpd_id}")
    return f"{' + '.join(subs)} {arrow} {' + '.join(prods)}"


def _predict_one(req, predictor, defaults):
    """Compute dG'° for one reaction request; return a response dict.

    Never raises: any per-reaction problem becomes ``{"ok": false}`` so a single
    bad reaction cannot abort a batch.
    """
    pH = float(req.get("pH", defaults.get("pH", 7.0)))
    I = float(req.get("I", defaults.get("I", 0.25)))
    T = float(req.get("T", defaults.get("T", 298.15)))
    arrow = req.get("arrow", defaults.get("arrow", "<=>"))

    try:
        rxn_id = req.get("rxn_id")
        equation = req.get("equation")
        stoich = req.get("stoichiometry")

        if rxn_id:
            dg, std = predictor.predict_reaction(rxn_id, pH=pH, I=I, T=T)
        elif equation:
            dg, std = predictor.predict_from_equation(
                equation, pH=pH, I=I, T=T, arrow=arrow
            )
        elif stoich:
            eq = _stoich_to_equation(stoich, arrow=arrow)
            dg, std = predictor.predict_from_equation(
                eq, pH=pH, I=I, T=T, arrow=arrow
            )
        else:
            return {"ok": False, "error": "no rxn_id / equation / stoichiometry"}
    except Exception as exc:  # noqa: BLE001 - per-reaction miss, not fatal
        return {"ok": False, "error": f"prediction failed: {exc}"}

    if dg is None:
        return {
            "ok": False,
            "error": "reaction not predictable (unknown reaction or "
            "undecomposable compound)",
        }

    return {
        "ok": True,
        "dg_prime": float(dg),
        "uncertainty": None if std is None else float(std),
        "pH": pH,
        "ionic_strength": I,
        "units": "kJ/mol",
        "transformed": True,
    }


def main():
    _setup_path()

    try:
        raw = sys.stdin.read()
        req = json.loads(raw) if raw.strip() else {}
    except Exception as exc:  # noqa: BLE001
        _fail_hard(f"invalid request JSON: {exc}")

    # Import + construct the predictor once (loads the model + caches): this is
    # the expensive step and is what makes the batch path cheap.
    try:
        from dg_prediction_modelseed import ModelSEEDdGPredictor
    except Exception as exc:  # noqa: BLE001
        _fail_hard(f"dGPredictor import failed: {exc}", code=3)

    try:
        predictor = ModelSEEDdGPredictor()
    except Exception as exc:  # noqa: BLE001
        _fail_hard(f"ModelSEEDdGPredictor construction failed: {exc}", code=4)

    if isinstance(req.get("reactions"), list):
        defaults = {
            k: req[k] for k in ("pH", "I", "T", "arrow") if k in req
        }
        results = [
            _predict_one(item if isinstance(item, dict) else {"rxn_id": item},
                         predictor, defaults)
            for item in req["reactions"]
        ]
        sys.stdout.write(json.dumps({"ok": True, "results": results}))
        sys.stdout.flush()
        return

    out = _predict_one(req, predictor, {})
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
