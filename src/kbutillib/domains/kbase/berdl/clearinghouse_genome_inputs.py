"""lw-9: the INPUTS of the KBDL genome loader.

Everything here only READS (and, for :func:`fetch_genome_objects`, downloads a
digest-verified KBDL object into a local directory). Nothing here writes to the
lakehouse, KBDL or poplar.

- :class:`AnnotationArchiveIndex` finds ``KBDLGenomeAnnotation`` results in the
  KBDL result-sweeper archive by GENE-ID PREFIX. A result's keys are
  ``'<genome_id>_<N>'`` and a genome object's features carry the same
  ``'<genome_id>_'`` prefix, so the prefix is the join.
- :func:`live_results_for_prefix` is the fallback for a prefix with nothing
  archived: it names jobs from the sweep ledger and fetches them from KBDL.
- :func:`read_term_function_map` reads W1's persisted TERM -> function-text map.
- :func:`build_external_ids` assembles ``genome_object_id -> {ncbi_assembly,
  fitness_org, img_taxon, ...}`` for the first production run from the three
  ledgers' REAL field names.
- :func:`fetch_genome_objects` downloads genome objects by id (run it where
  KBDL answers: kbhub or h100).
"""

from __future__ import annotations

import csv
import gzip
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .clearinghouse_ontology_wave import TERM_FUNCTION_MAP_COLUMNS

__all__ = [
    "DEFAULT_ARCHIVE_DIRS",
    "DEFAULT_SWEEP_LEDGER",
    "DEFAULT_OBJECT_DIR",
    "GenomeInputError",
    "AnnotationArchiveIndex",
    "live_results_for_prefix",
    "read_term_function_map",
    "ExternalIdBuild",
    "build_external_ids",
    "fetch_genome_objects",
    "read_genome_object",
]

#: Primary (kbhub) and Dropbox-mirror locations of swept annotation results.
DEFAULT_ARCHIVE_DIRS = (
    "~/.kbdl/job-results/KBDLGenomeAnnotation",
    "~/Dropbox/Science/kbdl-job-results/KBDLGenomeAnnotation",
)
DEFAULT_SWEEP_LEDGER = "~/.kbdl/sweep_ledger.jsonl"
#: Where downloaded CanonicalGenome objects are cached (``<object_id>.json``).
DEFAULT_OBJECT_DIR = "~/.kbdl/genome-objects"

