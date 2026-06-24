"""Transyt transport-reaction annotation via Docker.

TransytUtils runs the Transyt tool (from the merlin-sysbio group / U Minho)
inside a Docker container to annotate a proteome with transport reactions and
transporter families (TC numbers) from the TCDB.

The tool requires:
- A Docker image with Transyt's Java JAR and a Neo4j 4.x graph DB pre-loaded
  with the TCDB + ModelSEED mapping data.
- An NCBI taxonomy ID for the organism.

Usage example::

    from kbutillib import TransytUtils

    tu = TransytUtils()
    if not tu.is_available():
        print("Transyt Docker image not present — skip")
    else:
        result = tu.annotate(
            proteins={"prot1": "MKTAYIAKQRQISFVK...", "prot2": "MNTFSPD..."},
            tax_id="562",
        )
        for rec in result.records:
            print(rec.gene_id, [t.namespace + ":" + t.id for t in rec.terms])

Docker invocation strategy
--------------------------
The Docker image's native entrypoint is the KBase SDK runner.  We override it
with ``--entrypoint bash`` and run::

    neo4j start
    <neo4j readiness poll — curl http://localhost:7474/ up to neo4j_timeout s>
    java --add-exports java.base/jdk.internal.misc=ALL-UNNAMED
         -Dio.netty.tryReflectionSetAccessible=true
         -Dworkdir=/workdir
         -Xmx4096m
         -jar /opt/transyt/transyt.jar 3 /workdir/processingDir/

The input directory is bind-mounted at ``/workdir/processingDir`` and must
contain:

- ``protein.faa``    — FASTA with one record per query protein.
- ``params.txt``     — Key=value file with taxID, reference_database, and
                       scoring defaults.
- ``metabolites.txt``— (Optional) One ModelSEED compound id per line.

Output files are written to ``{input_dir}/results/``:

- ``results/transyt.xml``               — SBML with transport reactions and GPR.
- ``results/reactions_references.txt``  — TSV: Transyt rxn id → MSRXN → MSCPD.

Exit-code semantics
-------------------
Exit code 8 (or an empty results directory) means Transyt found no resolvable
taxonomy → no annotations.  This is treated as an empty result, **not** an
error.

Result parsing
--------------
``_parse_transyt_xml`` extracts gene → TC-family associations from the SBML
gene-product-association nodes.
``_parse_reactions_references`` maps Transyt reaction ids → ModelSEED reaction
and compound ids.  Both are pure functions (no Docker, no I/O side effects
beyond reading the files).
"""

from __future__ import annotations

