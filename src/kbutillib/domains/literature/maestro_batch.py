"""Layer 3 of the gene-literature stack: batched Maestro research.

Layers 1 and 2 run *here*: this process searches, this process calls a model.
Layer 3 does neither.  It hands a batch of genes to a Maestro **researcher**
task on another machine, which does the reading with its own network and its
own tools, writes its answers into the repository as a JSON file and commits
them on its own task branch.  This module is the two ends of that handoff:

* :func:`submit_gene_facts_batches` -- chunk the genes, assemble one
  SELF-CONTAINED prompt per batch, and submit one ``maestro submit`` per
  batch.  It returns a :class:`BatchHandle` per batch, which is a plain
  pydantic model: JSON-serialisable, so a caller may write the handles down
  and collect days later from another process.
* :func:`collect_gene_facts_batches` -- read each batch's committed output
  with ``git show <branch>:<path>``, verify every citation, and return
  ``{gene_id: GeneFacts}`` in exactly the layer-2 shape, cached under the
  layer-2 key scheme with ``api_type="maestro"``.

Three properties this module is built around
--------------------------------------------

**The worker cannot read the PRD, this file, or anything else we know.**  So
the prompt carries everything: the genes and their layer-1 paper slices as
JSON, the output JSON schema and the fact types, the evidence-class rules,
which KBUtilLib tools exist on the worker (and, pointedly, which do *not*),
the citation discipline, the environment it must verify before spending
anything, and the file it must write and commit.  A prompt that grew past
:data:`MAX_PROMPT_BYTES` is a prompt we cannot vouch for, so assembly fails
**before any batch is submitted** rather than after some are.

**``maestro status`` is not evidence.**  Collection never asks Maestro
whether a task succeeded; it asks git whether the output file exists on the
branch.  A task that reported success without committing an output has, for
our purposes, not produced one -- and a task still running looks exactly the
same, which is why the single status for "no readable output" is
``pending_or_failed``.

**Nothing the worker says is trusted because the worker said it.**  The
document is validated against :data:`~kbutillib.domains.literature.facts.FACTS_JSON_SCHEMA`
structurally, foreign and duplicated gene ids are refused, each assigned gene
that is absent is reported on its own, and every citation goes through
:func:`~kbutillib.domains.literature.facts.verify_facts` in **web mode** with
an EMPTY trusted set -- so each cited id has to resolve against Europe PMC.
We never handed the worker paper text, so there is no closed set of ids it
could legitimately have copied from: resolution is the only check available.

Annotation note: pydantic evaluates field annotations at class-creation time
and this repository still builds on Python 3.9, so model fields use
``Optional[...]``/``Union[...]`` rather than PEP 604 unions.

Example::

    from kbutillib.domains.literature.maestro_batch import (
        collect_gene_facts_batches,
        submit_gene_facts_batches,
    )

    handles = submit_gene_facts_batches(
        genes,
        {"name": "Acinetobacter baylyi", "synonyms": ["ADP1"]},
        papers_by_gene=annotation_by_gene,
        repo="KBUtilLib",
        worker_env={
            "kbutillib_revision": "06214d3",
            "cache_path": "/home/chenry/.kbutillib/gene_literature.sqlite",
            "paperblast_data_dir": "/scratch/chenry/paperblast",
            "mmseqs_executable": "/home/chenry/.local/bin/mmseqs",
            "snapshot_digest": "sha256:0e1d...",
        },
    )
    # ... later, once the tasks have run and committed ...
    facts = collect_gene_facts_batches(
        handles, repo_path="/home/chenry/Dropbox/Projects/KBUtilLib"
    )
"""

from __future__ import annotations

import collections.abc
import hashlib
import json
import subprocess
import uuid
from datetime import datetime, timezone
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from pydantic import BaseModel

from ...core.shared_env_utils import SharedEnvUtils
from .annotate import (
    EVIDENCE_CLASSES,
    Gene,
    GeneEntry,
    Organism,
    PaperAnnotation,
    PaperEntry,
    PaperIds,
)
from .cache import CACHE_PATH_CONFIG_KEY, LiteratureCache, resolve_cache_path
from .europepmc import normalise_paper_id

# The layer-2 key scheme, the layer-2 storage shape and the layer-2 verifier
# are IMPORTED rather than re-implemented.  "Cached under the same key scheme"
# is a requirement, and two copies of a hash recipe drift the first time
# either is touched -- at which point this layer's answers and layer 2's stop
# sharing a cache without anything failing.
from .facts import (
    FACT_TYPES,
    FACTS_JSON_SCHEMA,
    GENE_FACTS_TABLE,
    GeneFacts,
    PapersUsed,
    _cache_key,
    _read_papers,
    _storable,
    verify_facts,
)

__all__ = [
    "BATCH_SCHEMA_VERSION",
    "COLLECT_ERROR_KINDS",
    "DEFAULT_BACKEND",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_MACHINE",
    "DEFAULT_MAESTRO_EXE",
    "DEFAULT_OUT_DIR",
    "DEFAULT_TIMEOUT",
    "FORBIDDEN_MAESTRO_FLAGS",
    "MANIFEST_VERSION",
    "MAESTRO_API_TYPE",
    "MAESTRO_PROMPT_VERSION",
    "MAX_PROMPT_BYTES",
    "SUBMIT_CALL_TIMEOUT",
    "VERIFICATION_MODE",
    "WORKER_ENV_KEYS",
    "BatchHandle",
    "BatchManifest",
    "MaestroBatchUtils",
    "MaestroBatchUtilsImpl",
    "build_batch_prompt",
    "collect_gene_facts_batches",
    "submit_gene_facts_batches",
]

#: Bumped when :func:`build_batch_prompt` changes in a way that would change
#: a worker's answer.  Recorded in the manifest and in the cache key's
#: provenance, so an old answer is never served for a new prompt.
MAESTRO_PROMPT_VERSION = "1"

#: Bumped when :class:`BatchManifest` changes shape.
MANIFEST_VERSION = "1"

#: ``schema_version`` the worker must stamp on its output document.
BATCH_SCHEMA_VERSION = "1"

#: ``api_type`` recorded on every :class:`~kbutillib.domains.literature.facts.GeneFacts`
#: this module produces, and part of its cache key.  A Maestro answer and a
#: direct layer-2 answer are different things and must never share a row.
MAESTRO_API_TYPE = "maestro"

#: Collection always verifies in web mode: the worker searched the live web.
VERIFICATION_MODE = "web"

DEFAULT_MACHINE = "h100"
DEFAULT_BACKEND = "codex"
DEFAULT_BATCH_SIZE = 25
DEFAULT_OUT_DIR = "agent-io/gene-facts"
DEFAULT_TIMEOUT = 3600
DEFAULT_MAESTRO_EXE = "maestro"

#: Seconds allowed for the ``maestro submit`` call itself.  Submission queues
#: a task and returns; it is NOT the task's own ``--timeout``.
SUBMIT_CALL_TIMEOUT = 300

#: Keys :func:`submit_gene_facts_batches` requires in ``worker_env``.  The
#: mapping is not advisory: a worker whose kbutillib revision or data paths
#: differ from these produces answers attributed to a snapshot that never
#: computed them, so a batch submitted without the full set is refused here
#: rather than discovered later.
WORKER_ENV_KEYS: Tuple[str, ...] = (
    "kbutillib_revision",
    "cache_path",
    "paperblast_data_dir",
    "mmseqs_executable",
    "snapshot_digest",
)

