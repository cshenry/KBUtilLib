"""``kbu model`` — metabolic-modeling verb group over KBUtilLib's modeling API.

A thin facade over ``ms_reconstruction_utils``/``ms_fba_utils`` (build,
gapfill, FBA, FVA) plus a provenance-preserving ``exec`` escape hatch for
arbitrary KBUtilLib API use.  This module does not reimplement any modeling
math -- every verb calls straight through to the existing
``MSReconstructionUtils``/``MSFBAUtils`` methods.

Local/offline construction
---------------------------
The documented ``kbu`` interpreter contract is "kbutillib + cobra +
modelseedpy already importable" -- it does **not** include ``cobrakbase``
(KBase-workspace object I/O) or a checked-out ``cb_annotation_ontology_api``
sibling repo, both of which ``KBModelUtils.__init__`` unconditionally
requires.  ``tests/test_ms_fba_utils_eval.py`` already established the
sanctioned pattern for constructing ``MSFBAUtils``/``MSReconstructionUtils``
without that KBase-SDK plumbing: bypass ``KBModelUtils.__init__`` (which
pulls in ``cobrakbase``) and construct only the ``MSBiochemUtils`` base plus
the reconstruction/FBA-specific setup the local API surface needs.  This
module reuses that same pattern (see ``_construct_offline``) rather than
inventing a second one.  The modeling algorithms themselves
(``build_metabolic_model``, ``gapfill_metabolic_model``, ``run_fba``,
``run_fva``) are called completely unmodified.

Accepted --media forms (Acceptance Criterion #10)
---------------------------------------------------
1. **Local JSON file** (default, fully offline) -- a path to a JSON file
   whose contents are a dict accepted by ``modelseedpy.core.msmedia
   .MSMedia.from_dict``:

   - Minimal:  ``{"cpd00027": 10}``                      (uptake bound only)
   - Bounds:   ``{"cpd00027": [-10, 1000]}``              (lower, upper)
   - Complete: ``{"cpd00027": {"lower_bound": -10, "upper_bound": 1000,
     "concentration": 5.0, "name": "D-Glucose"}}``

   See ``tests/fixtures/model/glucose_minimal.json`` for a worked example.

2. **KBase workspace reference** -- any value containing ``/`` that is not
   an existing local file path is treated as a workspace object reference
   (``wsid/object_name`` or ``object_name`` with ``--media-ws``) and resolved
   via ``KBModelUtils.get_media(id_or_ref, ws, msmedia=True)``.  Requires a
   configured KBase token (``KB_AUTH_TOKEN``) and network access.

Accepted --objective forms (Acceptance Criterion #10)
---------------------------------------------------------
The reaction-flux DSL parsed by ``modelseedpy``'s ``ObjectivePkg.build_package``
(consumed by ``MSFBAUtils.run_fba``/``run_fva`` via
``configure_fba_formulation`` -> ``set_objective_from_string``):

    MAX{<rxn_id>}                 -- maximize a single reaction (most common)
    MIN{<rxn_id>}                 -- minimize a single reaction
    MAX{(2.0)+rxn00001|(1.0)-rxn00002}
        -- linear combination: "+"/"-" select the forward/reverse variable,
           the parenthesized number is the coefficient; the direction
           prefix is optional (omit for net flux).

``kbu model fba``/``kbu model fva`` accept this DSL via ``--objective``
(default ``MAX{bio1}``).  ``kbu model gapfill``'s ``--objective`` is
different: ``gapfill_metabolic_model``'s ``objective`` parameter is a bare
target reaction id (default ``bio1``), not the MAX{}/MIN{} DSL -- it is
passed straight through to ``MSGapfill(default_target=objective)``.

Accepted --template forms (reconstruct/gapfill)
--------------------------------------------------
``modelseedpy`` 0.4.2 ships exactly two templates locally (no KBase
workspace needed): ``core`` and ``gn`` (gram-negative).  ``--template``
accepts either shorthand (default ``gn``) for fully offline reconstruction,
or a KBase workspace reference (containing ``/``) resolved via
``KBModelUtils.get_template``, which requires KBase auth.  There is no
offline ``auto``/gram-positive/archaea template in this modelseedpy
release; the genome classifier data KBase uses for ``auto`` template
selection is not bundled with KBUtilLib, so ``gs_template="auto"`` is not
exposed here.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import click

from .manifest import now_utc_iso, sha256_file
from .session import _route_save_local
from .subproject import _find_project_root

# ── local/offline construction of the modeling-utility classes ─────────────


def _resolve_modelseed_db_path() -> Optional[str]:
    """Resolve a local ModelSEED biochemistry database directory, if any.

    Priority: ``KBU_MODELSEED_DB_PATH`` env var, then common local
    checkouts.  Returns ``None`` (deferring to KBUtilLib's own resolution,
    or skipping the biochem DB entirely) when nothing is found -- the
    biochem DB is only required for ``--atp-safe`` reconstruction/gapfill;
    the default (``--no-atp-safe``) verbs never touch it.
    """
    env = os.environ.get("KBU_MODELSEED_DB_PATH")
    candidates = [env] if env else []
    candidates += [
        str(Path.home() / "Dropbox" / "Projects" / "ModelSEEDDatabase"),
        str(Path.home() / "code" / "ModelSEEDDatabase"),
    ]
    for c in candidates:
        if c and (Path(c) / "Biochemistry").is_dir():
            return c
    return None


def _construct_offline(cls: type, **extra_kwargs: Any) -> Any:
    """Construct a ``KBModelUtils`` subclass without the cobrakbase-only path.

    See the module docstring for why this bypass exists.  Mirrors the
    pattern already established by
    ``tests/test_ms_fba_utils_eval.py::_make_fba_utils``.
    """
    from unittest.mock import patch

    from ..kb_model_utils import KBModelUtils
    from ..ms_biochem_utils import MSBiochemUtils

    db_path = _resolve_modelseed_db_path()

    def _init_bypass(self: Any, **kwargs: Any) -> None:
        MSBiochemUtils.__init__(self, **kwargs)

    patchers = [patch.object(KBModelUtils, "__init__", _init_bypass)]
    if db_path is None:
        def _noop_db(self: Any) -> None:
            self._biochem_db = None

        patchers.append(
            patch.object(MSBiochemUtils, "_ensure_database_available", _noop_db)
        )

    with contextlib.ExitStack() as stack:
        for p in patchers:
            stack.enter_context(p)
        instance = cls(
            config_file=False,
            token_file=None,
            kbase_token_file=None,
            modelseed_db_path=db_path,
            **extra_kwargs,
        )
    return instance


def _recon_utils() -> Any:
    """Return an offline-constructed ``MSReconstructionUtils`` instance."""
    from modelseedpy.core.msmodelutl import MSModelUtil

    from ..ms_reconstruction_utils import MSReconstructionUtils

    utils = _construct_offline(MSReconstructionUtils)
    utils.MSModelUtil = MSModelUtil
    return utils


def _fba_utils() -> Any:
    """Return an offline-constructed ``MSFBAUtils`` instance."""
    from modelseedpy.core.msmodelutl import MSModelUtil

    from ..ms_fba_utils import MSFBAUtils

    utils = _construct_offline(MSFBAUtils)
    utils.MSModelUtil = MSModelUtil
    return utils


# ── local templates (the only two modelseedpy 0.4.2 ships offline) ─────────

_LOCAL_TEMPLATE_IDS = {"core": "template_core", "gn": "template_gram_neg"}


def _load_local_template(shorthand: str) -> Any:
    """Build an ``MSTemplate`` from modelseedpy's bundled local template JSON."""
    from modelseedpy.core.mstemplate import MSTemplateBuilder
    from modelseedpy.helpers import get_template as _get_template_dict

    template_id = _LOCAL_TEMPLATE_IDS[shorthand]
    return MSTemplateBuilder.from_dict(_get_template_dict(template_id)).build()