import re
import shlex
import subprocess
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .annotator_utils import (
    AnnotationRecord,
    AnnotationResult,
    AnnotatorUtils,
    Term,
    ToolUnavailableError,
    _guard_protein,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TOOL = "transyt"
_INSTALL_HINT = (
    "Pull the Transyt Docker image: docker pull merlin-sysbio/kb_transyt "
    "or set transyt.docker_image in config.yaml and build docker/transyt/Dockerfile"
)

# Default params.txt values written to the input directory
_DEFAULT_SCORING = {
    "blastEvalue": "1e-5",
    "blastBitScore": "50",
    "blastIdentity": "50",
    "blastQueryCoverage": "50",
    "blastp_db": "NULL",
}

# SBML namespace used in Transyt output
_SBML_NS = "http://www.sbml.org/sbml/level2/version4"
_SBML_HTML_NS = "http://www.w3.org/1999/xhtml"

# TC number pattern in SBML reaction notes
_TC_PATTERN = re.compile(r"TC:\s*(\d+\.\w+\.\d+\.\d+(?:\.\d+)?)")

# Transyt exit code for "no resolvable taxonomy"
_EXIT_NO_TAXONOMY = 8


# ---------------------------------------------------------------------------
# Pure parse functions (no Docker, fully unit-testable offline)
# ---------------------------------------------------------------------------


def _parse_transyt_xml(
    xml_path: Path,
) -> dict[str, list[str]]:
    """Parse a Transyt SBML results file and return gene → reaction-id list.

    The SBML contains ``<geneProductAssociation reaction="R_Txxxx">`` nodes
    linking reactions to gene products.  This function collects all reaction
    ids associated with each gene-product name (the ``name`` attribute of
    ``<geneProduct>`` elements, which corresponds to the caller's protein id).

    Args:
        xml_path: Path to ``results/transyt.xml``.

    Returns:
        A dict mapping gene-product name (caller protein id) to a list of
        Transyt reaction ids (e.g. ``["R_T0001", "R_T0003"]``).  Genes with
        no associations are absent.
    """
    try:
        tree = ET.parse(str(xml_path))
    except ET.ParseError:
        return {}

    root = tree.getroot()

    # Resolve namespace prefix (the file may or may not include it)
    def _tag(local: str, ns: str = _SBML_NS) -> str:
        return f"{{{ns}}}{local}"

    # Build id → name map from listOfGeneProducts
    id_to_name: dict[str, str] = {}
    model = root.find(_tag("model"))
    if model is None:
        # Try without namespace
        model = root.find("model")
    if model is not None:
        for gp_list_tag in (_tag("listOfGeneProducts"), "listOfGeneProducts"):
            gp_list = model.find(gp_list_tag)
            if gp_list is not None:
                for gp_tag in (_tag("geneProduct"), "geneProduct"):
                    for gp in gp_list.findall(gp_tag):
                        gp_id = gp.get("id", "")
                        gp_name = gp.get("name", gp_id)
                        if gp_id:
                            id_to_name[gp_id] = gp_name
                break

    # Collect gene → reactions from listOfGeneProductAssociations
    gene_to_rxns: dict[str, list[str]] = {}
    if model is not None:
        for assoc_list_tag in (
            _tag("listOfGeneProductAssociations"),
            "listOfGeneProductAssociations",
        ):
            assoc_list = model.find(assoc_list_tag)
            if assoc_list is not None:
                for assoc_tag in (
                    _tag("geneProductAssociation"),
                    "geneProductAssociation",
                ):
                    for assoc in assoc_list.findall(assoc_tag):
                        rxn_id = assoc.get("reaction", "")
                        if not rxn_id:
                            continue
                        for ref_tag in (
                            _tag("geneProductRef"),
                            "geneProductRef",
                        ):
                            for ref in assoc.findall(ref_tag):
                                gp_id = ref.get("geneProduct", "")
                                gene_name = id_to_name.get(gp_id, gp_id)
                                if gene_name:
                                    gene_to_rxns.setdefault(gene_name, [])
                                    if rxn_id not in gene_to_rxns[gene_name]:
                                        gene_to_rxns[gene_name].append(rxn_id)
                break

    return gene_to_rxns


def _parse_reaction_tc(
    xml_path: Path,
) -> dict[str, str]:
    """Extract TC family from reaction notes in a Transyt SBML file.

    Each ``<reaction>`` element may contain a ``<notes>`` block with a line
    like ``TC: 3.A.1.1.1``.  This function builds a map from reaction id to
    TC family string.

    Args:
        xml_path: Path to ``results/transyt.xml``.

    Returns:
        Dict mapping reaction id (e.g. ``"R_T0001"``) to TC string
        (e.g. ``"3.A.1.1.1"``).
    """
    try:
        tree = ET.parse(str(xml_path))
    except ET.ParseError:
        return {}

    root = tree.getroot()

    def _tag(local: str, ns: str = _SBML_NS) -> str:
        return f"{{{ns}}}{local}"

    model = root.find(_tag("model"))
    if model is None:
        model = root.find("model")

    rxn_to_tc: dict[str, str] = {}
    if model is not None:
        for rxn_list_tag in (_tag("listOfReactions"), "listOfReactions"):
            rxn_list = model.find(rxn_list_tag)
            if rxn_list is not None:
                for rxn_tag in (_tag("reaction"), "reaction"):
                    for rxn in rxn_list.findall(rxn_tag):
                        rxn_id = rxn.get("id", "")
                        if not rxn_id:
                            continue
                        # Search notes for TC pattern
                        tc_val = _extract_tc_from_reaction(rxn)
                        if tc_val:
                            rxn_to_tc[rxn_id] = tc_val
                break

    return rxn_to_tc


def _extract_tc_from_reaction(rxn_elem: ET.Element) -> str | None:
    """Extract TC family string from a single SBML reaction element's notes.

    Args:
        rxn_elem: An ``<reaction>`` XML element.

    Returns:
        TC family string (e.g. ``"3.A.1.1.1"``) or None if not present.
    """
    # Walk all text content under <notes>
    for child in rxn_elem:
        local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if local == "notes":
            text = ET.tostring(child, encoding="unicode", method="text")
            match = _TC_PATTERN.search(text)
            if match:
                return match.group(1)
    return None


def _parse_reactions_references(
    ref_path: Path,
) -> dict[str, tuple[str | None, list[str]]]:
    """Parse reactions_references.txt → reaction → (MSRXN, [MSCPD, ...]).

    The file format (as produced by Transyt) is tab-separated with columns:
        transyt_rxn_id  modelseed_rxn_id  modelseed_cpd_ids

    Lines beginning with ``#`` are comments.  The ``modelseed_cpd_ids``
    field contains semicolon-separated compound ids (may be empty or absent).

    Args:
        ref_path: Path to ``results/reactions_references.txt``.

    Returns:
        Dict mapping Transyt reaction id to
        ``(modelseed_rxn_id, [modelseed_cpd_id, ...])``.
        ``modelseed_rxn_id`` is None when the column is absent or empty.
    """
    result: dict[str, tuple[str | None, list[str]]] = {}
    try:
        text = ref_path.read_text(encoding="utf-8")
    except OSError:
        return result

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        rxn_id = parts[0]
        msrxn = parts[1].strip() if len(parts) > 1 else None
        if msrxn == "" or msrxn is None:
            msrxn = None
        cpds_raw = parts[2].strip() if len(parts) > 2 else ""
        cpds = [c.strip() for c in cpds_raw.split(";") if c.strip()]
        result[rxn_id] = (msrxn, cpds)

    return result


def _build_annotation_records(
    gene_to_rxns: dict[str, list[str]],
    rxn_to_tc: dict[str, str],
    rxn_to_msrxn_cpds: dict[str, tuple[str | None, list[str]]],
    input_protein_ids: list[str],
) -> list[AnnotationRecord]:
    """Combine parsed Transyt outputs into AnnotationRecord list.

    For each gene in *gene_to_rxns*, emits:
    - One ``Term("TC", tc_family, "transporter")`` per associated reaction
      that has a TC annotation.
    - One ``Term("MSRXN", msrxn, "reaction")`` per associated reaction that
      maps to a ModelSEED reaction.
    - One ``Term("MSCPD", cpd, "compound")`` per associated metabolite.

    Only genes whose ids appear in *input_protein_ids* are included (to
    guard against stray ids from the tool output).

    Args:
        gene_to_rxns: Gene name → list of Transyt reaction ids.
        rxn_to_tc: Reaction id → TC family string.
        rxn_to_msrxn_cpds: Reaction id → (MSRXN, [MSCPD, ...]).
        input_protein_ids: The caller's original protein ids (as passed to
            ``annotate``).

    Returns:
        List of AnnotationRecord (one per gene that has at least one Term).
        Genes with no annotations are absent.
    """
    input_ids_set = set(input_protein_ids)
    records: list[AnnotationRecord] = []

    for gene_name, rxn_ids in gene_to_rxns.items():
        # Only include genes that were actually submitted
        if gene_name not in input_ids_set:
            continue

        terms: list[Term] = []
        seen_tc: set[str] = set()
        seen_msrxn: set[str] = set()
        seen_cpd: set[str] = set()

        for rxn_id in rxn_ids:
            tc = rxn_to_tc.get(rxn_id)
            if tc and tc not in seen_tc:
                terms.append(
                    Term(
                        namespace="TC",
                        id=tc,
                        value="transporter",
                        evidence={"transyt_rxn_id": rxn_id},
                    )
                )
                seen_tc.add(tc)

            mapping = rxn_to_msrxn_cpds.get(rxn_id)
            if mapping is not None:
                msrxn, cpds = mapping
                if msrxn and msrxn not in seen_msrxn:
                    terms.append(
                        Term(
                            namespace="MSRXN",
                            id=msrxn,
                            value="reaction",
                            evidence={"transyt_rxn_id": rxn_id},
                        )
                    )
                    seen_msrxn.add(msrxn)
                for cpd in cpds:
                    if cpd not in seen_cpd:
                        terms.append(
                            Term(
                                namespace="MSCPD",
                                id=cpd,
                                value="compound",
                                evidence={"transyt_rxn_id": rxn_id},
                            )
                        )
                        seen_cpd.add(cpd)

        if terms:
            records.append(AnnotationRecord(gene_id=gene_name, terms=terms))

    return records


# ---------------------------------------------------------------------------
# TransytUtils class
# ---------------------------------------------------------------------------


class TransytUtils(AnnotatorUtils):
    """Annotate a proteome with transport reactions using Transyt via Docker.

    Transyt predicts membrane transporter families (TC numbers) and their
    associated ModelSEED reactions and metabolites.  It requires a Docker
    image containing the Transyt JAR and a pre-populated Neo4j 4.x graph DB.

    Config keys (read via ``get_config_value``):
        ``transyt.docker_image`` — Docker image tag/digest to use.
        ``transyt.neo4j_timeout`` — Max seconds to wait for Neo4j readiness
            (default 120).

    Example::

        tu = TransytUtils()
        result = tu.annotate(
            proteins={"gene1": "MKTAYIAKQ...", "gene2": "MNFSTPD..."},
            tax_id="562",
        )

    Raises:
        ValueError: If *proteins* contains nucleotide sequences.
        ToolUnavailableError: If the Docker image is absent.
        ValueError: If *tax_id* is not provided.
    """

    _tool_name = "transyt"
    _install_hint = _INSTALL_HINT

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._docker_image: str = self.get_config_value(
            "transyt.docker_image",
            default="merlin-sysbio/kb_transyt:latest",
        )
        self._neo4j_timeout: int = int(
            self.get_config_value("transyt.neo4j_timeout", default=120)
        )

    def _docker_workdir_base(self) -> str:
        """Base dir for the per-run input dir (``tempfile.TemporaryDirectory``).

        The input dir is bind-mounted into the container, so it MUST live under
        a path Docker shares.  ``$TMPDIR`` on macOS is ``/var/folders/...``,
        which Docker Desktop does NOT share by default — a bind mount of it
        appears empty inside the container, so transyt finds no ``protein.faa``.
        Use a dir under the kbutillib home (``~/.kbutillib/transyt_work``),
        which is under the user's home and shared by default.  Override with
        ``transyt.docker_workdir``.
        """
        base = (
            self.get_config_value("transyt.docker_workdir", default="")
            or str(Path.home() / ".kbutillib" / "transyt_work")
        )
        Path(base).mkdir(parents=True, exist_ok=True)
        return base

    # ------------------------------------------------------------------
    # Availability probe
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if the configured Docker image is locally present.

        Runs ``docker image inspect <tag>`` and returns True on exit code 0,
        False on any failure (no Docker, image absent, etc.).  Side-effect
        free.
        """
        if not self._docker_image:
            return False
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", self._docker_image],
                capture_output=True,
                timeout=10,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def annotate(  # type: ignore[override]
        self,
        proteins: dict[str, str],
        tax_id: str = "",
        reference_database: str = "ModelSEED",
        metabolites: list[str] | None = None,
        **kwargs: Any,
    ) -> AnnotationResult:
        """Annotate proteins with transport reactions using Transyt.

        Args:
            proteins: Mapping of ``{gene_id: amino_acid_sequence}``.  The
                sequences must be amino-acid (protein); nucleotide sequences
                are rejected by the protein alphabet guard.
            tax_id: NCBI taxonomy id string (e.g. ``"562"`` for *E. coli*
                K-12).  **Required** — Transyt cannot run without a
                resolvable taxonomy id.
            reference_database: Reference database name.  Defaults to
                ``"ModelSEED"``.
            metabolites: Optional list of ModelSEED compound ids to
                constrain the search (written to ``metabolites.txt``).
            **kwargs: Ignored extra keyword arguments (for interface
                compatibility with the base class).

        Returns:
            An ``AnnotationResult`` with:
            - ``tool = "transyt"``
            - ``tool_version``: image tag/digest string.
            - ``db_version``: None (Transyt embeds its DB in the image).
            - ``run_id``: uuid4 hex.
            - ``command``: The exact ``docker run`` command line.
            - ``parameters``: Resolved parameters including defaults.
            - ``records``: Per-gene list; genes with no transport hits are
              absent.  Exit code 8 / empty results → empty list (not an
              error).

        Raises:
            ValueError: If ``tax_id`` is empty.
            ValueError: If any sequence in *proteins* fails the protein
                alphabet guard (looks like nucleotide).
            ToolUnavailableError: If the Docker image is not locally
                present.
        """
        if not tax_id:
            raise ValueError(
                "tax_id is required for TransytUtils.annotate(). "
                "Provide the NCBI taxonomy id for the organism "
                "(e.g. tax_id='562' for E. coli K-12)."
            )

        # Guard before availability check so callers get meaningful errors
        # for bad inputs even when the image is absent.
        _guard_protein(proteins)
        self._require_available()  # noqa: SIM117

        run_id = uuid.uuid4().hex

        parameters: dict[str, Any] = {
            "tax_id": tax_id,
            "reference_database": reference_database,
            "metabolites": metabolites,
            "docker_image": self._docker_image,
            "neo4j_timeout": self._neo4j_timeout,
            **_DEFAULT_SCORING,
        }

        with tempfile.TemporaryDirectory(
            prefix="transyt_", dir=self._docker_workdir_base()
        ) as tmpdir:
            indir = Path(tmpdir) / "processingDir"
            indir.mkdir()

            self._stage_inputs(indir, proteins, tax_id, reference_database, metabolites)
            cmd, exit_code = self._run_docker(indir)

            tool_version = f"{self._docker_image}"
            try:
                digest = self._get_image_digest()
                if digest:
                    tool_version = f"{self._docker_image}@{digest}"
            except Exception:  # pragma: no cover
                pass

            records: list[AnnotationRecord] = []
            if exit_code != _EXIT_NO_TAXONOMY:
                results_dir = indir / "results"
                xml_path = results_dir / "transyt.xml"
                ref_path = results_dir / "reactions_references.txt"
                if xml_path.exists() and ref_path.exists():
                    records = self._parse_results(
                        xml_path, ref_path, list(proteins.keys())
                    )

        return AnnotationResult(
            tool=_TOOL,
            tool_version=tool_version,
            db_version=None,
            run_id=run_id,
            command=cmd,
            parameters=parameters,
            records=records,
        )

    # ------------------------------------------------------------------
    # Input staging
    # ------------------------------------------------------------------

    def _stage_inputs(
        self,
        indir: Path,
        proteins: dict[str, str],
        tax_id: str,
        reference_database: str,
        metabolites: list[str] | None,
    ) -> None:
        """Write protein.faa, params.txt, and optional metabolites.txt.

        Args:
            indir: The input directory (bind-mounted at
                ``/workdir/processingDir`` inside the container).
            proteins: Mapping of gene id → amino-acid sequence.
            tax_id: NCBI taxonomy id.
            reference_database: Reference database name.
            metabolites: Optional list of ModelSEED compound ids.
        """
        # Write protein.faa
        faa_lines: list[str] = []
        for gid, seq in proteins.items():
            faa_lines.append(f">{gid}")
            faa_lines.append(seq.strip())
        (indir / "protein.faa").write_text("\n".join(faa_lines) + "\n", encoding="utf-8")

        # Write params.txt
        params_lines: list[str] = [
            f"taxID={tax_id}",
            f"reference_database={reference_database}",
        ]
        for k, v in _DEFAULT_SCORING.items():
            params_lines.append(f"{k}={v}")
        (indir / "params.txt").write_text("\n".join(params_lines) + "\n", encoding="utf-8")

        # Write optional metabolites.txt
        if metabolites:
            (indir / "metabolites.txt").write_text(
                "\n".join(metabolites) + "\n", encoding="utf-8"
            )

    # ------------------------------------------------------------------
    # Docker invocation
    # ------------------------------------------------------------------

    def _build_docker_command(self, indir: Path) -> list[str]:
        """Build the docker run argv list.

        Constructs the full ``docker run`` command that:
        1. Starts Neo4j.
        2. Polls Neo4j readiness (no fixed sleep).
        3. Launches the Transyt JAR.

        Args:
            indir: Host-side input directory, bind-mounted at
                ``/workdir/processingDir`` inside the container.

        Returns:
            List of strings forming the complete ``docker run`` command.
        """
        neo4j_poll = (
            "i=0; "
            f"while [ $i -lt {self._neo4j_timeout} ]; do "
            "curl -fsS http://localhost:7474/ > /dev/null 2>&1 && break; "
            "sleep 1; i=$((i+1)); "
            "done"
        )
        jar_cmd = (
            "java "
            "--add-exports java.base/jdk.internal.misc=ALL-UNNAMED "
            "-Dio.netty.tryReflectionSetAccessible=true "
            "-Dworkdir=/workdir "
            "-Xmx4096m "
            "-jar /opt/transyt/transyt.jar 3 /workdir/processingDir/"
        )
        inner_script = f"neo4j start && {neo4j_poll} && {jar_cmd}"

        return [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{indir}:/workdir/processingDir",
            "--entrypoint",
            "bash",
            self._docker_image,
            "-lc",
            inner_script,
        ]

    def _run_docker(self, indir: Path) -> tuple[str, int]:
        """Execute the Docker container and return (command_str, exit_code).

        Args:
            indir: Host-side input directory.

        Returns:
            A tuple of (shlex-quoted command string, docker exit code).
            Exit code 8 indicates no resolvable taxonomy (empty result).

        Note:
            The container is run with a generous timeout based on the
            neo4j_timeout setting plus a fixed overhead for JAR startup.
            Total timeout = neo4j_timeout * 2 + 300 seconds.
        """
        argv = self._build_docker_command(indir)
        cmd_str = shlex.join(argv)
        timeout = self._neo4j_timeout * 2 + 300

        proc = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
        )
        return cmd_str, proc.returncode

    # ------------------------------------------------------------------
    # Results parsing
    # ------------------------------------------------------------------

    def _parse_results(
        self,
        xml_path: Path,
        ref_path: Path,
        input_protein_ids: list[str],
    ) -> list[AnnotationRecord]:
        """Parse Transyt output files and build AnnotationRecords.

        Delegates to the pure parse functions, then assembles records.

        Args:
            xml_path: Path to ``results/transyt.xml``.
            ref_path: Path to ``results/reactions_references.txt``.
            input_protein_ids: Caller's original gene ids (used to filter
                output to submitted genes only).

        Returns:
            List of AnnotationRecord objects.
        """
        gene_to_rxns = _parse_transyt_xml(xml_path)
        rxn_to_tc = _parse_reaction_tc(xml_path)
        rxn_to_msrxn_cpds = _parse_reactions_references(ref_path)
        return _build_annotation_records(
            gene_to_rxns, rxn_to_tc, rxn_to_msrxn_cpds, input_protein_ids
        )

    # ------------------------------------------------------------------
    # Provenance helper
    # ------------------------------------------------------------------

    def _get_image_digest(self) -> str:
        """Return the Docker image digest for the configured image.

        Returns:
            Image digest string (e.g. ``"sha256:abc123..."``), or empty
            string on failure.
        """
        try:
            result = subprocess.run(
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    "{{index .RepoDigests 0}}",
                    self._docker_image,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
        return ""


# ---------------------------------------------------------------------------
# Convenience re-exports (so callers only need to import from transyt_utils)
# ---------------------------------------------------------------------------

__all__ = [
    "TransytUtils",
    # Parse helpers exposed for testing
    "_parse_transyt_xml",
    "_parse_reaction_tc",
    "_parse_reactions_references",
    "_build_annotation_records",
]