#: Flags that must NEVER reach ``maestro submit`` from here.  A researcher
#: batch is unreviewed model output; merging it automatically would put
#: unverified facts on a branch somebody trusts.  Pinned by a test, and
#: asserted in :meth:`MaestroBatchUtils.submit_gene_facts_batches` as well,
#: because a flag added by a later edit must fail loudly and locally.
FORBIDDEN_MAESTRO_FLAGS: Tuple[str, ...] = (
    "--auto-review",
    "--auto-merge",
    "--auto-merge-on-verdict",
)

#: Ceiling on one assembled prompt.  A prompt beyond this is not trimmed: it
#: is refused, before any batch of the call is submitted.
MAX_PROMPT_BYTES = 100 * 1024

#: The reasons collection reports an error for a gene, each prefixed onto
#: :attr:`~kbutillib.domains.literature.facts.GeneFacts.error` in the
#: ``"<kind>: <message>"`` form layer 2 already uses.
COLLECT_ERROR_KINDS: Tuple[str, ...] = (
    # No readable output on the branch.  Covers "still running", "failed",
    # "committed nothing" and "committed something unparseable" -- git cannot
    # tell them apart and neither can we.
    "pending_or_failed",
    # The document is readable but stamped with another batch's id.
    "batch_mismatch",
    # The document parsed and this assigned gene is simply not in it.
    "missing_from_output",
    # The same gene id appeared more than once in one document.
    "duplicate_in_output",
    # The entry is present but does not match the facts schema.
    "invalid_result",
    # The worker itself reported a failure for this gene.
    "worker_error",
)

#: Statuses a worker entry may declare.  Anything else is read as ``"ok"``
#: and left to verification, which is the part that actually decides whether
#: the entry carries anything.
_WORKER_STATUSES: Tuple[str, ...] = ("ok", "no_evidence", "error")


# ── models ───────────────────────────────────────────────────────────────


class BatchManifest(BaseModel):
    """Everything one batch was submitted WITH, carried by its handle.

    The point of the manifest is that collection needs nothing but the
    handles and a repository path.  The organism, the normalised genes, the
    layer-1 paper slices, the worker environment and the backend are all
    recorded here at submission time, so a collector running days later in
    another process reconstructs the same cache key without being told
    anything twice -- and cannot be told something *different* by accident.

    Attributes:
        manifest_version: :data:`MANIFEST_VERSION` at submission.
        prompt_version: :data:`MAESTRO_PROMPT_VERSION` at submission.
        genes: The batch's genes, normalised to
            :class:`~kbutillib.domains.literature.annotate.Gene`.
        organism: The organism they come from.
        paper_slices: ``{gene_id: PaperAnnotation}`` -- layer 1's output
            restricted to that one gene.  A gene the caller gave no papers
            for is absent, not empty.
        worker_env: The required environment, every key of
            :data:`WORKER_ENV_KEYS` present and non-empty.
        backend: The Maestro backend (also recorded as ``model`` on every
            resulting ``GeneFacts``).
        model: What to report as the model.  Equal to *backend*: a Maestro
            backend is the only model identity we have.
        machine: Target machine.
        repo: Target repository.
        timeout: The task timeout passed to ``maestro submit``.
        verification_mode: Always ``"web"`` -- recorded rather than assumed,
            because it is what the cache key means.
        prompt_sha256: Hash of the exact prompt text submitted.  Provenance,
            and part of the cache key: two batches whose genes match but
            whose prompts differ are two different questions.
    """

    manifest_version: str = MANIFEST_VERSION
    prompt_version: str = MAESTRO_PROMPT_VERSION
    genes: List[Gene] = []
    organism: Organism
    paper_slices: Dict[str, PaperAnnotation] = {}
    worker_env: Dict[str, str] = {}
    backend: str = DEFAULT_BACKEND
    model: str = DEFAULT_BACKEND
    machine: str = DEFAULT_MACHINE
    repo: str = ""
    timeout: int = DEFAULT_TIMEOUT
    verification_mode: str = VERIFICATION_MODE
    prompt_sha256: str = ""


class BatchHandle(BaseModel):
    """One submitted batch, and everything needed to collect it.

    A plain pydantic model on purpose: ``handle.model_dump()`` /
    ``model_dump_json()`` round-trips, so a caller may persist the handles
    and collect from a different process, or a different day.

    Attributes:
        batch_id: ``uuid4().hex[:12]``.  Names the output file and is stamped
            in the document, so an output can be matched to its batch.
        task_id: Maestro's task id, from the submit call's JSON.
        branch: The task branch Maestro will commit on (``branch_name`` in
            the submit JSON).  Collection reads the output from HERE, never
            from a working tree.
        gene_ids: The genes assigned to this batch, in order.  Collection
            reports one result per id in this list and nothing else.
        output_path: Repository-relative path the worker must write.
        submitted_at: UTC ISO-8601 timestamp of the submit call.
        repo: Target repository.
        machine: Target machine.
        backend: Maestro backend.
        input_manifest: :class:`BatchManifest` -- see there.
    """

    batch_id: str
    task_id: str
    branch: str
    gene_ids: List[str] = []
    output_path: str
    submitted_at: str
    repo: str
    machine: str
    backend: str
    input_manifest: BatchManifest


# ── small helpers ────────────────────────────────────────────────────────