def _resolve_template(recon: Any, template_arg: str) -> tuple:
    """Resolve ``--template`` to ``(gs_template_shorthand, gs_template_obj, core_template_obj)``.

    ``template_arg`` is either ``"core"``/``"gn"`` (local, offline) or a
    KBase workspace reference containing ``/`` (requires auth).
    """
    core_t = _load_local_template("core")
    if template_arg in _LOCAL_TEMPLATE_IDS:
        gs_t = core_t if template_arg == "core" else _load_local_template("gn")
        return template_arg, gs_t, core_t
    if "/" in template_arg:
        gs_t = recon.get_template(template_arg)
        return "custom", gs_t, core_t
    raise click.ClickException(
        f"Unknown --template '{template_arg}': expected 'core', 'gn', or a "
        "KBase workspace reference containing '/'."
    )


# ── media loading (see module docstring for the two accepted forms) ────────


def _load_media(recon: Any, media_arg: str) -> Any:
    """Load a media object from a local JSON file or a KBase workspace ref."""
    from modelseedpy.core.msmedia import MSMedia

    path = Path(media_arg)
    if path.is_file():
        try:
            with open(path, encoding="utf-8") as fh:
                media_dict = json.load(fh)
        except json.JSONDecodeError as exc:
            raise click.ClickException(
                f"--media file '{media_arg}' is not valid JSON: {exc}"
            ) from exc
        media = MSMedia.from_dict(media_dict)
        media.id = path.stem
        return media

    if "/" in media_arg:
        try:
            return recon.get_media(media_arg, msmedia=True)
        except Exception as exc:  # pragma: no cover - requires live KBase
            raise click.ClickException(
                f"Could not resolve --media '{media_arg}' as a KBase workspace "
                f"reference (and no local file exists at that path): {exc}"
            ) from exc

    raise click.ClickException(
        f"--media '{media_arg}' is neither an existing local JSON file nor a "
        "KBase workspace reference (expected 'wsid/object')."
    )


