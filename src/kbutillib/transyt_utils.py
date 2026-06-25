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

# SBML namespaces used in Transyt output.  The current TranSyT (v1.1) writes
# SBML Level 3 Version 2 with the fbc package; gene products and gene-product
# associations live in the fbc namespace (fbc:listOfGeneProducts,
# fbc:geneProduct fbc:id/fbc:label, fbc:geneProductRef fbc:geneProduct).
_SBML_NS = "http://www.sbml.org/sbml/level3/version2/core"
_SBML_FBC_NS = "http://www.sbml.org/sbml/level3/version1/fbc/version2"
_SBML_HTML_NS = "http://www.w3.org/1999/xhtml"

# ModelSEED species id pattern, e.g. "M_cpd00382_e0" → ("cpd00382", "e0").
_MS_SPECIES_RE = re.compile(r"^M_(cpd\d+)_([a-z]\d+)$")

# Transyt exit code for "no resolvable taxonomy"
_EXIT_NO_TAXONOMY = 8


def _local_tag(elem) -> str:
    """Return an element's local (namespace-stripped) tag name."""
    return elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag


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

    def _local(elem) -> str:
        """Local (namespace-stripped) tag name of an element."""
        return elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag

    def _fbc_attr(elem, name: str) -> str:
        """fbc-namespaced attribute, falling back to the bare attribute."""
        return elem.get(f"{{{_SBML_FBC_NS}}}{name}", elem.get(name, ""))

    # Build geneProduct id (e.g. "G_P02920") → caller label (e.g. "P02920").
    # TranSyT writes these under fbc:listOfGeneProducts as
    #   <fbc:geneProduct fbc:id="G_x" fbc:label="x" fbc:name="x"/>
    id_to_label: dict[str, str] = {}
    for elem in root.iter():
        if _local(elem) != "geneProduct":
            continue
        gp_id = _fbc_attr(elem, "id")
        label = _fbc_attr(elem, "label") or _fbc_attr(elem, "name") or gp_id
        if gp_id:
            id_to_label[gp_id] = label

    # Collect gene → reactions.  In L3V2/fbc the gene-product association is
    # NESTED inside each <reaction> as fbc:geneProductAssociation containing one
    # or more fbc:geneProductRef (possibly under fbc:and / fbc:or operators), so
    # walk every reaction and gather all geneProductRef descendants.
    gene_to_rxns: dict[str, list[str]] = {}
    for rxn in root.iter():
        if _local(rxn) != "reaction":
            continue
        rxn_id = rxn.get("id", "")
        if not rxn_id:
            continue
        for ref in rxn.iter():
            if _local(ref) != "geneProductRef":
                continue
            gp_id = _fbc_attr(ref, "geneProduct")
            gene_name = id_to_label.get(gp_id, gp_id)
            if gene_name:
                rxns = gene_to_rxns.setdefault(gene_name, [])
                if rxn_id not in rxns:
                    rxns.append(rxn_id)

    return gene_to_rxns


def _parse_species(xml_path: Path) -> dict[str, dict[str, Any]]:
    """Parse listOfSpecies → species id → compound info.

    TranSyT species ids look like ``M_cpd00382_e0`` (ModelSEED compound +
    compartment).  Returns, per species id::

        {"cpd": "cpd00382" | None, "compartment": "e0",
         "name": "<display name>", "modelseed": bool}

    ``modelseed`` is False for species whose id is not a ModelSEED compound
    (then ``cpd`` is None and callers fall back to the name/id).
    """
    try:
        tree = ET.parse(str(xml_path))
    except ET.ParseError:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for elem in tree.getroot().iter():
        if _local_tag(elem) != "species":
            continue
        sid = elem.get("id", "")
        if not sid:
            continue
        name = elem.get("name", "")
        m = _MS_SPECIES_RE.match(sid)
        if m:
            out[sid] = {"cpd": m.group(1), "compartment": m.group(2),
                        "name": name, "modelseed": True}
        else:
            out[sid] = {"cpd": None, "compartment": elem.get("compartment", ""),
                        "name": name, "modelseed": False}
    return out