_FIRST_KEY = re.compile(rb'^\s*\{\s*"((?:[^"\\]|\\.)*)"\s*:')
_PREFIX = re.compile(r"^(.*?)_\d+$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class GenomeInputError(ValueError):
    """An input file is missing, malformed or unusable. Nothing was written."""


# -- genome objects -----------------------------------------------------------


def read_genome_object(path: str | Path) -> dict[str, Any]:
    """Read one CanonicalGenome JSON object (plain or ``.gz``)."""
    path = Path(path).expanduser()
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rt", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise GenomeInputError(f"{path}: cannot read a genome object ({exc})") from exc
    if not isinstance(data, dict):
        raise GenomeInputError(f"{path}: a genome object is a JSON object, got {type(data).__name__}")
    return data


def fetch_genome_objects(
    object_ids: Iterable[str],
    dest_dir: str | Path = DEFAULT_OBJECT_DIR,
    *,
    client: Any = None,
) -> dict[str, Path]:
    """Download genome objects by id into ``dest_dir`` (``<id>.json``).

    Uses :meth:`KBDLServiceUtils.download_object`, which verifies the sha256 of
    a content object BEFORE moving it into place, so a cached file is always
    whole. An id already present is not downloaded again. READ-ONLY on KBDL.
    Run it where KBDL answers (kbhub or h100); an unreachable service raises
    the client's own error and leaves nothing behind.
    """
    dest = Path(dest_dir).expanduser()
    if client is None:
        from ..external.kbdl_service_utils import KBDLServiceUtils  # noqa: PLC0415

        client = KBDLServiceUtils()
    out: dict[str, Path] = {}
    for object_id in object_ids:
        if not _HEX64.match(object_id):
            raise GenomeInputError(f"object id {object_id!r} is not a 64-hex content object id")
        target = dest / f"{object_id}.json"
        if not target.exists():
            client.download_object(object_id, target)
        out[object_id] = target
    return out


# -- the annotation archive ---------------------------------------------------


def _prefix_of_key(key: str) -> str | None:
    match = _PREFIX.match(key)
    return match.group(1) if match else None


def _first_key_prefix(path: Path) -> str | None:
    """The gene-id prefix of a result file, from its first key only."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as handle:
        head = handle.read(8192)
    match = _FIRST_KEY.match(head)
    if not match:
        return None
    try:
        key = json.loads(b'"' + match.group(1) + b'"')
    except ValueError:
        return None
    return _prefix_of_key(key)


def _job_id(path: Path) -> str:
    name = path.name
    for suffix in (".json.gz", ".json"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


class AnnotationArchiveIndex:
    """prefix -> result files, built once and cached.

    Only the FIRST KEY of each file is read to learn its prefix, so indexing
    3,300 results costs a few seconds. ``cache_path`` (optional) persists the
    index keyed by ``(size, mtime_ns)`` so a re-run only reads new or changed
    files; :meth:`refresh` forces a rescan. The same job id found in two
    directories is indexed once (the first directory wins).
    """

    def __init__(
        self,
        dirs: Sequence[str | Path] = DEFAULT_ARCHIVE_DIRS,
        *,
        extra_files: Sequence[str | Path] = (),
        cache_path: str | Path | None = None,
    ) -> None:
        self.dirs = [Path(d).expanduser() for d in dirs]
        self.extra_files = [Path(f).expanduser() for f in extra_files]
        self.cache_path = Path(cache_path).expanduser() if cache_path else None
        self._by_prefix: dict[str, list[Path]] = {}
        self._built = False
        self.scanned = 0
        self.unreadable: list[str] = []

    def _candidates(self) -> list[Path]:
        seen: dict[str, Path] = {}
        for directory in self.dirs:
            if not directory.is_dir():
                continue
            for path in sorted(directory.iterdir()):
                if path.name.endswith((".json.gz", ".json")):
                    seen.setdefault(_job_id(path), path)
        for path in self.extra_files:
            if path.is_dir():
                for inner in sorted(path.iterdir()):
                    if inner.name.endswith((".json.gz", ".json")):
                        seen.setdefault(_job_id(inner), inner)
            elif path.exists():
                seen.setdefault(_job_id(path), path)
        return list(seen.values())

    def _read_cache(self) -> dict[str, Any]:
        if self.cache_path and self.cache_path.exists():
            try:
                data = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except ValueError:
                pass
        return {}

    def refresh(self) -> "AnnotationArchiveIndex":
        """(Re)build the index; reuses cache entries whose size and mtime match."""
        cache = self._read_cache()
        fresh: dict[str, Any] = {}
        by_prefix: dict[str, list[Path]] = {}
        self.scanned = 0
        self.unreadable = []
        for path in self._candidates():
            try:
                stat = path.stat()
            except OSError:
                self.unreadable.append(str(path))
                continue
            fingerprint = [stat.st_size, stat.st_mtime_ns]
            held = cache.get(str(path))
            if held and held.get("fingerprint") == fingerprint:
                prefix = held.get("prefix")
            else:
                self.scanned += 1
                try:
                    prefix = _first_key_prefix(path)
                except (OSError, EOFError, ValueError):
                    self.unreadable.append(str(path))
                    continue
            fresh[str(path)] = {"fingerprint": fingerprint, "prefix": prefix}
            if prefix:
                by_prefix.setdefault(prefix, []).append(path)
        self._by_prefix = by_prefix
        self._built = True
        if self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(fresh, sort_keys=True), encoding="utf-8")
        return self

    def files_for(self, prefix: str) -> list[Path]:
        """Every archived result whose gene ids carry ``prefix`` (sorted by job id)."""
        if not self._built:
            self.refresh()
        return sorted(self._by_prefix.get(prefix, []), key=_job_id)

    @property
    def prefixes(self) -> int:
        if not self._built:
            self.refresh()
        return len(self._by_prefix)

    @staticmethod
    def load(path: str | Path) -> dict[str, Any]:
        """Read one result file (``gene_id -> TOOL -> NAMESPACE -> [values]``)."""
        path = Path(path)
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, EOFError, ValueError) as exc:
            raise GenomeInputError(f"{path}: cannot read an annotation result ({exc})") from exc
        if not isinstance(data, dict):
            raise GenomeInputError(f"{path}: an annotation result is a JSON object")
        return data


def live_results_for_prefix(
    prefix: str,
    *,
    ledger_path: str | Path = DEFAULT_SWEEP_LEDGER,
    client: Any = None,
    archived_job_ids: Iterable[str] = (),
) -> list[tuple[str, dict[str, Any]]]:
    """FALLBACK: results named by the sweep ledger for ``prefix``, fetched live.

    The ledger rows of a saved ``KBDLGenomeAnnotation`` job carry its
    ``locus_prefix``. Only jobs not already archived are fetched, with
    ``GET /jobs/{id}/result`` (read-only). A job KBDL no longer holds is
    skipped; the caller reports it. Returns ``[(job_id, result), ...]``.
    """
    ledger = Path(ledger_path).expanduser()
    if not ledger.exists():
        return []
    have = set(archived_job_ids)
    wanted: list[str] = []
    with ledger.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if (
                row.get("job_type") == "KBDLGenomeAnnotation"
                and row.get("locus_prefix") == prefix
                and row.get("job_id") not in have
                and row.get("job_id") not in wanted
            ):
                wanted.append(row["job_id"])
    if not wanted:
        return []
    if client is None:
        from ..external.kbdl_service_utils import KBDLServiceUtils  # noqa: PLC0415

        client = KBDLServiceUtils()
    out: list[tuple[str, dict[str, Any]]] = []
    for job_id in wanted:
        try:
            result = client.get_job_result(job_id)
        except Exception:  # noqa: BLE001 - an expired job is expected, not fatal
            continue
        if isinstance(result, dict):
            out.append((job_id, result))
    return out


# -- W1's term -> function-text map -------------------------------------------


def read_term_function_map(path: str | Path) -> dict[str, dict[str, str]]:
    """Read the TSV W1 persists (:func:`write_term_function_map`).

    Returns ``{term_curie: row}``. Never re-parses an ontology.

    Raises:
        GenomeInputError: a missing file, a wrong header or a duplicate term.
    """
    path = Path(path).expanduser()
    try:
        handle = path.open(encoding="utf-8", newline="")
    except OSError as exc:
        raise GenomeInputError(f"{path}: cannot read the term -> function map ({exc})") from exc
    with handle:
        reader = csv.reader(handle, delimiter="\t")
        header = next(reader, None)
        if header is None or tuple(header) != tuple(TERM_FUNCTION_MAP_COLUMNS):
            raise GenomeInputError(
                f"{path}: header {header!r} is not {list(TERM_FUNCTION_MAP_COLUMNS)}"
            )
        out: dict[str, dict[str, str]] = {}
        for number, cols in enumerate(reader, 2):
            if not cols:
                continue
            if len(cols) != len(TERM_FUNCTION_MAP_COLUMNS):
                raise GenomeInputError(f"{path}:{number}: expected {len(TERM_FUNCTION_MAP_COLUMNS)} columns")
            row = dict(zip(TERM_FUNCTION_MAP_COLUMNS, cols))
            if row["term_curie"] in out:
                raise GenomeInputError(f"{path}:{number}: duplicate term {row['term_curie']}")
            out[row["term_curie"]] = row
    return out


# -- external ids from the three ledgers --------------------------------------

#: The three ledgers of the first production run (paths relative to the runs dir).
ADP1_DIR = (
    "ai-ale-of-acinetobacter-baylyi-adp1/arcs/i-want-to-annotate-the-acinetobacter-bay"
    "/build/kbdl_job_output"
)
FITNESS_LEDGER = "inner-loop/arcs/fitness-genome-analysis/data/build_ledger.json"
FITNESS_ANNOTATIONS = "inner-loop/arcs/fitness-genome-analysis/data/annotations"
PMI_LEDGER = "inner-loop/arcs/additional-genome-annotation/data/pmi_ledger.json"
PMI_ANNOTATIONS = "inner-loop/arcs/additional-genome-annotation/data/annotations_pmi_alltools"

#: external_ids keys that become genome aliases; a value may be a list.
_ALIAS_FIELDS = ("ncbi_assembly", "fitness_org", "img_taxon")
_GCF = re.compile(r"\bGC[AF]_\d{9}\.\d+\b")


@dataclass
class ExternalIdBuild:
    """The assembled map plus what it could not say."""

    external_ids: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Local annotation files the ledgers point at (extra inputs for the index).
    annotation_paths: list[Path] = field(default_factory=list)
    #: Plain-language notes: ledgers missing, genomes without an object id, ...
    notes: list[str] = field(default_factory=list)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GenomeInputError(f"{path}: cannot read ({exc})") from exc


def _fasta_ids_from_builds(build_dir: Path | None) -> dict[str, str]:
    """``genome_object_id -> fasta_object_id`` from swept KBDLBuildGenome results."""
    out: dict[str, str] = {}
    if build_dir is None or not build_dir.is_dir():
        return out
    for path in sorted(build_dir.iterdir()):
        if not path.name.endswith((".json.gz", ".json")):
            continue
        try:
            opener = gzip.open if path.suffix == ".gz" else open
            with opener(path, "rt", encoding="utf-8") as handle:
                result = json.load(handle)
        except (OSError, EOFError, ValueError):
            continue
        genomes = result.get("genomes") if isinstance(result, dict) else None
        if not isinstance(genomes, dict):
            continue
        for info in genomes.values():
            if isinstance(info, dict) and info.get("genome_object_id") and info.get("fasta_object_id"):
                out.setdefault(str(info["genome_object_id"]), str(info["fasta_object_id"]))
    return out


def build_external_ids(
    runs_dir: str | Path = "~/Dropbox/Science/runs",
    *,
    build_results_dir: str | Path | None = "~/Dropbox/Science/kbdl-job-results/KBDLBuildGenome",
) -> ExternalIdBuild:
    """Assemble ``external_ids`` for ADP1, the Fitness Browser set and PMI.

    REAL field names, read from the ledgers (not assumed):

    - ADP1 ``job_record.json``: ``genome_object_id``, ``tax_id`` (-> taxon_id)
      and ``source`` ("NCBI GCF_000046845.1 fasta+gff ...", the accession is
      parsed out of it -> ``ncbi_assembly``); ``build_genome_*.json`` beside it
      carries ``fasta_object_id``; ``annot_result_*.json`` is an annotation
      file.
    - Fitness ``build_ledger.json``: keyed by organism; ``sref`` -> the
      ``fitness_org`` alias, ``genome_object_id``.
    - PMI ``pmi_ledger.json``: keyed by IMG taxon id; ``genome_id`` -> the
      ``img_taxon`` alias, ``genome_object_id``, ``strain``.

    The ledgers' ``taxonomy.reference`` is the NEAREST reference found by ANI,
    not the genome's own assembly, so it is NEVER used as ``ncbi_assembly``;
    only ADP1 states its assembly. Every genome here was built from supplied
    FASTA + GFF (ADP1 ``source``; a ``gff`` file or ``gff_path`` per Fitness /
    PMI genome), so each carries ``gene_call_tool = 'SOURCE_GFF'``.
    """
    runs = Path(runs_dir).expanduser()
    fasta_ids = _fasta_ids_from_builds(Path(build_results_dir).expanduser() if build_results_dir else None)
    build = ExternalIdBuild()
    ext = build.external_ids

    def put(object_id: str | None, **fields: Any) -> None:
        if not object_id:
            return
        entry = ext.setdefault(object_id, {})
        for key, value in fields.items():
            if value in (None, ""):
                continue
            held = entry.get(key)
            if key in _ALIAS_FIELDS and held not in (None, value):
                # one genome object under several external ids (e.g. an organism
                # and its deletion strain built from the same genome)
                entry[key] = sorted({*([held] if isinstance(held, str) else held), value})
            else:
                entry[key] = value
        if object_id in fasta_ids:
            entry.setdefault("fasta_object_id", fasta_ids[object_id])
        entry.setdefault("gene_call_tool", "SOURCE_GFF")

    # -- ADP1
    adp1 = runs / ADP1_DIR
    record_path = adp1 / "job_record.json"
    if record_path.exists():
        record = _load_json(record_path)
        match = _GCF.search(str(record.get("source") or ""))
        fasta = None
        for built in sorted(adp1.glob("build_genome_*.json")):
            for info in (_load_json(built).get("genomes") or {}).values():
                if isinstance(info, dict) and info.get("genome_object_id") == record.get("genome_object_id"):
                    fasta = info.get("fasta_object_id")
        put(
            record.get("genome_object_id"),
            ncbi_assembly=match.group(0) if match else None,
            taxon_id=record.get("tax_id"),
            fasta_object_id=fasta,
        )
        build.annotation_paths.extend(sorted(adp1.glob("annot_result_*.json")))
        if not match:
            build.notes.append("ADP1: no GCF accession in job_record.source")
    else:
        build.notes.append(f"ADP1 job_record.json not found under {adp1}")

    # -- Fitness Browser
    fitness = runs / FITNESS_LEDGER
    if fitness.exists():
        for key, row in sorted(_load_json(fitness).items()):
            object_id = row.get("genome_object_id")
            if not object_id:
                build.notes.append(f"fitness {key}: no genome_object_id in the ledger")
                continue
            put(object_id, fitness_org=row.get("sref") or key)
        annotations = runs / FITNESS_ANNOTATIONS
        if annotations.is_dir():
            build.annotation_paths.append(annotations)
    else:
        build.notes.append(f"Fitness ledger not found: {fitness}")

    # -- PMI
    pmi = runs / PMI_LEDGER
    if pmi.exists():
        for key, row in sorted(_load_json(pmi).items()):
            object_id = row.get("genome_object_id")
            if not object_id:
                build.notes.append(f"pmi {key}: no genome_object_id in the ledger")
                continue
            put(object_id, img_taxon=row.get("genome_id") or key)
        annotations = runs / PMI_ANNOTATIONS
        if annotations.is_dir():
            build.annotation_paths.append(annotations)
    else:
        build.notes.append(f"PMI ledger not found: {pmi}")
    return build