# ── model file I/O (plain cobra JSON -- portable, no KBase object needed) ──


def _load_model(model_path: str) -> Any:
    import cobra

    p = Path(model_path)
    if not p.is_file():
        raise click.ClickException(f"--model file not found: {model_path}")
    try:
        cobra_model = cobra.io.load_json_model(str(p))
    except Exception as exc:
        raise click.ClickException(
            f"Could not load '{model_path}' as a cobra JSON model: {exc}"
        ) from exc
    from modelseedpy.core.msmodelutl import MSModelUtil

    return MSModelUtil.get(cobra_model)


def _save_model(mdlutl: Any, out_path: str) -> None:
    import cobra

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    cobra.io.save_json_model(mdlutl.model, str(out_path))


# ── kbu model group ─────────────────────────────────────────────────────────


@click.group("model")
def model_cmd() -> None:
    """Metabolic-modeling verbs (reconstruct/gapfill/fba/fva) + exec escape hatch.

    A thin facade over KBUtilLib's ``ms_reconstruction_utils``/
    ``ms_fba_utils`` modules.  Runs in the standard ``kbu`` interpreter
    (kbutillib + cobra + modelseedpy); no additional environment needed for
    the default (``--no-atp-safe``) local verbs.
    """


# ── reconstruct ──────────────────────────────────────────────────────────


@model_cmd.command("reconstruct")
@click.option(
    "--genome",
    required=True,
    metavar="PATH",
    help="Path to a local protein FASTA file (.faa/.fasta), one sequence per gene.",
)
@click.option(
    "--out",
    required=True,
    metavar="MODEL.json",
    help="Path to write the draft model as a cobra JSON model.",
)
@click.option(
    "--template",
    default="gn",
    show_default=True,
    metavar="core|gn|WSID/NAME",
    help="Genome-scale template: 'core', 'gn' (gram-negative, local/offline), "
    "or a KBase workspace reference (requires auth).",
)
@click.option(
    "--model-id",
    "model_id",
    default=None,
    metavar="ID",
    help="Model id (defaults to the genome file's stem).",
)
@click.option(
    "--atp-safe/--no-atp-safe",
    default=False,
    show_default=True,
    help="Apply ATP-safe correction (MSATPCorrection). Requires a local "
    "ModelSEED biochemistry database (KBU_MODELSEED_DB_PATH or "
    "~/Dropbox/Projects/ModelSEEDDatabase); off by default for offline "
    "determinism.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def reconstruct_cmd(
    genome: str,
    out: str,
    template: str,
    model_id: Optional[str],
    atp_safe: bool,
    as_json: bool,
) -> None:
    """Build a draft metabolic model from a genome (build_metabolic_model facade)."""
    genome_path = Path(genome)
    if not genome_path.is_file():
        raise click.ClickException(f"--genome file not found: {genome}")

    from modelseedpy.core.msgenome import MSGenome

    ms_genome = MSGenome.from_fasta(str(genome_path))
    resolved_model_id = model_id or genome_path.stem

    recon = _recon_utils()
    _, gs_template_obj, core_template_obj = _resolve_template(recon, template)

    try:
        current_output, mdlutl = recon.build_metabolic_model(
            ms_genome,
            None,  # genome_classifier: unused unless gs_template=="auto"
            model_id=resolved_model_id,
            gs_template=template if template in _LOCAL_TEMPLATE_IDS else "gn",
            core_template=core_template_obj,
            gs_template_obj=gs_template_obj,
            atp_safe=atp_safe,
        )
    except Exception as exc:
        raise click.ClickException(f"reconstruct failed: {exc}") from exc

    if mdlutl is None:
        raise click.ClickException(
            f"reconstruct failed: {current_output.get('Comments')}"
        )

    _save_model(mdlutl, out)

    result = {
        "model_path": str(Path(out).resolve()),
        "reactions": len(mdlutl.model.reactions),
        "metabolites": len(mdlutl.model.metabolites),
        "genes": len(mdlutl.model.genes),
    }

    if as_json:
        click.echo(json.dumps(result))
    else:
        click.echo(
            f"Model written to {result['model_path']} "
            f"({result['reactions']} reactions, {result['metabolites']} metabolites, "
            f"{result['genes']} genes)"
        )