def _parse_reactions(xml_path: Path) -> dict[str, dict[str, Any]]:
    """Parse listOfReactions → reaction id → stoichiometry + direction.

    Returns, per reaction id::

        {"reactants": [(species_id, stoich_str), ...],
         "products":  [(species_id, stoich_str), ...],
         "reversible": bool}
    """
    try:
        tree = ET.parse(str(xml_path))
    except ET.ParseError:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for rxn in tree.getroot().iter():
        if _local_tag(rxn) != "reaction":
            continue
        rid = rxn.get("id", "")
        if not rid:
            continue
        info: dict[str, Any] = {
            "reactants": [], "products": [],
            "reversible": rxn.get("reversible", "false") == "true",
        }
        for side in rxn:
            key = {
                "listOfReactants": "reactants",
                "listOfProducts": "products",
            }.get(_local_tag(side))
            if key is None:
                continue
            for sref in side:
                if _local_tag(sref) != "speciesReference":
                    continue
                sp = sref.get("species", "")
                if sp:
                    info[key].append((sp, sref.get("stoichiometry", "1")))
        out[rid] = info
    return out


def _parse_scores_method1(scores_path: Path) -> dict[str, list[tuple[str, str]]]:
    """Parse scoresMethod1.txt → gene → [(tc_number, evalue), ...].

    Format (per gene)::

        >P69786
        4.A.1.1.9 - Evalue: 2.72E-149\t[TZ4900020, ...]
        4.A.1.1.1 - Evalue: 0.0\t[TZ4900020, ...]

    The TC numbers are the gene's predicted transporter families; this file is
    the ONLY place TranSyT emits them (they are not present in the SBML).
    """
    out: dict[str, list[tuple[str, str]]] = {}
    try:
        text = Path(scores_path).read_text(encoding="utf-8")
    except OSError:
        return out
    gene: str | None = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith(">"):
            gene = line[1:].strip()
            out.setdefault(gene, [])
            continue
        if gene is None:
            continue
        m = re.match(r"\s*(\S+)\s*-\s*Evalue:\s*(\S+)", line)
        if m:
            out[gene].append((m.group(1).strip(), m.group(2).strip()))
    return out


def _species_token(species_id: str, species: dict[str, dict[str, Any]]) -> str:
    """Render a species as ``cpd00382[e0]`` (ModelSEED) or name/id otherwise."""
    info = species.get(species_id)
    if info is None:
        return species_id
    comp = info.get("compartment") or ""
    if info["modelseed"] and info["cpd"]:
        return f"{info['cpd']}[{comp}]" if comp else info["cpd"]
    label = info.get("name") or species_id
    return f"{label}[{comp}]" if comp else label


def _reaction_equation(rxn: dict[str, Any],
                       species: dict[str, dict[str, Any]]) -> str:
    """Build a human-readable equation in ModelSEED compounds where possible.

    e.g. ``cpd00382[e0] + cpd00067[e0] <=> cpd00067[c0] + cpd00382[c0]``.
    """
    def _side(refs: list[tuple[str, str]]) -> str:
        parts: list[str] = []
        for sid, stoich in refs:
            token = _species_token(sid, species)
            try:
                is_one = float(stoich) == 1.0
            except ValueError:
                is_one = True
            parts.append(token if is_one else f"({stoich}) {token}")
        return " + ".join(parts)
    arrow = " <=> " if rxn.get("reversible") else " => "
    return _side(rxn.get("reactants", [])) + arrow + _side(rxn.get("products", []))


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
        # TranSyT writes the ModelSEED rxn id(s) bracketed, e.g. "[rxn05643]";
        # strip the brackets so the stored id is the bare "rxn05643".
        if msrxn:
            msrxn = msrxn.strip("[]").strip()
        if msrxn == "" or msrxn is None:
            msrxn = None
        cpds_raw = parts[2].strip() if len(parts) > 2 else ""
        cpds = [c.strip() for c in cpds_raw.split(";") if c.strip()]
        result[rxn_id] = (msrxn, cpds)

    return result