def _sha256(text: str) -> str:
    """Hex SHA-256 of *text*."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _utc_now_iso() -> str:
    """Now, UTC, ISO-8601 -- the ``submitted_at`` stamp."""
    return datetime.now(timezone.utc).isoformat()


def _as_gene(entry: Union[Gene, Mapping[str, Any]]) -> Gene:
    """One gene, normalised."""
    return entry if isinstance(entry, Gene) else Gene(**dict(entry))


def _as_organism(entry: Union[Organism, Mapping[str, Any]]) -> Organism:
    """The organism, normalised."""
    return entry if isinstance(entry, Organism) else Organism(**dict(entry))


def _normalise_genes(genes: Iterable[Union[Gene, Mapping[str, Any]]]) -> List[Gene]:
    """Normalise the gene list, refusing an empty or repeated id.

    Two genes sharing an id would collide in the output document and in the
    returned mapping -- one would overwrite the other's answer -- so it is
    refused before anything is spent.

    Raises:
        ValueError: a gene has no id, or two genes share one.
    """
    result: List[Gene] = []
    seen: set = set()
    for entry in genes or []:
        gene = _as_gene(entry)
        if not str(gene.id or "").strip():
            raise ValueError("every gene needs a non-empty id")
        if gene.id in seen:
            raise ValueError(f"duplicate gene id {gene.id!r} in the submitted genes")
        seen.add(gene.id)
        result.append(gene)
    return result


def _validated_worker_env(worker_env: Any) -> Dict[str, str]:
    """Check ``worker_env`` carries every key of :data:`WORKER_ENV_KEYS`.

    Args:
        worker_env: The caller's mapping.

    Returns:
        ``{key: str(value)}`` for exactly :data:`WORKER_ENV_KEYS`, so the
        manifest records the required set and nothing else.

    Raises:
        ValueError: it is not a mapping, or a required key is missing or
            empty.  Incomplete is refused rather than defaulted: a default
            here would be this process guessing at another machine's paths.
    """
    if not isinstance(worker_env, collections.abc.Mapping):
        raise ValueError(
            "worker_env must be a mapping carrying "
            + ", ".join(WORKER_ENV_KEYS)
            + f" -- got {type(worker_env).__name__}"
        )
    missing = [
        key
        for key in WORKER_ENV_KEYS
        if not str(worker_env.get(key) or "").strip()
    ]
    if missing:
        raise ValueError(
            "worker_env is incomplete: missing or empty "
            + ", ".join(missing)
            + ". Every one of "
            + ", ".join(WORKER_ENV_KEYS)
            + " is required, because the worker checks them before it spends "
            "anything."
        )
    return {key: str(worker_env[key]) for key in WORKER_ENV_KEYS}


def _slice_for(
    gene: Gene, papers: Any
) -> Optional[PaperAnnotation]:
    """The layer-1 slice for one gene, as a one-gene annotation.

    Args:
        gene: The gene.
        papers: None, a
            :class:`~kbutillib.domains.literature.annotate.PaperAnnotation`
            (restricted here to this gene), a mapping that validates as one
            (a round-tripped annotation), or a sequence of publication ids.

    Returns:
        A one-gene :class:`PaperAnnotation`, or None when there is nothing.

    Raises:
        TypeError: *papers* is a bare string or some other type.
    """
    if papers is None:
        return None
    if isinstance(papers, PaperAnnotation):
        return papers.for_gene(gene.id) if gene.id in papers.genes else papers
    if isinstance(papers, (str, bytes)):
        raise TypeError(
            "a gene's papers must be None, a sequence of publication ids or "
            "a PaperAnnotation, not a single string"
        )
    if isinstance(papers, collections.abc.Mapping):
        return PaperAnnotation.model_validate(dict(papers)).for_gene(gene.id)
    if isinstance(papers, collections.abc.Iterable):
        entries: Dict[str, PaperEntry] = {}
        order: List[str] = []
        for raw in papers:
            identifier = normalise_paper_id(raw)
            if identifier is None or identifier in entries:
                continue
            entries[identifier] = PaperEntry(
                ids=_ids_from_canonical(identifier), genes={gene.id: []}
            )
            order.append(identifier)
        return PaperAnnotation(
            papers=entries, genes={gene.id: GeneEntry(paper_ids=order)}
        )
    raise TypeError(
        "a gene's papers must be None, a sequence of publication ids or a "
        f"PaperAnnotation, not {type(papers).__name__}"
    )


def _ids_from_canonical(canonical: str) -> PaperIds:
    """Split a canonical id (``PMID:1``, ``PMCID:PMC1``, ``DOI:10.1/x``)."""
    prefix, _, rest = canonical.partition(":")
    kind = prefix.upper()
    if kind == "PMID":
        return PaperIds(pmid=rest)
    if kind == "PMCID":
        return PaperIds(pmcid=rest)
    if kind == "DOI":
        return PaperIds(doi=rest)
    return PaperIds()


def _schema_problems(schema: Mapping[str, Any], value: Any, path: str = "$") -> List[str]:
    """Structural problems in *value* against a JSON-schema subset.

    Supports ``type`` (object/array/string/number/integer/boolean),
    ``required``, ``properties`` and ``items`` -- which is the whole of
    :data:`~kbutillib.domains.literature.facts.FACTS_JSON_SCHEMA`'s structure
    and needs no dependency this repository does not already have.

    ``enum`` is deliberately NOT enforced.  The enumerations in that schema
    are the fact types and the evidence classes, and both are already checked
    per fact by :func:`~kbutillib.domains.literature.facts.verify_facts` --
    which rejects the one offending fact and keeps the rest.  Enforcing them
    here would make one bad ``type`` discard every good fact beside it.

    Args:
        schema: The schema fragment.
        value: The value to check.
        path: Where we are, for the message.

    Returns:
        A list of human-readable problems; empty means structurally valid.
    """
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            return [f"{path}: expected an object"]
        problems: List[str] = []
        for key in schema.get("required") or []:
            if key not in value:
                problems.append(f"{path}.{key}: required and absent")
        for key, sub in (schema.get("properties") or {}).items():
            if key in value and isinstance(sub, collections.abc.Mapping):
                problems.extend(_schema_problems(sub, value[key], f"{path}.{key}"))
        return problems
    if expected == "array":
        if not isinstance(value, list):
            return [f"{path}: expected an array"]
        items = schema.get("items")
        if not isinstance(items, collections.abc.Mapping):
            return []
        problems = []
        for index, entry in enumerate(value):
            problems.extend(_schema_problems(items, entry, f"{path}[{index}]"))
        return problems
    if expected == "string":
        if not isinstance(value, str):
            return [f"{path}: expected a string"]
        return []
    if expected in ("number", "integer"):
        if isinstance(value, bool) or not isinstance(
            value, int if expected == "integer" else (int, float)
        ):
            return [f"{path}: expected {'an integer' if expected == 'integer' else 'a number'}"]
        return []
    if expected == "boolean":
        return [] if isinstance(value, bool) else [f"{path}: expected a boolean"]
    return []


def _json_with_duplicate_keys(text: str) -> Tuple[Any, List[str]]:
    """Parse JSON, reporting any object key that appeared twice.

    ``json.loads`` keeps the LAST of two identical keys and says nothing.
    For a document whose keys are gene ids that is the worst possible
    behaviour -- a worker that answered one gene twice would have one answer
    silently discarded -- so duplicates are detected and reported, and the
    affected gene is failed rather than guessed at.

    Args:
        text: The document text.

    Returns:
        ``(parsed, duplicate_keys)``.

    Raises:
        ValueError: *text* is not JSON (``json.JSONDecodeError`` is one).
    """
    duplicates: List[str] = []

    def hook(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                duplicates.append(key)
            result[key] = item
        return result

    return json.loads(text, object_pairs_hook=hook), duplicates


def _error_facts(
    gene_id: str,
    *,
    kind: str,
    message: str,
    manifest: BatchManifest,
    papers_used: Optional[PapersUsed] = None,
) -> GeneFacts:
    """A ``status="error"`` result for one gene, in the layer-2 shape.

    The error text is ``"<kind>: <message>"`` -- the same spelling layer 2's
    ``_finalise`` uses for a failed model call, because a caller that already
    switches on that prefix must not need a second rule for this layer.
    """
    return GeneFacts(
        gene_id=gene_id,
        status="error",
        mode=VERIFICATION_MODE,
        papers_used=papers_used or PapersUsed(),
        model=manifest.model,
        api_type=MAESTRO_API_TYPE,
        prompt_version=manifest.prompt_version,
        error=f"{kind}: {message}",
    )


# ── the prompt ───────────────────────────────────────────────────────────


def _gene_block(gene: Gene, annotation: Optional[PaperAnnotation]) -> Dict[str, Any]:
    """One gene plus its layer-1 slice, trimmed for the prompt.

    Nulls and empty collections are dropped: the worker reads this, and
    thirty ``"identity": null`` lines per paper are thirty lines of the
    prompt budget spent saying nothing.
    """
    block: Dict[str, Any] = {"gene_id": gene.id}
    if gene.aliases:
        block["aliases"] = list(gene.aliases)
    if gene.uniprot:
        block["uniprot"] = gene.uniprot
    if gene.sequence:
        block["protein_sequence"] = gene.sequence

    papers: List[Dict[str, Any]] = []
    if annotation is not None:
        entry = annotation.genes.get(gene.id)
        order = (
            entry.paper_ids
            if entry is not None and entry.paper_ids
            else list(annotation.papers)
        )
        for paper_id in order:
            paper = annotation.papers.get(paper_id)
            if paper is None:
                continue
            record: Dict[str, Any] = {"paper_id": paper_id}
            for field in ("title", "year", "journal"):
                value = getattr(paper, field, None)
                if value:
                    record[field] = value
            ids = {
                name: value
                for name, value in paper.ids.model_dump().items()
                if value
            }
            if ids:
                record["ids"] = ids
            evidence: List[Dict[str, Any]] = []
            for links in paper.genes.values():
                for item in links:
                    evidence.append(
                        {
                            name: value
                            for name, value in item.model_dump().items()
                            if value is not None
                        }
                    )
            if evidence:
                record["layer1_evidence"] = evidence
            papers.append(record)
    block["layer1_papers"] = papers
    return block


def build_batch_prompt(
    manifest: BatchManifest, *, batch_id: str, output_path: str
) -> str:
    """Assemble the SELF-CONTAINED prompt for one batch.

    The worker has no access to this repository's PRD, to this module, or to
    anything else the submitting process knows.  Everything it needs is in
    the returned text: the environment it must verify first, the genes and
    their layer-1 slices as JSON, which tools it has (and which it does not),
    the evidence-class and citation rules, the fact types, the exact output
    document shape, and the instruction to write AND COMMIT that document on
    its own task branch before it exits.

    Args:
        manifest: The batch's :class:`BatchManifest`.
        batch_id: The batch id, stamped in the document the worker writes.
        output_path: Repository-relative path the worker must write.

    Returns:
        The prompt text.
    """
    genes_json = json.dumps(
        [
            _gene_block(gene, manifest.paper_slices.get(gene.id))
            for gene in manifest.genes
        ],
        indent=2,
        sort_keys=True,
    )
    per_gene_schema = json.dumps(FACTS_JSON_SCHEMA, indent=2, sort_keys=True)
    worker_env_json = json.dumps(manifest.worker_env, indent=2, sort_keys=True)
    gene_ids = [gene.id for gene in manifest.genes]
    document_shape = json.dumps(
        {
            "schema_version": BATCH_SCHEMA_VERSION,
            "batch_id": batch_id,
            "results": {
                gene_ids[0] if gene_ids else "<gene_id>": {
                    "status": "ok",
                    "facts": ["<fact objects, per the schema above>"],
                    "notes": "<what you could not get to, in one or two lines>",
                }
            },
        },
        indent=2,
    )

    sections: List[str] = []

    sections.append(
        "\n".join(
            [
                "# Gene literature research batch",
                "",
                f"You are researching {len(manifest.genes)} gene(s) of "
                f"{manifest.organism.name}"
                + (
                    " (also known as "
                    + ", ".join(str(name) for name in manifest.organism.synonyms)
                    + ")"
                    if manifest.organism.synonyms
                    else ""
                )
                + (
                    f", NCBI taxon {manifest.organism.taxon_id}"
                    if manifest.organism.taxon_id
                    else ""
                )
                + ".",
                "",
                "For each assigned gene, extract what the scientific "
                "literature actually SAYS about it, as a list of typed "
                "statements each carrying at least one citation. Then write "
                "the result file described at the end of this prompt and "
                "COMMIT it on your task branch.",
                "",
                "This prompt is self-contained. Do not look for a PRD, a "
                "design document or a task plan: everything that defines the "
                "job is below. If something you need is genuinely absent, "
                "record that in the gene's `notes` rather than inventing it.",
                "",
                f"batch_id: {batch_id}",
                f"repository: {manifest.repo}    machine: {manifest.machine}"
                f"    backend: {manifest.backend}",
            ]
        )
    )

    sections.append(
        "\n".join(
            [
                "## STEP 0 -- verify your environment BEFORE you spend anything",
                "",
                "This batch was submitted against one specific KBUtilLib "
                "snapshot and one specific set of data paths. Answers "
                "computed against a different snapshot would be filed under "
                "a provenance that never produced them, so check first:",
                "",
                "```json",
                worker_env_json,
                "```",
                "",
                "Run, before any research:",
                "",
                "```bash",
                "python -c 'import kbutillib; print(kbutillib.__file__)'",
                "git -C <the kbutillib checkout> rev-parse --short HEAD",
                "```",
                "",
                "Check that:",
                "",
                "1. `import kbutillib` SUCCEEDS, and the checkout it came "
                f"from is at revision `{manifest.worker_env.get('kbutillib_revision', '')}`"
                " (a longer sha whose prefix matches is fine).",
                "2. `cache_path`'s parent directory exists and is writable.",
                "3. `paperblast_data_dir` exists, and `mmseqs_executable` "
                "exists and is executable -- these are what PaperBLAST needs.",
                "4. `snapshot_digest` matches the data snapshot you are "
                "about to read, if the snapshot records one.",
                "",
                "**If ANY of those does not hold, do not research anything.** "
                "Write the output document with an error entry for EVERY "
                "assigned gene:",
                "",
                "```json",
                '{"status": "error", "error": "environment mismatch: '
                '<exactly what differed>", "facts": []}',
                "```",
                "",
                "commit it, and exit. A wrong-environment batch reported as "
                "an error costs one task; a wrong-environment batch reported "
                "as facts corrupts the cache for everyone who reads it next.",
            ]
        )
    )

    sections.append(
        "\n".join(
            [
                "## The genes, with what layer 1 already found",
                "",
                "`layer1_papers` is what an earlier automated pass "
                "associated with the gene, with its evidence record. It is a "
                "starting point and nothing more: it may be incomplete, and "
                "a listed paper may turn out to say nothing useful. You are "
                "not restricted to these papers, and you are not obliged to "
                "use them.",
                "",
                "```json",
                genes_json,
                "```",
                "",
                "Assigned gene ids, in full: " + ", ".join(gene_ids) + ".",
                "Exactly these, no others.",
            ]
        )
    )

    sections.append(
        "\n".join(
            [
                "## Tools you have on this machine",
                "",
                "KBUtilLib is installed (you verified it in step 0). From "
                "`kbutillib.domains.literature`:",
                "",
                "- `GeneLiteratureUtils.annotate_genes_with_papers(genes, "
                "organism, ...)` -- layer 1: which papers mention a gene, "
                "across PaperBLAST, Europe PMC and UniProt, with an evidence "
                "class per link.",
                "- `EuropePMCClient` -- `search(query)`, `get(ids)` (title, "
                "abstract, journal, year, ids) and `resolve(id)` for one "
                "publication id. `alias_query(symbol, organism_terms)` builds "
                "the query.",
                "- `PaperBLASTUtils` -- homologs of a protein sequence that "
                "have literature attached, with identity and coverage.",
                "",
                "**What KBUtilLib does NOT have, in this version:** there is "
                "no full-text method and no supplementary-file method "
                "anywhere in the package. `EuropePMCClient` returns titles, "
                "abstracts and ids -- nothing more. Do not call, and do not "
                "assume the existence of, a `full_text`, `fulltext`, "
                "`get_full_text` or `supplementary_files` method on it; such "
                "a call will fail and cost you the batch.",
                "",
                "You MAY fetch open-access full text and supplementary "
                "material ad hoc with your own network tools, for example:",
                "",
                "```",
                "https://www.ebi.ac.uk/europepmc/webservices/rest/<PMCID>/fullTextXML",
                "https://www.ebi.ac.uk/europepmc/webservices/rest/<PMCID>/supplementaryFiles",
                "```",
                "",
                "Respect each source's terms and rate limits, and expect "
                "most non-open-access papers to give you nothing. When "
                "material you needed was unavailable -- paywalled, absent, a "
                "failed fetch -- SAY SO in that gene's `notes`. "
                "\"Could not read the full text of PMID:12345 (paywalled); "
                "the abstract alone does not say which promoter\" is a "
                "useful result. Silence about it is not.",
            ]
        )
    )

    sections.append(
        "\n".join(
            [
                "## Evidence classes",
                "",
                "Every citation carries an `evidence_class`, one of: "
                + ", ".join(f"`{name}`" for name in EVIDENCE_CLASSES)
                + ". The rules, which are not negotiable:",
                "",
                f"- `direct` -- the paper characterises THIS gene in "
                f"{manifest.organism.name} (or in a strain of it).",
                "- `curated` -- the claim comes from a curated database "
                "entry (UniProt, EcoCyc, TIGRFAM and such), not from the "
                "paper's own experiments.",
                "- `homolog` -- **the paper characterises a HOMOLOG, in "
                "another organism.** This is the common case for a "
                "PaperBLAST hit, and it must be labelled `homolog` even when "
                "the homolog is 90% identical and the claim is obviously "
                "transferable. The transfer is the reader's call, not yours.",
                "- `mention` -- **a row in a genome-wide table, a "
                "supplementary list, a transcriptomics or TnSeq table, an "
                "ortholog-table entry -- anything where the gene appears as "
                "one of hundreds -- is a `mention`**, not evidence about this "
                "gene, however suggestive the column heading is.",
                "",
                "If you are unsure between two classes, use the weaker one.",
            ]
        )
    )

    sections.append(
        "\n".join(
            [
                "## Citations: ids only, and every fact needs one",
                "",
                "- Cite publications ONLY as `PMID:<digits>`, "
                "`PMCID:PMC<digits>` or `DOI:<doi>`. A bare URL, a journal "
                "name, a paper title, a database accession or a web page is "
                "NOT a citation and will be discarded.",
                "- Every fact MUST carry at least one citation. Every "
                "citation's id is checked against Europe PMC after you "
                "answer; an id that does not resolve is dropped, and a fact "
                "left with no citations is discarded entirely. So do not "
                "state a fact you cannot cite -- it will not survive, and it "
                "will be recorded as a fact you asserted and could not "
                "support.",
                "- Prefer a `quote`: the sentence you took the claim from. "
                "It is optional, and it is the difference between a claim a "
                "human can check in ten seconds and one they must re-find.",
                "- Do not cite a paper you did not actually read some of "
                "(abstract included). Do not cite from memory.",
            ]
        )
    )

    sections.append(
        "\n".join(
            [
                "## Fact types",
                "",
                "A fact's `type` must be exactly one of: "
                + ", ".join(f"`{name}`" for name in FACT_TYPES)
                + ". A type outside that list is rejected on collection, so "
                "use `other` rather than inventing one.",
                "",
                "`confidence` is one of `high`, `medium`, `low`.",
            ]
        )
    )

    sections.append(
        "\n".join(
            [
                "## The output document",
                "",
                "Each gene's value follows this JSON schema exactly:",
                "",
                "```json",
                per_gene_schema,
                "```",
                "",
                "and the document as a whole is:",
                "",
                "```json",
                document_shape,
                "```",
                "",
                "Rules on the document:",
                "",
                f"- `schema_version` is the string "
                f'"{BATCH_SCHEMA_VERSION}"; `batch_id` is "{batch_id}".',
                "- `results` has **exactly one entry per assigned gene id**, "
                f"all {len(gene_ids)} of them, **including genes you failed "
                "on**. A gene you could find nothing for gets "
                '`{"status": "no_evidence", "facts": [], "notes": "..."}`; a '
                'gene whose research broke gets `{"status": "error", '
                '"error": "<what happened>", "facts": []}`. A missing gene is '
                "reported as a failure of the batch, so an honest error entry "
                "is strictly better than an omission.",
                "- Do NOT add gene ids that were not assigned to you: a "
                "foreign id is rejected on collection and wastes the work.",
                "- Do NOT write the same gene id twice.",
                "- `notes` is free text, per gene. Use it for what you could "
                "not get to.",
            ]
        )
    )

    sections.append(
        "\n".join(
            [
                "## Write it, and COMMIT it, before you exit",
                "",
                f"Write the document to `{output_path}` "
                "(repository-relative), then:",
                "",
                "```bash",
                f"git add {output_path}",
                f'git commit -m "facts(batch {batch_id}): gene literature '
                'results"',
                "```",
                "",
                "on the task branch you are already on. Do NOT create or "
                "switch branches, do NOT push, and do NOT merge: the branch "
                "is read directly with `git show <branch>:"
                f"{output_path}`, and a result that is not committed does not "
                "exist as far as collection is concerned. An uncommitted "
                "file is indistinguishable from a task that never ran.",
                "",
                "Pretty-print the JSON (two-space indent) -- a human reads "
                "these diffs.",
            ]
        )
    )

    return "\n\n".join(sections) + "\n"


# ── the utility ──────────────────────────────────────────────────────────


def _subprocess_runner(
    argv: Sequence[str], *, timeout: Optional[float] = None
) -> subprocess.CompletedProcess:
    """Run *argv* and capture its output.  The default runner.

    Kept as a module-level function so that a test injects a replacement at
    the ONE seam, and so that the real path -- an actual process, actual
    argv, actual stdout -- is what production uses.
    """
    return subprocess.run(  # noqa: S603 - argv is built here, never a shell string
        list(argv),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


class MaestroBatchUtils(SharedEnvUtils):
    """Submit gene-facts research batches to Maestro, and collect them.

    Args:
        europepmc: Object exposing ``resolve(id)``, used to verify every
            cited id at collection.  Defaults to a lazily created
            :class:`~kbutillib.domains.literature.europepmc.EuropePMCClient`.
        cache: Shared literature cache.  Defaults to a
            :class:`~kbutillib.domains.literature.cache.LiteratureCache` at
            ``gene_literature.cache_path``.
        runner: ``runner(argv, timeout=...) -> CompletedProcess``-like, used
            for BOTH ``maestro submit`` and ``git show``.  Defaults to
            :func:`subprocess.run` with captured output.
        git_exe: The git executable.  ``"git"``, resolved on PATH, unless a
            caller names one.
        **kwargs: Passed to :class:`SharedEnvUtils`.

    Config:
        ``gene_literature.cache_path``: cache location.

    Note:
        ``maestro_exe`` is an argument of
        :meth:`submit_gene_facts_batches`, not of this constructor, and it
        has no magic: whatever is passed is what is executed.  The default
        ``"maestro"`` resolves on PATH -- which on a fleet machine is a REAL
        submitter of real paid tasks.  Tests in this repository always pass
        an explicit fake, and the literature suite additionally puts a
        refusing fake first on PATH so that an accidental default cannot
        reach the real binary.
    """

    def __init__(
        self,
        europepmc: Optional[Any] = None,
        cache: Optional[LiteratureCache] = None,
        runner: Optional[Callable[..., Any]] = None,
        git_exe: str = "git",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._europepmc = europepmc
        self._cache = cache
        self._runner = runner if runner is not None else _subprocess_runner
        self.git_exe = git_exe

    # ── lazily built collaborators ────────────────────────────────────

    def _delegate_kwargs(self) -> Dict[str, Any]:
        """Constructor kwargs that carry this object's config to a delegate."""
        return {
            "config": self.export_environment().get("config") or {},
            "config_file": False,
            "token_file": None,
            "kbase_token_file": None,
        }

    @property
    def europepmc(self) -> Any:
        """The Europe PMC client, built on first use when not injected."""
        if self._europepmc is None:
            from .europepmc import EuropePMCClient

            self._europepmc = EuropePMCClient(**self._delegate_kwargs())
        return self._europepmc

    @property
    def cache(self) -> LiteratureCache:
        """The shared literature cache, opened on first use when not injected."""
        if self._cache is None:
            self._cache = LiteratureCache(
                resolve_cache_path(self.get_config_value(CACHE_PATH_CONFIG_KEY))
            )
        return self._cache

    # ── submission ────────────────────────────────────────────────────

    def submit_gene_facts_batches(
        self,
        genes: Iterable[Union[Gene, Mapping[str, Any]]],
        organism: Union[Organism, Mapping[str, Any]],
        papers_by_gene: Optional[Mapping[str, Any]] = None,
        *,
        repo: str,
        worker_env: Mapping[str, Any],
        machine: str = DEFAULT_MACHINE,
        backend: str = DEFAULT_BACKEND,
        batch_size: int = DEFAULT_BATCH_SIZE,
        out_dir: str = DEFAULT_OUT_DIR,
        timeout: int = DEFAULT_TIMEOUT,
        maestro_exe: str = DEFAULT_MAESTRO_EXE,
    ) -> List[BatchHandle]:
        """Submit one Maestro researcher task per batch of *batch_size* genes.

        Every prompt is assembled, and size-checked, BEFORE the first
        submission: a call that would fail on its fourth batch must not leave
        three paid tasks running.

        Args:
            genes: The genes.  Normalised; a repeated id is refused.
            organism: The organism they come from.
            papers_by_gene: ``{gene_id: papers}`` -- layer 1's output per
                gene, as a ``PaperAnnotation`` (sliced here), a mapping that
                validates as one, or a sequence of publication ids.  A gene
                with no entry is submitted with no paper slice.
            repo: The repository the task runs in, as Maestro names it.
            worker_env: Required mapping carrying every key of
                :data:`WORKER_ENV_KEYS`.
            machine: Target machine.
            backend: Maestro backend; also the reported ``model``.
            batch_size: Genes per task.
            out_dir: Repository-relative directory for the output files.
            timeout: Per-task timeout, in seconds, passed through.
            maestro_exe: The submitter to execute.  Whatever is passed is
                what runs -- see the class note.

        Returns:
            One :class:`BatchHandle` per batch, in batch order.

        Raises:
            ValueError: no ``repo``; ``batch_size`` below 1; a gene with no
                id or a repeated gene id; ``worker_env`` missing or
                incomplete; an assembled prompt over
                :data:`MAX_PROMPT_BYTES`.
            RuntimeError: a submit call failed, or its stdout carried no
                ``task_id``.  The message names the batches already
                submitted, because those are real and need collecting.
            TypeError: a ``papers_by_gene`` value is not an accepted type.
        """
        if not str(repo or "").strip():
            raise ValueError("repo is required: Maestro submits into a repository")
        if int(batch_size) < 1:
            raise ValueError(f"batch_size must be at least 1, got {batch_size!r}")

        gene_list = _normalise_genes(genes)
        target = _as_organism(organism)
        env = _validated_worker_env(worker_env)
        slices_by_gene = dict(papers_by_gene or {})
        directory = str(out_dir or "").strip().strip("/")

        # Assemble everything first.  Nothing is submitted until every batch
        # of this call has a prompt that is within budget.
        planned: List[Tuple[str, str, BatchManifest, str]] = []
        for start in range(0, len(gene_list), int(batch_size)):
            batch = gene_list[start : start + int(batch_size)]
            batch_id = uuid.uuid4().hex[:12]
            output_path = f"{directory}/{batch_id}.json" if directory else f"{batch_id}.json"
            paper_slices: Dict[str, PaperAnnotation] = {}
            for gene in batch:
                annotation = _slice_for(gene, slices_by_gene.get(gene.id))
                if annotation is not None:
                    paper_slices[gene.id] = annotation
            manifest = BatchManifest(
                genes=batch,
                organism=target,
                paper_slices=paper_slices,
                worker_env=env,
                backend=backend,
                model=backend,
                machine=machine,
                repo=repo,
                timeout=int(timeout),
            )
            prompt = build_batch_prompt(
                manifest, batch_id=batch_id, output_path=output_path
            )
            size = len(prompt.encode("utf-8"))
            if size > MAX_PROMPT_BYTES:
                raise ValueError(
                    f"the assembled prompt for batch {batch_id} is {size} bytes, "
                    f"over the {MAX_PROMPT_BYTES}-byte limit "
                    f"({len(batch)} genes). Nothing was submitted. Lower "
                    "batch_size, or drop the protein sequences / layer-1 "
                    "slices from the genes in this batch."
                )
            manifest.prompt_sha256 = _sha256(prompt)
            planned.append((batch_id, output_path, manifest, prompt))

        handles: List[BatchHandle] = []
        for batch_id, output_path, manifest, prompt in planned:
            argv = [
                str(maestro_exe),
                "submit",
                "--role",
                "researcher",
                "--backend",
                str(backend),
                "--machine",
                str(machine),
                "--repo",
                str(repo),
                "--success-criterion",
                "output-signal",
                "--expected-output",
                output_path,
                "--timeout",
                str(int(timeout)),
                prompt,
            ]
            # A researcher batch is unreviewed model output.  This assertion
            # is cheap and local, and it fails an edit that adds a merge flag
            # here before that edit can merge anything.
            forbidden = [flag for flag in argv if flag in FORBIDDEN_MAESTRO_FLAGS]
            if forbidden:
                raise RuntimeError(
                    "refusing to submit with " + ", ".join(forbidden)
                )

            task_id, branch = self._submit_one(
                argv, batch_id=batch_id, submitted=handles
            )
            handles.append(
                BatchHandle(
                    batch_id=batch_id,
                    task_id=task_id,
                    branch=branch,
                    gene_ids=[gene.id for gene in manifest.genes],
                    output_path=output_path,
                    submitted_at=_utc_now_iso(),
                    repo=repo,
                    machine=machine,
                    backend=backend,
                    input_manifest=manifest,
                )
            )
        return handles

    def _submit_one(
        self,
        argv: Sequence[str],
        *,
        batch_id: str,
        submitted: Sequence[BatchHandle],
    ) -> Tuple[str, str]:
        """Run one submit call and read ``task_id`` / ``branch_name`` out of it.

        Raises:
            RuntimeError: the call failed, or stdout carried no task id.  The
                message lists the batches already submitted by this call --
                those tasks are running and somebody has to collect them.
        """
        try:
            completed = self._runner(list(argv), timeout=SUBMIT_CALL_TIMEOUT)
        except Exception as problem:  # noqa: BLE001 - reported, with context
            raise RuntimeError(
                self._submit_failure(batch_id, submitted, str(problem))
            ) from problem

        returncode = int(getattr(completed, "returncode", 1) or 0)
        stdout = str(getattr(completed, "stdout", "") or "")
        stderr = str(getattr(completed, "stderr", "") or "")
        if returncode != 0:
            raise RuntimeError(
                self._submit_failure(
                    batch_id,
                    submitted,
                    f"the submit call exited {returncode}: "
                    f"{(stderr or stdout).strip()[:2000]}",
                )
            )

        payload = _submit_payload(stdout)
        task_id = str((payload or {}).get("task_id") or "").strip()
        branch = str(
            (payload or {}).get("branch_name")
            or (payload or {}).get("branch")
            or ""
        ).strip()
        if not task_id:
            raise RuntimeError(
                self._submit_failure(
                    batch_id,
                    submitted,
                    "the submit call printed no task_id. stdout was: "
                    + (stdout.strip()[:2000] or "(empty)"),
                )
            )
        if not branch:
            # A task with no branch cannot be collected: the output is read
            # from the branch, and nothing else knows where it went.
            raise RuntimeError(
                self._submit_failure(
                    batch_id,
                    submitted,
                    f"the submit call returned task {task_id} with no "
                    "branch_name, so its output could never be read",
                )
            )
        return task_id, branch

    @staticmethod
    def _submit_failure(
        batch_id: str, submitted: Sequence[BatchHandle], detail: str
    ) -> str:
        """The message for a failed submission, naming what IS already live."""
        message = f"Maestro submission failed for batch {batch_id}: {detail}"
        if submitted:
            message += (
                ". Already submitted by this call, and still live: "
                + ", ".join(
                    f"{handle.batch_id} (task {handle.task_id})"
                    for handle in submitted
                )
            )
        return message

    # ── collection ────────────────────────────────────────────────────

    def collect_gene_facts_batches(
        self,
        handles: Iterable[Union[BatchHandle, Mapping[str, Any]]],
        *,
        repo_path: str,
        organism: Optional[Union[Organism, Mapping[str, Any]]] = None,
        papers_by_gene: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, GeneFacts]:
        """Read, verify and cache every batch's committed output.

        Maestro's own status is never consulted: the question is whether the
        output file exists on the task branch, which is answered by
        ``git -C <repo_path> show <branch>:<output_path>``.  A task that
        reported success without committing has produced nothing, and looks
        the same as one still running -- hence the single
        ``pending_or_failed`` status for both.

        Args:
            handles: :class:`BatchHandle` objects, or mappings that validate
                as them (a round trip through JSON).
            repo_path: A checkout of the target repository that has the task
                branches.  Only ``git show`` is run against it: no checkout,
                no fetch, no working-tree change.
            organism: Ignored when the handle carries a manifest, which is
                always, and accepted only so that an older caller passing it
                still works.  The manifest is the authority: it records what
                was actually submitted.
            papers_by_gene: As *organism* -- the manifest's slices are used.

        Returns:
            ``{gene_id: GeneFacts}`` with exactly one entry per gene id
            assigned across the handles, each with ``api_type="maestro"``,
            ``model=<backend>`` and ``mode="web"``.

        Raises:
            ValueError: no ``repo_path``, or two handles claim the same gene
                id (the answers would overwrite each other).
        """
        if not str(repo_path or "").strip():
            raise ValueError("repo_path is required: the outputs are read from git")

        handle_list = [
            handle
            if isinstance(handle, BatchHandle)
            else BatchHandle.model_validate(dict(handle))
            for handle in handles or []
        ]
        if organism is not None or papers_by_gene is not None:
            self.log_debug(
                "collect_gene_facts_batches ignores organism/papers_by_gene: "
                "each handle's input_manifest records what was submitted"
            )

        assigned: Dict[str, str] = {}
        for handle in handle_list:
            for gene_id in handle.gene_ids:
                if gene_id in assigned:
                    raise ValueError(
                        f"gene {gene_id!r} is assigned to two handles "
                        f"({assigned[gene_id]} and {handle.batch_id}); their "
                        "answers would overwrite each other"
                    )
                assigned[gene_id] = handle.batch_id

        results: Dict[str, GeneFacts] = {}
        for handle in handle_list:
            results.update(self._collect_one(handle, repo_path=repo_path))
        return results

    def _collect_one(
        self, handle: BatchHandle, *, repo_path: str
    ) -> Dict[str, GeneFacts]:
        """One batch: read it, validate it, verify it, cache it."""
        manifest = handle.input_manifest
        text, failure = self._read_output(handle, repo_path=repo_path)
        if failure is not None:
            return {
                gene_id: _error_facts(
                    gene_id,
                    kind="pending_or_failed",
                    message=failure,
                    manifest=manifest,
                )
                for gene_id in handle.gene_ids
            }

        try:
            document, duplicates = _json_with_duplicate_keys(text or "")
        except ValueError as problem:
            return {
                gene_id: _error_facts(
                    gene_id,
                    kind="pending_or_failed",
                    message=(
                        f"{handle.branch}:{handle.output_path} is not JSON: "
                        f"{problem}"
                    ),
                    manifest=manifest,
                )
                for gene_id in handle.gene_ids
            }

        if not isinstance(document, dict) or not isinstance(
            document.get("results"), dict
        ):
            return {
                gene_id: _error_facts(
                    gene_id,
                    kind="pending_or_failed",
                    message=(
                        f"{handle.branch}:{handle.output_path} carries no "
                        "'results' object"
                    ),
                    manifest=manifest,
                )
                for gene_id in handle.gene_ids
            }

        stamped = str(document.get("batch_id") or "").strip()
        if stamped and stamped != handle.batch_id:
            # The file on this path belongs to another batch.  Reading it
            # would attribute one batch's answers to another's provenance.
            return {
                gene_id: _error_facts(
                    gene_id,
                    kind="batch_mismatch",
                    message=(
                        f"{handle.branch}:{handle.output_path} is stamped "
                        f"batch_id {stamped!r}, not {handle.batch_id!r}"
                    ),
                    manifest=manifest,
                )
                for gene_id in handle.gene_ids
            }

        entries: Dict[str, Any] = document["results"]
        foreign = [key for key in entries if key not in handle.gene_ids]
        if foreign:
            # Rejected, not merged: a gene this batch was not given was not
            # researched under this batch's manifest, and may belong to
            # another batch whose own answer it would overwrite.
            self.log_warning(
                f"batch {handle.batch_id} returned "
                f"{len(foreign)} gene id(s) it was not assigned, which are "
                f"rejected: {', '.join(sorted(foreign)[:20])}"
            )

        provenance = _output_provenance(
            handle, text=text or "", schema_version=document.get("schema_version")
        )
        resolve = getattr(self.europepmc, "resolve", None)

        results: Dict[str, GeneFacts] = {}
        for gene in manifest.genes:
            if gene.id in duplicates:
                results[gene.id] = _error_facts(
                    gene.id,
                    kind="duplicate_in_output",
                    message=(
                        f"gene {gene.id!r} appears more than once in "
                        f"{handle.branch}:{handle.output_path}; neither entry "
                        "can be trusted"
                    ),
                    manifest=manifest,
                )
                continue
            if gene.id not in entries:
                # Reported per gene, deliberately: "17 of 25 genes came back"
                # is a different situation from "the batch failed", and the
                # caller can resubmit exactly the eight.
                results[gene.id] = _error_facts(
                    gene.id,
                    kind="missing_from_output",
                    message=(
                        f"gene {gene.id!r} was assigned to batch "
                        f"{handle.batch_id} but is absent from "
                        f"{handle.branch}:{handle.output_path}"
                    ),
                    manifest=manifest,
                )
                continue
            results[gene.id] = self._gene_result(
                gene,
                entries[gene.id],
                handle=handle,
                provenance=provenance,
                resolve=resolve,
            )
        return results

    def _read_output(
        self, handle: BatchHandle, *, repo_path: str
    ) -> Tuple[Optional[str], Optional[str]]:
        """``git show <branch>:<path>``, as ``(text, failure_message)``."""
        argv = [
            self.git_exe,
            "-C",
            str(repo_path),
            "show",
            f"{handle.branch}:{handle.output_path}",
        ]
        try:
            completed = self._runner(argv, timeout=SUBMIT_CALL_TIMEOUT)
        except Exception as problem:  # noqa: BLE001 - reported as pending/failed
            return None, f"git show failed: {problem}"
        if int(getattr(completed, "returncode", 1) or 0) != 0:
            stderr = str(getattr(completed, "stderr", "") or "").strip()
            return None, (
                f"no output committed at {handle.branch}:{handle.output_path} "
                f"(task {handle.task_id} may still be running, or may have "
                f"failed): {stderr[:500] or 'git show exited non-zero'}"
            )
        return str(getattr(completed, "stdout", "") or ""), None

    def _gene_result(
        self,
        gene: Gene,
        entry: Any,
        *,
        handle: BatchHandle,
        provenance: Dict[str, Any],
        resolve: Optional[Callable[[str], Any]],
    ) -> GeneFacts:
        """Validate, verify and cache one gene's entry."""
        manifest = handle.input_manifest
        annotation = manifest.paper_slices.get(gene.id)
        paper_ids, evidence = _read_papers(gene, annotation)
        papers_used = PapersUsed(selected=list(paper_ids))

        if not isinstance(entry, dict):
            return _error_facts(
                gene.id,
                kind="invalid_result",
                message=f"the entry is a {type(entry).__name__}, not an object",
                manifest=manifest,
                papers_used=papers_used,
            )

        declared = str(entry.get("status") or "ok")
        if declared == "error":
            return _error_facts(
                gene.id,
                kind="worker_error",
                message=str(entry.get("error") or "the worker reported an error"),
                manifest=manifest,
                papers_used=papers_used,
            )

        problems = _schema_problems(FACTS_JSON_SCHEMA, entry)
        if problems:
            return _error_facts(
                gene.id,
                kind="invalid_result",
                message="the entry does not match the facts schema: "
                + "; ".join(problems[:10]),
                manifest=manifest,
                papers_used=papers_used,
            )

        # An EMPTY trusted set, in web mode: the worker was never handed
        # paper text, so there is no closed set of ids it could legitimately
        # have copied.  Every cited id therefore has to resolve against
        # Europe PMC -- including the layer-1 ids we put in the prompt.
        facts, rejected = verify_facts(
            entry.get("facts"),
            trusted={},
            mode=VERIFICATION_MODE,
            resolve=resolve,
        )

        status = declared if declared in _WORKER_STATUSES else "ok"
        answer = GeneFacts(
            gene_id=gene.id,
            status=status,
            mode=VERIFICATION_MODE,
            facts=facts,
            rejected_facts=rejected,
            papers_used=papers_used,
            model=manifest.model,
            resolved_model=manifest.backend,
            api_type=MAESTRO_API_TYPE,
            prompt_version=manifest.prompt_version,
            notes=str(entry.get("notes") or ""),
        )
        answer.cache_key = _cache_key(
            gene=gene,
            organism=manifest.organism,
            max_papers=len(paper_ids),
            evidence=evidence,
            # The context is what the ANSWER rests on: this layer never sent
            # paper text, so what it sends is the prompt (by hash) and the
            # manifest, and what it reads back is the committed document (by
            # hash).  Both go in, so a re-run with a changed prompt, a
            # changed worker environment or a different committed output is a
            # different key.
            context=json.dumps(provenance, sort_keys=True, default=str),
            model=manifest.model,
            api_type=MAESTRO_API_TYPE,
            endpoint={"machine": handle.machine, "repo": handle.repo},
            mode=VERIFICATION_MODE,
        )
        self.cache.put(GENE_FACTS_TABLE, answer.cache_key, _storable(answer))
        return answer