# ── gapfill ──────────────────────────────────────────────────────────────


@model_cmd.command("gapfill")
@click.option("--model", "model_path", required=True, metavar="MODEL.json")
@click.option("--media", "media_arg", required=True, metavar="MEDIA")
@click.option(
    "--out",
    "out_path",
    default=None,
    metavar="MODEL.json",
    help="Where to write the gapfilled model (defaults to overwriting --model).",
)
@click.option(
    "--objective",
    default="bio1",
    show_default=True,
    metavar="RXN_ID",
    help="Bare target reaction id for gapfilling (NOT the MAX{}/MIN{} DSL -- "
    "see module docs).",
)
@click.option(
    "--atp-safe/--no-atp-safe",
    default=False,
    show_default=True,
    help="Include ATP-safe gapfill tests (requires a local ModelSEED "
    "biochemistry database); off by default.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def gapfill_cmd(
    model_path: str,
    media_arg: str,
    out_path: Optional[str],
    objective: str,
    atp_safe: bool,
    as_json: bool,
) -> None:
    """Gapfill a model to feasibility on a media (gapfill_metabolic_model facade)."""
    mdlutl = _load_model(model_path)
    recon = _recon_utils()
    media = _load_media(recon, media_arg)

    core_t = _load_local_template("core")
    gn_t = _load_local_template("gn")

    try:
        _current_output, solutions, _sol, _sol_media = recon.gapfill_metabolic_model(
            mdlutl,
            None,  # genome: unused unless expression_obj is supplied
            media_objs=[media],
            templates=[gn_t],
            core_template=core_t,
            atp_safe=atp_safe,
            objective=objective,
        )
    except Exception as exc:
        raise click.ClickException(f"gapfill failed: {exc}") from exc

    reactions_added: list = []
    if solutions and media in solutions:
        gfsolution = solutions[media]
        reactions_added = sorted(gfsolution.get("new", {})) + sorted(
            gfsolution.get("reversed", {})
        )

    resolved_out = out_path or model_path
    _save_model(mdlutl, resolved_out)

    result = {
        "model_in": str(Path(model_path).resolve()),
        "media": media_arg,
        "model_out": str(Path(resolved_out).resolve()),
        "reactions_added": reactions_added,
    }

    if as_json:
        click.echo(json.dumps(result))
    else:
        click.echo(
            f"Gapfilled model written to {result['model_out']} "
            f"({len(reactions_added)} reactions added)"
        )


# ── fba ──────────────────────────────────────────────────────────────────


@model_cmd.command("fba")
@click.option("--model", "model_path", required=True, metavar="MODEL.json")
@click.option("--media", "media_arg", required=True, metavar="MEDIA")
@click.option(
    "--objective",
    default="MAX{bio1}",
    show_default=True,
    metavar="DSL",
    help="ObjectivePkg DSL string, e.g. 'MAX{bio1}' (see module docs).",
)
@click.option(
    "--top",
    default=25,
    show_default=True,
    help="Max number of fluxes to include, sorted by |flux| descending.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def fba_cmd(
    model_path: str,
    media_arg: str,
    objective: str,
    top: int,
    as_json: bool,
) -> None:
    """Run FBA (pFBA) on a model+media (MSFBAUtils.run_fba facade)."""
    mdlutl = _load_model(model_path)
    recon = _recon_utils()
    media = _load_media(recon, media_arg)
    fba = _fba_utils()

    try:
        solution = fba.run_fba(mdlutl, media=media, objective=objective, run_pfba=True)
    except Exception as exc:
        raise click.ClickException(f"fba failed: {exc}") from exc

    flux_items = sorted(
        solution.fluxes.items(), key=lambda kv: abs(kv[1]), reverse=True
    )[:top]
    result = {
        "objective_value": float(solution.objective_value),
        "fluxes": [{"id": rid, "value": float(val)} for rid, val in flux_items],
        "solver_status": str(solution.status),
    }

    if as_json:
        click.echo(json.dumps(result))
    else:
        click.echo(
            f"objective_value={result['objective_value']:.6g} "
            f"status={result['solver_status']} "
            f"(top {len(result['fluxes'])} fluxes by |value|)"
        )
        for f in result["fluxes"][:10]:
            click.echo(f"  {f['id']}\t{f['value']:.6g}")


