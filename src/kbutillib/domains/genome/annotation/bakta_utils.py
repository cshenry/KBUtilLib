"""BAKTA annotation utilities for KBUtilLib.

Implements ``BaktaUtils``, which wraps Bakta's **protein** entry point
(``bakta_proteins``, https://github.com/oschwengers/bakta) to annotate a
caller-supplied set of amino-acid (protein) sequences against a locally
registered Bakta reference database.

This is deliberately the ``bakta_proteins`` route, not the whole-contig
``bakta`` nucleotide pipeline — the two are different input shapes and only
the protein route is wired here.

Database
--------
The database directory is an explicit constructor argument
(``BaktaUtils(db_path=...)``), resolved per-request by the caller — NOT a
config key. This mirrors the ``cache_file=`` decision documented for other
per-request-resolved KBUtilLib annotators and keeps the database selection
out of static config.

``<db_path>/version.json`` is machine-readable
(``{"date", "major", "minor", "type"}``) and is read on every run so the
database's ``major`` version is recorded in ``AnnotationResult.parameters``.
This module does **not** assert tool-vs-database compatibility — Bakta owns
that decision, and an incompatible pair surfaces as a non-zero exit from
``bakta_proteins`` itself, which raises ``subprocess.CalledProcessError``.
When ``version.json`` is missing or unparseable, ``BaktaUtils`` fails fast
with ``ToolUnavailableError`` before ever invoking the tool, using a fixed
message: ``"Bakta DB at <resolved_path> is missing db/version.json or it is
not parseable"``.

Docker/native dispatch
-----------------------
Follows the existing KBUtilLib convention exactly (see ``ProkkaUtils`` /
``TransytUtils``): config key ``bakta.docker_image`` (empty => native
``bakta_proteins`` binary on ``PATH``) and ``bakta.docker_workdir`` for the
tempdir base. Availability is probed with ``docker image inspect``.

Docker containers run with ``--user <uid>:<gid>``, ``--network none``, and
the database bind-mounted read-only. ``bakta_proteins`` requires
``/opt/conda/bin`` on ``PATH`` inside the container or it fails with
*"AMRFinderPlus not found or not executable"* — the container is therefore
launched with ``--entrypoint bash`` so ``PATH`` can be prefixed before the
tool runs.

Result parsing
---------------
Reads Bakta's native JSON output at ``<output>/input.json``. Its
``features`` array carries one record per input protein, keyed by the
caller's own id (``feature["id"]``) — Bakta's protein entry point writes the
FASTA header verbatim from the caller's dict, so no id-remapping is needed
(unlike ``ProkkaUtils`` / ``DRAM2Utils``, which must remap to tool-safe ids).

Per the PRD's originally resolved spec (D4), only the free-text ``product``
string was emitted as a ``Term``:

    Term(namespace="FUNCTION", id=None, value=<product>, evidence={...})

That FUNCTION-destined term is still emitted exactly this way, byte-for-byte
— reactions are reached downstream by joining this text against mapping
tables keyed on it, so it is never rewritten. D4's confinement of the
``psc`` ontology accessions to ``Term.evidence`` only has since been
**deliberately reversed** (see the ``kbdl-ontology-descriptions-v1`` PRD,
task ``annotator-description-emission``): Bakta's per-gene record also
carries ``gene``, and, nested under ``psc``, ``ec_ids``,
``kegg_orthology_id``, ``cog_id`` and ``go_ids``. These are now surfaced
**additionally** as their own namespaced, described Terms —

    Term(namespace="EC"|"KO"|"COG"|"GO", id=<accession>,
         value="<accession>: <description>")

— described via an injected ``OntologyDictionary`` (degrading to the bare
accession when no description is available), alongside a
``Term(namespace="role", ...)`` per ModelSEED role an EC accession resolves
to via an injected ``EcRoleResolver``. None of this touches the FUNCTION
term or its evidence dict: ``ec_ids``, ``kegg_orthology_id``, ``cog_id`` and
``go_ids`` continue to be carried in ``Term.evidence`` too (alongside
``aa_hexdigest`` when present), so nothing is silently dropped there either.

The payload's own version stamp, ``version = {"bakta": ..., "db":
{"version": ..., "type": ...}}``, is the source for
``AnnotationResult.tool_version`` / ``db_version``.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from kbutillib.domains.modeling.ec_role_resolver import EcRoleResolver

from .annotator_utils import (
    AnnotationRecord,
    AnnotationResult,
    AnnotatorUtils,
    Term,
    ToolUnavailableError,
    _guard_protein,
    describe_or_accession,
)
from .ontology_dictionary import OntologyDictionary

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TOOL = "bakta"
_INSTALL_HINT = (
    "Build the kbutillib/bakta Docker image (docker/bakta/Dockerfile) and set "
    "bakta.docker_image in config.yaml, or install bakta_proteins on PATH. "
    "Requires a registered Bakta database directory containing version.json."
)

# psc.* fields carried in Term.evidence (unconditionally, as before) AND now
# additionally surfaced as their own namespaced, described Terms (see module
# docstring) -- the PRD's deliberate reversal of D4's "evidence only" rule.
_EVIDENCE_ONLY_PSC_FIELDS: tuple[str, ...] = (
    "ec_ids",
    "kegg_orthology_id",
    "cog_id",
    "go_ids",
)

# psc.* field -> the ontology namespace its accessions belong to.
_PSC_FIELD_NAMESPACE: dict[str, str] = {
    "ec_ids": "EC",
    "kegg_orthology_id": "KO",
    "cog_id": "COG",
    "go_ids": "GO",
}


# ---------------------------------------------------------------------------
# Pure parse helpers (offline-unit-testable)
# ---------------------------------------------------------------------------


def _as_accession_list(value: Any) -> list[str]:
    """Normalize a ``psc.*`` field into a list of non-empty accession strings.

    Bakta's ``psc`` fields are inconsistently shaped across fields (e.g.
    ``ec_ids``/``kegg_orthology_id``/``go_ids`` are lists, ``cog_id`` is a
    bare string) — this normalizes either shape to a list so all four fields
    can be walked identically. ``None``/empty/non-string entries are
    dropped; anything else falls back to an empty list rather than raising.
    """
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, str) and v]
    return []


def _psc_ontology_terms(
    psc: dict[str, Any],
    ontology_dictionary: OntologyDictionary | None,
    ec_role_resolver: EcRoleResolver | None,
) -> list[Term]:
    """Build the additive, described Terms for a feature's ``psc`` block.

    One ``Term(namespace=<EC|KO|COG|GO>, id=<accession>, value="<accession>:
    <description>")`` per accession in ``ec_ids``/``kegg_orthology_id``/
    ``cog_id``/``go_ids``, degrading to the bare accession (never raising)
    when *ontology_dictionary* has no description on file. Each EC
    accession additionally yields a ``Term(namespace="role", ...)`` per
    ModelSEED role name *ec_role_resolver* resolves it to — this is how EC
    reaches reactions, through ModelSEED's curated role -> complex ->
    reaction chain. Never touches the FUNCTION-destined term or its
    evidence dict (built separately by the caller).

    Args:
        psc: The feature's ``psc`` dict (already type-checked by the
            caller).
        ontology_dictionary: Dictionary used to describe each accession, or
            ``None`` (every accession degrades to itself).
        ec_role_resolver: Resolver used to expand EC accessions into
            ModelSEED role names, or ``None`` (no role Terms are emitted).

    Returns:
        The list of additive Terms (possibly empty, when *psc* carries no
        ontology accessions).
    """
    terms: list[Term] = []
    for field_name, namespace in _PSC_FIELD_NAMESPACE.items():
        for accession in _as_accession_list(psc.get(field_name)):
            terms.append(
                Term(
                    namespace=namespace,
                    id=accession,
                    value=describe_or_accession(
                        namespace, accession, ontology_dictionary
                    ),
                )
            )
            if namespace == "EC" and ec_role_resolver is not None:
                for role in ec_role_resolver.roles_for_ec(accession):
                    terms.append(Term(namespace="role", id=None, value=role))
    return terms


def _parse_bakta_features(
    features: list[dict[str, Any]],
    input_ids: set[str] | None = None,
    ontology_dictionary: OntologyDictionary | None = None,
    ec_role_resolver: EcRoleResolver | None = None,
) -> list[AnnotationRecord]:
    """Convert Bakta's ``input.json`` ``features`` array into AnnotationRecords.

    Emits, per gene, the free-text ``product`` string under
    ``namespace="FUNCTION"`` — byte-identical to D4's original behaviour,
    since reactions are reached downstream by joining this exact text
    against mapping tables keyed on it. ``ec_ids``, ``kegg_orthology_id``,
    ``cog_id`` and ``go_ids`` (nested under ``feature["psc"]``) continue to
    be carried in ``Term.evidence`` (D4's original rule) AND are now
    additionally emitted as their own namespaced, described Terms (see
    :func:`_psc_ontology_terms`) — the PRD's deliberate, knowing reversal of
    D4's "evidence only" confinement. ``aa_hexdigest`` is also carried in
    evidence when present.

    Args:
        features: The parsed ``features`` array from Bakta's ``input.json``.
            Each element is expected to carry ``id`` (the caller's original
            protein id, preserved verbatim) and ``product``.
        input_ids: When given, only features whose ``id`` is a member of this
            set are retained (defends against stray rows from a shared
            output). ``None`` disables the filter.
        ontology_dictionary: Dictionary used to describe ``psc`` accessions,
            or ``None`` (every accession degrades to itself; never raises).
        ec_role_resolver: Resolver used to expand EC accessions into
            ModelSEED role names, or ``None`` (no role Terms are emitted).

    Returns:
        List of ``AnnotationRecord``. A feature with an empty/missing
        ``product`` yields no record and is therefore absent from the
        result — this is not an error.
    """
    records: list[AnnotationRecord] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        gene_id = feature.get("id")
        if not gene_id:
            continue
        if input_ids is not None and gene_id not in input_ids:
            continue

        product = (feature.get("product") or "").strip()
        if not product:
            continue

        evidence: dict[str, Any] = {}
        psc = feature.get("psc")
        if isinstance(psc, dict):
            for key in _EVIDENCE_ONLY_PSC_FIELDS:
                value = psc.get(key)
                if value:
                    evidence[key] = value
        gene_val = feature.get("gene")
        if gene_val:
            evidence["gene"] = gene_val
        aa_hexdigest = feature.get("aa_hexdigest")
        if aa_hexdigest:
            evidence["aa_hexdigest"] = aa_hexdigest

        # FUNCTION-destined term: MUST stay byte-identical to D4's original
        # output. Never rewrite `product` or fold an accession into it.
        terms: list[Term] = [
            Term(namespace="FUNCTION", id=None, value=product, evidence=evidence)
        ]
        if isinstance(psc, dict):
            terms.extend(
                _psc_ontology_terms(psc, ontology_dictionary, ec_role_resolver)
            )

        records.append(AnnotationRecord(gene_id=gene_id, terms=terms))

    return records


def _parse_bakta_db_version(text: str) -> dict[str, Any]:
    """Parse a Bakta ``db/version.json`` payload.

    Args:
        text: Raw file contents of ``<db_path>/version.json``.

    Returns:
        The parsed JSON object as a dict.

    Raises:
        ValueError: If *text* is not parseable JSON, or does not decode to a
            dict carrying a ``major`` key.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not parseable JSON: {exc}") from exc
    if not isinstance(payload, dict) or "major" not in payload:
        raise ValueError("missing 'major' key")
    return payload


# ---------------------------------------------------------------------------
# BaktaUtils class
# ---------------------------------------------------------------------------


class BaktaUtils(AnnotatorUtils):
    """Annotate proteins with Bakta's protein entry point (``bakta_proteins``).

    Config keys (read via ``get_config_value``):
        ``bakta.docker_image`` — Docker image tag to run ``bakta_proteins``
            in. Empty (the default) => run the native ``bakta_proteins``
            binary on ``PATH``.
        ``bakta.executable`` — native executable name/path (default
            ``"bakta_proteins"``). Ignored when ``bakta.docker_image`` is set.
        ``bakta.docker_workdir`` — base dir for the per-run bind-mounted
            work dir (default ``~/.kbutillib/bakta_work``).

    Ontology description / EC-role resolution:
        ``ontology_dictionary`` — an ``OntologyDictionary`` instance used to
            describe ``psc.ec_ids``/``kegg_orthology_id``/``cog_id``/``go_ids``
            accessions. When not given, one is constructed from this
            instance's own config (``ontology_dictionary.{ko,ec,go,cog}_path``
            — see ``OntologyDictionary``), which degrades gracefully (never
            raises) when a namespace's staged dictionary is unset/unreadable.
        ``ec_role_resolver`` — an ``EcRoleResolver`` instance used to expand
            each EC accession into ModelSEED role names. When not given, no
            role-namespace Terms are emitted and the degradation is recorded
            in ``AnnotationResult.parameters["ec_role_resolver"]``.

    Example::

        bu = BaktaUtils(db_path="/scratch/kbdl/refdata/bakta-db-6.0-full")
        if bu.is_available():
            result = bu.annotate({"gene1": "MKTAYIAKQ...", "gene2": "MNFSTPD..."})
            for rec in result.records:
                print(rec.gene_id, [t.value for t in rec.terms])

    Raises:
        ValueError: If the input contains nucleotide-looking sequences.
        ToolUnavailableError: If the Docker image / native binary is absent,
            or if the database's ``version.json`` is missing or unparseable.
    """

    _tool_name = _TOOL
    _install_hint = _INSTALL_HINT

    def __init__(
        self,
        db_path: str,
        ontology_dictionary: OntologyDictionary | None = None,
        ec_role_resolver: EcRoleResolver | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize BaktaUtils against a specific, already-resolved database.

        Args:
            db_path: Path to the Bakta reference database directory
                (must contain ``version.json`` and ``bakta.db``). Resolved
                per-request by the caller — this is a constructor argument,
                not a config key, so multiple database versions can coexist
                without touching config.
            ontology_dictionary: Dictionary used to describe ``psc.*``
                ontology accessions. When ``None`` (the default), one is
                constructed from this instance's own config, which degrades
                gracefully — never raises — for any namespace whose staged
                dictionary is unset/unreadable.
            ec_role_resolver: Resolver used to expand EC accessions into
                ModelSEED role names. When ``None`` (the default), no
                role-namespace Terms are emitted for this instance's runs;
                the degradation is recorded in
                ``AnnotationResult.parameters["ec_role_resolver"]``.
            **kwargs: Forwarded to ``AnnotatorUtils.__init__``.
        """
        super().__init__(**kwargs)
        self.db_path = db_path
        self._bakta_exe: str = self.get_config_value(
            "bakta.executable", default="bakta_proteins"
        )
        #: When set, bakta_proteins runs inside this Docker image instead of
        #: as a local executable. Empty string => native executable mode.
        self._docker_image: str = (
            self.get_config_value("bakta.docker_image", default="") or ""
        )
        self._ontology_dictionary: OntologyDictionary = (
            ontology_dictionary
            if ontology_dictionary is not None
            else OntologyDictionary(**kwargs)
        )
        #: EcRoleResolver instance, or None -- constructor-injected only
        #: (see module docstring: "the caller supplies the path ... via the
        #: constructor"). No automatic file discovery is attempted here, so
        #: an absent resolver can never raise; it simply degrades to no
        #: role-namespace Terms (recorded in parameters, see annotate()).
        self._ec_role_resolver: EcRoleResolver | None = ec_role_resolver

    # ------------------------------------------------------------------
    # Availability probe
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if bakta_proteins can run, side-effect-free.

        In Docker mode (``bakta.docker_image`` set) this checks that the
        image is present locally (``docker image inspect``). Otherwise it
        checks that the configured executable resolves via ``shutil.which``
        (absolute paths are checked for existence + executable bit).

        Returns:
            True if bakta_proteins is runnable via the active mode.
        """
        if self._docker_image:
            return self._docker_image_present()
        exe = Path(self._bakta_exe)
        if exe.is_absolute():
            return exe.is_file() and os.access(exe, os.X_OK)
        return shutil.which(self._bakta_exe) is not None

    def _docker_workdir_base(self) -> str | None:
        """Base dir for the per-run work dir (``tempfile.TemporaryDirectory``).

        Returns None in native mode (system default ``$TMPDIR``). In Docker
        mode the work dir is bind-mounted into the container, so it must
        live under a path Docker Desktop shares by default (see
        ``ProkkaUtils._docker_workdir_base`` for the same macOS/Docker
        caveat). Override with ``bakta.docker_workdir``.
        """
        if not self._docker_image:
            return None
        base = (
            self.get_config_value("bakta.docker_workdir", default="")
            or str(Path.home() / ".kbutillib" / "bakta_work")
        )
        Path(base).mkdir(parents=True, exist_ok=True)
        return base

    def _docker_image_present(self) -> bool:
        """Return True if ``docker image inspect <bakta.docker_image>`` exits 0."""
        if not self._docker_image:
            return False
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", self._docker_image],
                capture_output=True,
                text=True,
                timeout=15,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False

    # ------------------------------------------------------------------
    # Public annotate method
    # ------------------------------------------------------------------

    def annotate(  # type: ignore[override]
        self,
        proteins: dict[str, str],
        threads: int = 1,
        **params: Any,
    ) -> AnnotationResult:
        """Annotate protein sequences using Bakta's ``bakta_proteins``.

        Args:
            proteins: Mapping ``{caller_id: amino_acid_sequence}``. Values
                must be protein sequences; sequences that look like
                nucleotide (>10% non-protein chars) raise ``ValueError``.
            threads: Number of CPUs passed to ``--threads``. Default 1.
            **params: Extra parameters merged into the recorded
                ``AnnotationResult.parameters`` dict. Not forwarded to the
                ``bakta_proteins`` CLI.

        Returns:
            An ``AnnotationResult`` with:
            - ``tool = "bakta"``.
            - ``tool_version`` / ``db_version`` sourced from the payload's
              own ``version`` stamp.
            - ``records`` keyed by the caller's original ids; genes with no
              ``product`` are absent.

        Raises:
            ToolUnavailableError: If bakta_proteins is not available, or if
                the configured database's ``version.json`` is missing or
                unparseable.
            ValueError: If the input sequences fail the protein alphabet
                guard.
            subprocess.CalledProcessError: If ``bakta_proteins`` exits
                non-zero.
        """
        self._require_available()
        _guard_protein(proteins)

        resolved_db = str(Path(self.db_path).expanduser().resolve())
        db_payload = self._read_db_version(resolved_db)

        run_id = uuid.uuid4().hex

        with tempfile.TemporaryDirectory(dir=self._docker_workdir_base()) as tmpdir:
            tmp = Path(tmpdir)
            fasta_path = tmp / "input.faa"
            _write_one_line_faa(fasta_path, proteins)
            outdir = tmp / "bakta_out"

            payload, command = self._run_bakta(
                fasta_path=fasta_path,
                outdir=outdir,
                resolved_db=resolved_db,
                threads=threads,
            )

        if self._ec_role_resolver is None:
            self.log_warning(
                "No EcRoleResolver was provided to BaktaUtils; EC accessions "
                "will not yield role-namespace terms until an EcRoleResolver "
                "(built from ModelSEED's Annotations/Roles.tsv) is injected "
                "via BaktaUtils(ec_role_resolver=...)."
            )

        features = payload.get("features", []) if isinstance(payload, dict) else []
        records = _parse_bakta_features(
            features,
            set(proteins),
            ontology_dictionary=self._ontology_dictionary,
            ec_role_resolver=self._ec_role_resolver,
        )

        version_info = payload.get("version", {}) if isinstance(payload, dict) else {}
        tool_version = (
            version_info.get("bakta") if isinstance(version_info, dict) else None
        )
        db_info = version_info.get("db") if isinstance(version_info, dict) else None
        db_version = (
            db_info.get("version") if isinstance(db_info, dict) else None
        )

        parameters: dict[str, Any] = {
            "threads": threads,
            "db_path": resolved_db,
            "db_major": db_payload.get("major"),
            "db_type": db_payload.get("type"),
            "docker_image": self._docker_image,
            **params,
        }
        # Record ontology-dictionary degradation the same way KofamscanUtils
        # records "ko_function_map: absent" -- only namespaces this run
        # actually queried (i.e. that had at least one accession) can be
        # known to be degraded (see OntologyDictionary.degraded_namespaces).
        for namespace in sorted(self._ontology_dictionary.degraded_namespaces):
            parameters[f"ontology_{namespace.lower()}"] = "absent"
        if self._ec_role_resolver is None:
            parameters["ec_role_resolver"] = "absent"

        return AnnotationResult(
            tool=_TOOL,
            tool_version=tool_version,
            db_version=db_version,
            run_id=run_id,
            command=command,
            parameters=parameters,
            records=records,
        )

    # ------------------------------------------------------------------
    # Database version check
    # ------------------------------------------------------------------

    def _read_db_version(self, resolved_db: str) -> dict[str, Any]:
        """Read and parse ``<resolved_db>/version.json``.

        Args:
            resolved_db: Absolute, resolved path to the database directory.

        Returns:
            The parsed ``version.json`` payload.

        Raises:
            ToolUnavailableError: When the file is missing or unparseable.
                Uses the fixed wording required for ops-runbook matching:
                ``"Bakta DB at <resolved_db> is missing db/version.json or
                it is not parseable"``.
        """
        version_path = Path(resolved_db) / "version.json"
        try:
            text = version_path.read_text(encoding="utf-8")
            payload = _parse_bakta_db_version(text)
        except (OSError, ValueError):
            raise ToolUnavailableError(
                tool=_TOOL,
                detail=(
                    f"Bakta DB at {resolved_db} is missing db/version.json "
                    "or it is not parseable"
                ),
            ) from None
        return payload

    # ------------------------------------------------------------------
    # Internal subprocess runner
    # ------------------------------------------------------------------

    def _run_bakta(
        self,
        fasta_path: Path,
        outdir: Path,
        resolved_db: str,
        threads: int,
    ) -> tuple[dict[str, Any], str]:
        """Run bakta_proteins and return (parsed input.json, command string).

        Args:
            fasta_path: Path to the one-line-per-record input FASTA.
            outdir: Output directory. ``bakta_proteins`` refuses an
                ``--output`` directory that already exists, so this method
                creates only ``outdir.parent`` and leaves ``outdir`` itself
                for ``bakta_proteins`` to create.
            resolved_db: Absolute, resolved path to the database directory.
            threads: CPU count.

        Returns:
            Tuple of (parsed ``input.json`` payload — ``{}`` if the file is
            absent, shlex-quoted command string).

        Raises:
            subprocess.CalledProcessError: If bakta_proteins exits non-zero.
        """
        # bakta_proteins refuses an --output directory that already exists,
        # so only the parent is created here; bakta_proteins creates
        # ``outdir`` itself.
        outdir.parent.mkdir(parents=True, exist_ok=True)

        if self._docker_image:
            work = fasta_path.parent.resolve()
            if outdir.parent.resolve() != work:
                raise ValueError(
                    "Docker mode requires the input FASTA and outdir to share "
                    f"a parent dir; got fasta={fasta_path.parent} "
                    f"outdir={outdir.parent}"
                )
            inner = (
                "PATH=/opt/conda/bin:$PATH bakta_proteins "
                f"--threads {threads} --db /db --output /work/{outdir.name} "
                f"/work/{fasta_path.name}"
            )
            cmd = [
                "docker", "run", "--rm",
                "--user", f"{os.getuid()}:{os.getgid()}",
                "--network", "none",
                "-v", f"{work}:/work",
                "-v", f"{resolved_db}:/db:ro",
                "--entrypoint", "bash",
                self._docker_image,
                "-lc", inner,
            ]
        else:
            cmd = [
                self._bakta_exe,
                "--threads", str(threads),
                "--db", resolved_db,
                "--output", str(outdir),
                str(fasta_path),
            ]

        command_str = shlex.join(cmd)

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, cmd, result.stdout, result.stderr
            )

        json_path = outdir / "input.json"
        if not json_path.exists():
            return {}, command_str
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        return payload, command_str


# ---------------------------------------------------------------------------
# Shared FASTA writer (one-line-per-record, no wrapping)
# ---------------------------------------------------------------------------


def _write_one_line_faa(path: Path, sequences: dict[str, str]) -> None:
    """Write ``{id: sequence}`` as a one-line-per-record FASTA (no wrapping).

    Args:
        path: Destination ``.faa`` path (created/overwritten).
        sequences: Mapping of id -> amino-acid sequence. Headers are written
            verbatim from the caller's ids (no remapping).
    """
    lines: list[str] = []
    for seq_id, seq in sequences.items():
        lines.append(f">{seq_id}")
        lines.append(seq.strip())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Convenience re-exports
# ---------------------------------------------------------------------------

__all__ = [
    "BaktaUtils",
    "_parse_bakta_features",
    "_parse_bakta_db_version",
    "_write_one_line_faa",
]