def _output_provenance(
    handle: BatchHandle, *, text: str, schema_version: Any
) -> Dict[str, Any]:
    """What the cache key hashes besides the gene: manifest + output facts."""
    manifest = handle.input_manifest
    return {
        "manifest_version": manifest.manifest_version,
        "maestro_prompt_version": manifest.prompt_version,
        "prompt_sha256": manifest.prompt_sha256,
        "worker_env": dict(manifest.worker_env),
        "backend": manifest.backend,
        "verification_mode": manifest.verification_mode,
        "batch_id": handle.batch_id,
        "task_id": handle.task_id,
        "branch": handle.branch,
        "output_path": handle.output_path,
        "output_sha256": _sha256(text),
        "output_schema_version": (
            str(schema_version) if schema_version is not None else None
        ),
    }


def _submit_payload(stdout: str) -> Optional[Dict[str, Any]]:
    """The JSON object in a submit call's stdout, or None.

    The whole of stdout is tried first; failing that, each line from the last
    backwards, so a submitter that logged a line before its JSON is still
    read.  Nothing is guessed: a line that is not a JSON object is skipped,
    not pattern-matched.
    """
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    for line in reversed(text.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


# ── module-level API ─────────────────────────────────────────────────────


def submit_gene_facts_batches(
    genes: Iterable[Union[Gene, Mapping[str, Any]]],
    organism: Union[Organism, Mapping[str, Any]],
    papers_by_gene: Optional[Mapping[str, Any]] = None,
    *,
    repo: str,
    worker_env: Mapping[str, Any],
    machine: str = DEFAULT_MACHINE,
    backend: str = DEFAULT_BACKEND,
    batch_size: int = DEFAULT_BATCH_SIZE,
    out_dir: str = DEFAULT_OUT_DIR,
    timeout: int = DEFAULT_TIMEOUT,
    maestro_exe: str = DEFAULT_MAESTRO_EXE,
    **utils_kwargs: Any,
) -> List[BatchHandle]:
    """:meth:`MaestroBatchUtils.submit_gene_facts_batches` on a default instance.

    Args:
        genes: As the method.
        organism: As the method.
        papers_by_gene: As the method.
        repo: As the method.
        worker_env: As the method.
        machine: As the method.
        backend: As the method.
        batch_size: As the method.
        out_dir: As the method.
        timeout: As the method.
        maestro_exe: As the method.
        **utils_kwargs: Passed to :class:`MaestroBatchUtils` -- ``runner``,
            ``config``, and so on.  This is the seam a test uses instead of
            monkeypatching the module.

    Returns:
        One :class:`BatchHandle` per batch.
    """
    return MaestroBatchUtils(**utils_kwargs).submit_gene_facts_batches(
        genes,
        organism,
        papers_by_gene,
        repo=repo,
        worker_env=worker_env,
        machine=machine,
        backend=backend,
        batch_size=batch_size,
        out_dir=out_dir,
        timeout=timeout,
        maestro_exe=maestro_exe,
    )


def collect_gene_facts_batches(
    handles: Iterable[Union[BatchHandle, Mapping[str, Any]]],
    *,
    repo_path: str,
    organism: Optional[Union[Organism, Mapping[str, Any]]] = None,
    papers_by_gene: Optional[Mapping[str, Any]] = None,
    **utils_kwargs: Any,
) -> Dict[str, GeneFacts]:
    """:meth:`MaestroBatchUtils.collect_gene_facts_batches` on a default instance.

    Args:
        handles: As the method.
        repo_path: As the method.
        organism: Accepted and ignored -- see the method.
        papers_by_gene: Accepted and ignored -- see the method.
        **utils_kwargs: Passed to :class:`MaestroBatchUtils`.

    Returns:
        ``{gene_id: GeneFacts}``.
    """
    return MaestroBatchUtils(**utils_kwargs).collect_gene_facts_batches(
        handles,
        repo_path=repo_path,
        organism=organism,
        papers_by_gene=papers_by_gene,
    )


# ── Composition-based implementation ─────────────────────────────────────


class MaestroBatchUtilsImpl:
    """Composition-based version of :class:`MaestroBatchUtils`.

    Holds ``env: SharedEnvUtils`` instead of inheriting, and delegates all
    method calls to an internal instance.
    """

    def __init__(self, env, **kwargs):
        self._env = env
        _kwargs = {
            "config_file": False,
            "token_file": None,
            "kbase_token_file": None,
        }
        # Carry the environment's configuration across, so the delegate reads
        # the same gene_literature.* settings the env was built with.
        try:
            _kwargs["config"] = env.export_environment().get("config") or {}
        except Exception:
            pass
        _kwargs.update(kwargs)
        self._delegate = MaestroBatchUtils(**_kwargs)

    @property
    def env(self):
        return self._env

    def __getattr__(self, name):
        # Delegate all attribute access to the utility instance.
        return getattr(self._delegate, name)