# ── fva ──────────────────────────────────────────────────────────────────


@model_cmd.command("fva")
@click.option("--model", "model_path", required=True, metavar="MODEL.json")
@click.option("--media", "media_arg", required=True, metavar="MEDIA")
@click.option(
    "--reactions",
    "reactions_arg",
    default=None,
    metavar="ID,ID,...",
    help="Comma-separated reaction ids to report (default: all reactions).",
)
@click.option(
    "--fraction-of-optimum",
    "fraction_of_optimum",
    default=0.9,
    show_default=True,
    type=float,
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
def fva_cmd(
    model_path: str,
    media_arg: str,
    reactions_arg: Optional[str],
    fraction_of_optimum: float,
    as_json: bool,
) -> None:
    """Run FVA via ms_fba_utils.run_fva (NOT cobra.flux_variability_analysis)."""
    mdlutl = _load_model(model_path)
    recon = _recon_utils()
    media = _load_media(recon, media_arg)
    fba = _fba_utils()

    try:
        fva_raw = fba.run_fva(
            mdlutl, media=media, fraction_of_optimum=fraction_of_optimum
        )
    except Exception as exc:
        raise click.ClickException(f"fva failed: {exc}") from exc

    wanted_ids = None
    if reactions_arg:
        wanted_ids = {r.strip() for r in reactions_arg.split(",") if r.strip()}
        missing = wanted_ids - set(fva_raw)
        if missing:
            raise click.ClickException(
                f"--reactions not found in model: {sorted(missing)}"
            )

    reactions = [
        {"id": rid, "min": float(vals["MIN"]), "max": float(vals["MAX"])}
        for rid, vals in sorted(fva_raw.items())
        if wanted_ids is None or rid in wanted_ids
    ]
    result = {"reactions": reactions}

    if as_json:
        click.echo(json.dumps(result))
    else:
        click.echo(f"{len(reactions)} reactions:")
        for r in reactions[:20]:
            click.echo(f"  {r['id']}\tmin={r['min']:.6g}\tmax={r['max']:.6g}")
        if len(reactions) > 20:
            click.echo(f"  ... +{len(reactions) - 20} more (use --json)")


# ── exec (provenance-preserving escape hatch) ───────────────────────────────


def _package_versions() -> dict:
    versions = {}
    for pkg in ("kbutillib", "cobra", "modelseedpy"):
        try:
            mod = __import__(pkg)
            versions[pkg] = getattr(mod, "__version__", "unknown")
        except Exception:
            versions[pkg] = None
    return versions


def _resolve_runs_root() -> tuple:
    """Resolve the durable runs root and whether a real project context exists.

    Returns ``(runs_root: Path, has_project_context: bool)``.  Prefers the
    project ``runs/`` dir (found via a ``kbu-project.toml`` ancestor of
    cwd); falls back to ``~/.kbcache/kbu-model-exec/`` when no project
    context exists.
    """
    project_root = _find_project_root(Path.cwd())
    if (project_root / "kbu-project.toml").is_file():
        return project_root / "runs", True
    return Path.home() / ".kbcache", False


def _cap_bytes(data: bytes, cap: int = 1_000_000) -> bytes:
    return data[:cap]


@model_cmd.command(
    "exec",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.argument("script", type=click.Path())
@click.option(
    "--timeout",
    default=600,
    show_default=True,
    type=int,
    help="Wall-clock timeout in seconds for the script subprocess.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON.")
@click.argument("script_args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def exec_cmd(
    ctx: click.Context,
    script: str,
    timeout: int,
    as_json: bool,
    script_args: tuple,
) -> None:
    """Run SCRIPT (a Python file) in the kbu interpreter with full provenance.

    Any arguments after ``--`` are passed through to the script unchanged.
    The script always runs with a durable, per-run directory as its cwd
    (see module docs) -- never a throwaway temp dir -- so relative outputs
    it writes are captured, and a copy of the script plus stdout/stderr/
    run.json survive the invocation.
    """
    script_path = Path(script)
    if not script_path.is_file():
        raise click.ClickException(f"SCRIPT not found: {script}")

    # click puts everything after "--" into script_args when
    # ignore_unknown_options+allow_extra_args are set; also honor any
    # leftover ctx.args (defensive, covers both click parsing paths).
    passthrough_args = list(script_args) + list(ctx.args)

    script_hash = sha256_file(script_path)
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    runs_root, has_project_context = _resolve_runs_root()
    run_dir = runs_root / "kbu-model-exec" / f"{timestamp}-{script_hash[:12]}"
    run_dir.mkdir(parents=True, exist_ok=True)

    script_copy = run_dir / script_path.name
    shutil.copy2(script_path, script_copy)

    argv = [sys.executable, str(script_copy), *passthrough_args]
    started_at = now_utc_iso()
    t0 = time.time()

    try:
        proc = subprocess.run(
            argv,
            cwd=str(run_dir),
            env=os.environ.copy(),
            capture_output=True,
            timeout=timeout,
        )
        exit_code = proc.returncode
        stdout_bytes = _cap_bytes(proc.stdout)
        stderr_bytes = _cap_bytes(proc.stderr)
    except subprocess.TimeoutExpired as exc:
        exit_code = 124  # conventional timeout exit code
        stdout_bytes = _cap_bytes(exc.stdout or b"")
        stderr_bytes = _cap_bytes(
            (exc.stderr or b"") + b"\n[kbu model exec] timed out after "
            + str(timeout).encode() + b"s"
        )

    finished_at = now_utc_iso()

    stdout_text = stdout_bytes.decode("utf-8", errors="replace")
    stderr_text = stderr_bytes.decode("utf-8", errors="replace")

    (run_dir / "stdout.txt").write_text(stdout_text, encoding="utf-8")
    (run_dir / "stderr.txt").write_text(stderr_text, encoding="utf-8")

    run_record = {
        "script_hash": script_hash,
        "exit_code": exit_code,
        "started_at": started_at,
        "finished_at": finished_at,
        "versions": _package_versions(),
        "argv": argv,
        "cwd": str(run_dir),
    }
    (run_dir / "run.json").write_text(
        json.dumps(run_record, indent=2), encoding="utf-8"
    )

    # Record a lightweight kbu session entry so the run shows in
    # `kbu session list`, but only when a real project context exists --
    # otherwise this would scatter subprojects/ directories into whatever
    # cwd the caller happened to be in (e.g. KING's ~/koros).
    if has_project_context:
        _record_exec_session(script_path, run_dir, exit_code, started_at, finished_at)

    result = {
        "stdout": stdout_text,
        "stderr": stderr_text,
        "exit_code": exit_code,
        "run_dir": str(run_dir),
    }

    if as_json:
        click.echo(json.dumps(result))
    else:
        click.echo(stdout_text, nl=False)
        if stderr_text:
            click.echo(stderr_text, nl=False, err=True)
        click.echo(f"[exit_code={exit_code}] run_dir={run_dir}", err=True)

    # A failing script must surface a nonzero exit_code in the envelope, not
    # crash `kbu model exec` itself -- so this command always exits 0 unless
    # `kbu` invocation itself was malformed (click handles that separately).


def _record_exec_session(
    script_path: Path,
    run_dir: Path,
    exit_code: int,
    started_at: str,
    finished_at: str,
) -> None:
    """Best-effort `kbu session save`-equivalent entry for `kbu model exec`.

    Non-fatal: a failure here must never crash the exec command itself
    (the run already succeeded/failed and its provenance is already on
    disk under run_dir).
    """
    try:
        payload = {
            "session_id": run_dir.name,
            "command": "kbu-model-exec",
            "summary": f"kbu model exec {script_path.name} (exit_code={exit_code})",
            "started_at": started_at,
            "ended_at": finished_at,
            "topics_discussed": [],
            "decisions_made": [],
            "work_submitted": [str(run_dir)],
            "next_steps": [],
        }
        _route_save_local(payload, "kbu-model-exec")
    except Exception:
        # Best-effort: provenance already lives under run_dir regardless.
        pass