def _build_annotation_records(
    gene_to_rxns: dict[str, list[str]],
    gene_to_tc: dict[str, list[tuple[str, str]]],
    reactions: dict[str, dict[str, Any]],
    species: dict[str, dict[str, Any]],
    rxn_to_msrxn_cpds: dict[str, tuple[str | None, list[str]]],
    input_protein_ids: list[str],
) -> list[AnnotationRecord]:
    """Combine parsed TranSyT outputs into AnnotationRecord list.

    For each submitted gene, emits as much information as TranSyT provides:

    - ``Term("TC", <tc_number>, "transporter", {"evalue": ...})`` per predicted
      transporter family (from scoresMethod1.txt).
    - Per associated transport reaction, one reaction Term whose ``value`` is the
      full equation in ModelSEED compounds, e.g.
      ``cpd00382[e0] + cpd00067[e0] <=> cpd00067[c0] + cpd00382[c0]``:
        * ``Term("MSRXN", <modelseed_rxn_id>, <equation>, ...)`` when the
          reaction maps to a ModelSEED reaction (reactions_references.txt);
        * otherwise ``Term("TRANSYT_RXN", <transyt_rxn_id>, <equation>, ...)`` so
          a reaction is never dropped just for lacking a ModelSEED id.
    - ``Term("MSCPD", <cpd_id>, <name>, {"compartment": ...})`` per ModelSEED
      compound involved; ``Term("CPD", <name_or_id>, ...)`` for any
      non-ModelSEED compound.

    Only genes whose ids appear in *input_protein_ids* are included.

    Args:
        gene_to_rxns: Gene id → list of TranSyT reaction ids (from the SBML).
        gene_to_tc: Gene id → list of (TC number, e-value) (scoresMethod1.txt).
        reactions: Reaction id → {reactants, products, reversible}.
        species: Species id → {cpd, compartment, name, modelseed}.
        rxn_to_msrxn_cpds: TranSyT reaction id → (ModelSEED rxn id, [cpds]).
        input_protein_ids: The caller's original protein ids.

    Returns:
        List of AnnotationRecord (one per submitted gene with at least one Term).
    """
    input_ids = set(input_protein_ids)
    records: list[AnnotationRecord] = []

    for gene in sorted(set(gene_to_rxns) | set(gene_to_tc)):
        if gene not in input_ids:
            continue

        terms: list[Term] = []

        seen_tc: set[str] = set()
        for tc, evalue in gene_to_tc.get(gene, []):
            if tc in seen_tc:
                continue
            seen_tc.add(tc)
            terms.append(Term(namespace="TC", id=tc, value="transporter",
                              evidence={"evalue": evalue}))

        seen_rxn: set[str] = set()
        seen_cpd: set[str] = set()
        for rxn_id in gene_to_rxns.get(gene, []):
            rxn = reactions.get(rxn_id)
            if rxn is None:
                continue
            equation = _reaction_equation(rxn, species)
            mapping = rxn_to_msrxn_cpds.get(rxn_id)
            msrxn = mapping[0] if mapping else None
            if msrxn:
                if msrxn not in seen_rxn:
                    seen_rxn.add(msrxn)
                    terms.append(Term(
                        namespace="MSRXN", id=msrxn, value=equation,
                        evidence={"transyt_rxn_id": rxn_id,
                                  "reversible": rxn["reversible"]},
                    ))
            elif rxn_id not in seen_rxn:
                seen_rxn.add(rxn_id)
                terms.append(Term(
                    namespace="TRANSYT_RXN", id=rxn_id, value=equation,
                    evidence={"reversible": rxn["reversible"]},
                ))

            for sid, _stoich in rxn["reactants"] + rxn["products"]:
                info = species.get(sid)
                if info is None:
                    continue
                if info["modelseed"] and info["cpd"]:
                    if info["cpd"] in seen_cpd:
                        continue
                    seen_cpd.add(info["cpd"])
                    terms.append(Term(
                        namespace="MSCPD", id=info["cpd"],
                        value=info["name"] or info["cpd"],
                        evidence={"compartment": info["compartment"]},
                    ))
                else:
                    key = info.get("name") or sid
                    if key in seen_cpd:
                        continue
                    seen_cpd.add(key)
                    terms.append(Term(
                        namespace="CPD", id=key, value=info.get("name") or sid,
                        evidence={"compartment": info.get("compartment", "")},
                    ))

        if terms:
            records.append(AnnotationRecord(gene_id=gene, terms=terms))

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
        # Memory split between the JVM and the in-container neo4j.  Defaults suit
        # a Docker VM with >=6 GB; on a smaller VM set these lower (and/or raise
        # Docker Desktop's memory) or the JVM is OOM-killed mid-run.
        self._jvm_xmx: str = self.get_config_value("transyt.jvm_xmx", default="4096m")
        self._neo4j_heap: str = self.get_config_value(
            "transyt.neo4j_heap", default="1g"
        )
        self._neo4j_pagecache: str = self.get_config_value(
            "transyt.neo4j_pagecache", default="512m"
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
                scores_path = results_dir / "scoresMethod1.txt"
                # The SBML is the primary output; reactions_references.txt and
                # scoresMethod1.txt are optional enrichments (handled as missing
                # by their parsers), so gate only on the SBML.
                if xml_path.exists():
                    records = self._parse_results(
                        xml_path, ref_path, scores_path, list(proteins.keys())
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

        # Write params.txt.  TranSyT parses this file with a TAB delimiter
        # (FilesUtils.readMapFromFile splits each line on "\t" and drops lines
        # without 2 fields), so params MUST be tab-separated.  Writing "key=value"
        # silently yields an empty param map — taxID then resolves to null and
        # TranSyT NPEs in the taxonomy lookup before doing any work.
        params_lines: list[str] = [
            f"taxID\t{tax_id}",
            f"reference_database\t{reference_database}",
        ]
        for k, v in _DEFAULT_SCORING.items():
            params_lines.append(f"{k}\t{v}")
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
            f"-Xmx{self._jvm_xmx} "
            "-jar /opt/transyt/transyt.jar 3 /workdir/processingDir/"
        )
        # neo4j and the JVM share the container's memory.  Bound the neo4j heap
        # so it leaves room for the JVM; both are configurable.  On a small
        # Docker VM (e.g. 2 GB) the defaults must be reduced or the JVM is
        # OOM-killed (exit 137) mid graph-query.
        neo4j_env = (
            f"NEO4J_dbms_memory_heap_max__size={self._neo4j_heap} "
            f"NEO4J_dbms_memory_pagecache_size={self._neo4j_pagecache} "
        )
        inner_script = f"{neo4j_env}neo4j start && {neo4j_poll} && {jar_cmd}"

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
        scores_path: Path,
        input_protein_ids: list[str],
    ) -> list[AnnotationRecord]:
        """Parse TranSyT output files and build AnnotationRecords.

        Delegates to the pure parse functions, then assembles records.

        Args:
            xml_path: Path to ``results/transyt.xml`` (gene→reaction links,
                reaction stoichiometry, species/compound info).
            ref_path: Path to ``results/reactions_references.txt`` (TranSyT
                reaction id → ModelSEED reaction id).
            scores_path: Path to ``results/scoresMethod1.txt`` (gene → predicted
                TC families + e-values).
            input_protein_ids: Caller's original gene ids (used to filter
                output to submitted genes only).

        Returns:
            List of AnnotationRecord objects.
        """
        gene_to_rxns = _parse_transyt_xml(xml_path)
        gene_to_tc = _parse_scores_method1(scores_path)
        reactions = _parse_reactions(xml_path)
        species = _parse_species(xml_path)
        rxn_to_msrxn_cpds = _parse_reactions_references(ref_path)
        return _build_annotation_records(
            gene_to_rxns, gene_to_tc, reactions, species,
            rxn_to_msrxn_cpds, input_protein_ids,
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
    "_parse_species",
    "_parse_reactions",
    "_parse_scores_method1",
    "_parse_reactions_references",
    "_reaction_equation",
    "_build_annotation_records",
]
