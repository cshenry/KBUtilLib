"""The ``arc`` adapter, its manifest grammar, and the ``arc-kbdl`` preset.

Every fixture here is built in ``tmp_path`` IN THE SHAPES Albert surveyed on
the BERDL pod (2026-10-06): the ``runs`` table's real column list, the two
job-result shapes, and a backup tarball with a ledger and an annotation
member. The pod itself is unreachable from a test runner, so the shapes are
the contract -- an adapter that passed against a convenient fixture and not
the real shape would fail on first contact with the store.

NOTHING HERE REACHES THE NETWORK. The genome objects come from a fake KBDL
client with a ``download_object`` that writes bytes and nothing else.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import sqlite3
import tarfile
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from kbutillib.domains.external import kbdl_service_utils as ksu
from kbutillib.domains.identity import standardizers
from kbutillib.domains.kbase.berdl import clearinghouse_backfill as cb
from kbutillib.domains.kbase.berdl import clearinghouse_manifest as cm
from kbutillib.domains.kbase.berdl import clearinghouse_schema as schema
from kbutillib.domains.kbase.berdl import clearinghouse_shard as cs

# ==========================================================================
# Fixtures in the pod's shapes
# ==========================================================================

#: The real `runs` DDL, column for column, as surveyed on the pod. Written out
#: in full rather than reduced to the columns the adapter reads: the adapter
#: must work against the store that exists, and a narrowed fixture would hide
#: a `SELECT *` that depended on a column order the real table does not have.
RUNS_DDL = """
CREATE TABLE runs (
    record_id INTEGER PRIMARY KEY,
    analysis_id TEXT,
    run_uid TEXT,
    project TEXT,
    arc_slug TEXT,
    subject TEXT,
    kind TEXT,
    producer TEXT,
    producer_version TEXT,
    status TEXT,
    trust_tier TEXT,
    provenance TEXT,
    contract_version TEXT,
    unknown_kind INTEGER,
    artifacts TEXT,
    payload TEXT,
    created_at TEXT,
    updated_at TEXT,
    subject_feature_count INTEGER,
    method_count INTEGER,
    consistency_overall REAL,
    consistency_metric_version TEXT,
    ic_corpus_version TEXT,
    description TEXT
)
"""

#: A 64-hex FASTA object id. Its first 16 characters are the CanonicalGenome's
#: `genome_id` and therefore the prefix of every one of its feature ids -- the
#: ONLY legitimate join key (BuiltGenomeEntry.prefix is a CLASS prefix and is
#: never used as one).
FASTA_ID = "a1b2c3d4e5f60718" + "9" * 48
GENOME_ID = FASTA_ID[:16]

CONTIGS = ["ACGTACGTAC", "TTTTGGGGCCCCAAAA"]
TRANSLATIONS = ["MKTAYIAKQR", "MSTNPKPQRK", "MVLSPADKTN"]
DNA = ["ATGAAAACCGCATATATTGCTAAACAACGT", "ATGTCTACCAATCCTAAACCTCAACGT",
       "ATGGTGCTGTCTCCTGCCGACAAAACCAAC"]


def _canonical_genome(object_id: str, *, taxid: object = 562) -> dict:
    """A CanonicalGenome: two contigs, three features, every sequence inline.

    `dna_sequence` and `protein_translation` are required on every feature of
    a real CanonicalGenome, so they are present on every feature here.
    """
    return {
        "genome_id": GENOME_ID,
        "prefix": "eco",  # the CLASS prefix -- deliberately present and unused
        "taxonomy": {
            "genus": "Escherichia",
            "species": "coli",
            "lineage": "Bacteria;Proteobacteria",
            "ncbi_taxid": taxid,
            "ani": 99.1,
            "reference": "GCF_000005845.2",
            "reason": "skani",
            "source_database": "gtdb",
        },
        "contigs": [
            {
                "id": f"{GENOME_ID}_c{i}",
                "source_id": f"contig{i}",
                "length": len(sequence),
                "sequence": sequence,
            }
            for i, sequence in enumerate(CONTIGS)
        ],
        "features": [
            {
                "id": f"{GENOME_ID}_{i}",
                "source_id": f"gene{i}",
                "location": [[f"{GENOME_ID}_c0", i * 100, "+", 30]],
                "dna_sequence": DNA[i],
                "protein_translation": TRANSLATIONS[i],
            }
            for i in range(3)
        ],
    }


def _object_id_for(payload: dict) -> str:
    """The content-addressed id of a JSON object: sha256 of the bytes served."""
    return hashlib.sha256(_object_bytes(payload)).hexdigest()


def _object_bytes(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


class FakeKBDLClient:
    """A stand-in for ``KBDLServiceUtils`` with only ``download_object``.

    Serves bytes out of a dict keyed by object id and records every id it was
    asked for, which is how "each needed object is fetched ONCE" is asserted.
    An id it does not hold raises, exactly as a 404 would.
    """

    def __init__(self, objects: dict[str, bytes]):
        self.objects = dict(objects)
        self.requested: list[str] = []

    def download_object(self, object_id: str, dest_path) -> Path:
        self.requested.append(object_id)
        if object_id not in self.objects:
            raise ksu.KBDLNotFoundError(f"no object {object_id}")
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.objects[object_id])
        return dest


@pytest.fixture
def genome_object() -> dict:
    return _canonical_genome("x")


@pytest.fixture
def arc_store(tmp_path, genome_object):
    """A complete arc store in ``tmp_path``: db, job results, tarball, client.

    Returns a small namespace with everything a source needs to be pointed at
    it. Rows of EVERY kind the store holds are present, including the three
    non-backfillable ones, so the by-kind skip accounting has something to
    count.
    """
    object_id = _object_id_for(genome_object)
    results = tmp_path / "job-results"
    (results / "KBDLBuildGenome").mkdir(parents=True)
    (results / "KBDLGenomeAnnotation").mkdir(parents=True)

    build_result = results / "KBDLBuildGenome" / "build-job-1.json.gz"
    with gzip.open(build_result, "wt", encoding="utf-8") as handle:
        json.dump(
            {
                "genomes": {
                    "E_coli_K12": {
                        "genome_id": GENOME_ID,
                        "prefix": "eco",
                        "fasta_object_id": FASTA_ID,
                        "genome_object_id": object_id,
                        "taxonomy": genome_object["taxonomy"],
                        "skani_hits": [],
                    }
                },
                "errors": {},
                "coordinate_convention": "0-based-half-open",
            },
            handle,
        )

    annotation_result = (
        results / "KBDLGenomeAnnotation" / "annotation-job-1.json.gz"
    )
    with gzip.open(annotation_result, "wt", encoding="utf-8") as handle:
        json.dump(
            {
                f"{GENOME_ID}_0": {
                    "BAKTA": {"FUNCTION": ["hypothetical protein"]},
                    "KOFAMSCAN": {"KO": ["K00001"]},
                    "TRANSYT": {"EC": ["1.1.1.1"]},
                },
                f"{GENOME_ID}_1": {},  # unannotated -- yields nothing
                f"{GENOME_ID}_2": {"PROKKA": {"FUNCTION": ["dehydrogenase"]}},
            },
            handle,
        )

    db_path = tmp_path / "runs.sqlite"
    connection = sqlite3.connect(db_path)
    connection.executescript(RUNS_DDL)
    rows = [
        (1, "build-job-1", "kbdl.build_genome", str(build_result),
         "2026-09-20T10:00:00Z", "2026-09-20T10:00:00Z"),
        (2, "annotation-job-1", "kbdl.genome_annotation", str(annotation_result),
         "2026-09-23T15:13:54Z", "2026-09-23T15:13:54Z"),
        (3, "model-1", "kbdl.model_build", "", "2026-09-24T00:00:00Z",
         "2026-09-24T00:00:00Z"),
        (4, "fitness-1", "kbdl.fitness_analysis", "", "2026-09-24T01:00:00Z",
         "2026-09-24T01:00:00Z"),
        (5, "recon-1", "kbutillib.reconstruct", "", "2026-09-24T02:00:00Z",
         "2026-09-24T02:00:00Z"),
    ]
    for record_id, run_uid, kind, result, created, updated in rows:
        connection.execute(
            "INSERT INTO runs (record_id, run_uid, kind, artifacts, created_at, "
            "updated_at, status) VALUES (?, ?, ?, ?, ?, ?, 'completed')",
            (
                record_id,
                run_uid,
                kind,
                json.dumps({"result": result}) if result else None,
                created,
                updated,
            ),
        )
    connection.commit()
    connection.close()

    client = FakeKBDLClient({object_id: _object_bytes(genome_object)})

    class _Store:
        pass

    store = _Store()
    store.tmp_path = tmp_path
    store.db_path = db_path
    store.object_id = object_id
    store.client = client
    store.cache_dir = tmp_path / "objects"
    store.build_result = build_result
    store.annotation_result = annotation_result
    return store


def _make_tarball(
    path: Path,
    *,
    ledger_key: str,
    genome_object_id: str,
    done_ts: object = "2026-09-23T13:07:00Z",
    annotations: dict | None = None,
) -> Path:
    """A backup-arc tarball: one batch ledger and one annotation member."""
    ledger = {
        ledger_key: {
            "genome_id": ledger_key,
            "archive_object_id": "arch-1",
            "state": "done",
            "build_job_id": "build-job-9",
            "genome_object_id": genome_object_id,
            "taxonomy": {"ncbi_taxid": 562, "genus": "Escherichia"},
            "ann_job_id": "ann-job-9",
            "n_features": 3,
            "n_annotated": 1,
            "done_ts": done_ts,
        }
    }
    members = {
        "data/batch_ledger.json": ledger,
        "data/pmi_ledger.json": {},
        f"data/annotations/{ledger_key}.json": (
            annotations
            if annotations is not None
            else {f"{GENOME_ID}_1": {"RAST": {"role": ["transporter"]}}}
        ),
    }
    with tarfile.open(path, "w:gz") as archive:
        for name, payload in members.items():
            raw = json.dumps(payload).encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
    return path


def _retaxonomise(store, taxid: object) -> None:
    """Rewrite the store's taxonomy -- in the build result AND the object.

    The adapter takes a genome's taxonomy from whichever source names it
    first (the build record, then the object), so a test that changed only
    one of them would be testing nothing: the other would still supply 562.
    """
    with gzip.open(store.build_result, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    for entry in payload["genomes"].values():
        entry["taxonomy"]["ncbi_taxid"] = taxid
    with gzip.open(store.build_result, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)
    store.client.objects[store.object_id] = _object_bytes(
        _canonical_genome("x", taxid=taxid)
    )


def _source(name: str, store, *, emit: str, **locator) -> cm.Source:
    """A ``[[source]]`` block pointed at ``store``, with mapping omitted."""
    base = {
        "db_path": str(store.db_path),
        "emit": emit,
        "tarballs": "",
        "cache_dir": str(store.cache_dir),
        **locator,
    }
    return cm.Source(
        name=name,
        adapter="arc",
        entity_type="protein",
        kinds=("entity",),
        locator=base,
        hash_spec={"raw_column": "protein_translation"},
        content={},
        result={},
    )


def _adapter(name: str, store, *, emit: str, **locator) -> cs.ArcSourceAdapter:
    adapter = cs.ArcSourceAdapter(
        _source(name, store, emit=emit, **locator), client=store.client
    )
    adapter.log_note = lambda message: None  # keep test output clean
    return adapter


@pytest.fixture(autouse=True)
def state_root(tmp_path, monkeypatch):
    """Point the clearinghouse state root at tmp_path for every test here.

    Checkpoints live under it, and a test that wrote one into the operator's
    real state directory would change what the NEXT real run reads.
    """
    root = tmp_path / "state"
    monkeypatch.setenv("KBU_CLEARINGHOUSE_STATE_DIR", str(root))
    return root


# ==========================================================================
# download_object
# ==========================================================================


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body
        self.ok = True
        self.status_code = 200

    def iter_content(self, chunk_size=None):
        yield self._body


class _RecordingClient(ksu.KBDLServiceUtils):
    """A real client with ``_request`` replaced, so the method under test runs."""

    def __init__(self, body: bytes):
        self._body = body
        self.paths: list[str] = []
        self.base_url = "http://example.invalid"
        self.timeout = 1.0
        self.session = None

    def _request(self, method, path, **kwargs):
        self.paths.append(path)
        return _FakeResponse(self._body)


def test_download_object_verifies_the_digest_and_places_the_file(tmp_path):
    body = b'{"genome_id": "x"}'
    object_id = hashlib.sha256(body).hexdigest()
    client = _RecordingClient(body)

    dest = client.download_object(object_id, tmp_path / "nested" / "g.json")

    assert dest.read_bytes() == body
    assert client.paths == [f"/objects/{object_id}/content"]


def test_download_object_rejects_a_sha_mismatch_and_caches_nothing(tmp_path):
    """The check PRECEDES the rename, so a bad body never becomes a cache
    entry."""
    client = _RecordingClient(b"these are not the bytes you asked for")
    object_id = hashlib.sha256(b"something else").hexdigest()
    dest = tmp_path / "g.json"

    with pytest.raises(ksu.KBDLObjectDigestError) as excinfo:
        client.download_object(object_id, dest)

    assert object_id in str(excinfo.value)
    assert not dest.exists()
    # and no partial left behind either
    assert list(tmp_path.iterdir()) == []


def test_download_object_skips_the_check_for_a_reference_object(tmp_path):
    """A ``ref:`` id is a hash of the resolved PATH, not of the bytes, so
    there is nothing for the bytes to equal and checking would reject every
    reference object as corrupt."""
    client = _RecordingClient(b"arbitrary reference payload")
    dest = client.download_object("ref:abc123", tmp_path / "r.json")
    assert dest.read_bytes() == b"arbitrary reference payload"
    assert not ksu.is_content_object("ref:abc123")


# ==========================================================================
# The manifest grammar: contig_set, parameter_set_column, @prefix
# ==========================================================================


def _hash_source(toml_text: str):
    return cm.shard_plan(cm.load_manifest(toml_text))[0].hash_source


def test_contig_set_is_accepted_for_genome_and_refused_for_anything_else():
    genome = """
[[source]]
name = "g"
adapter = "arc"
emit = "genome"
entity_type = "genome"
kinds = ["entity"]
  [source.hash]
  contig_set = "contig_sequences"
"""
    assert _hash_source(genome) == ("contig_set", "contig_sequences", "genome")

    protein = genome.replace('entity_type = "genome"', 'entity_type = "protein"')
    with pytest.raises(cm.ManifestError, match="ONLY for entity_type 'genome'"):
        cm.load_manifest(protein) and cm.shard_plan(cm.load_manifest(protein))


def test_contig_set_is_exclusive_with_the_other_two_hash_modes():
    both = """
[[source]]
name = "g"
adapter = "arc"
emit = "genome"
entity_type = "genome"
kinds = ["entity"]
  [source.hash]
  contig_set = "contig_sequences"
  precomputed = "digest"
"""
    with pytest.raises(cm.ManifestError, match="exactly one of raw_column"):
        cm.shard_plan(cm.load_manifest(both))


def test_contig_set_refuses_a_string_and_matches_standardizers_entity_hash():
    """The failure this mode exists to prevent: ``entity_hash("genome", str)``
    does not raise, it hashes the SET OF CHARACTERS."""
    plan = cm.shard_plan(
        cm.load_manifest(
            """
[[source]]
name = "g"
adapter = "arc"
emit = "genome"
entity_type = "genome"
kinds = ["entity"]
  [source.hash]
  contig_set = "contig_sequences"
"""
        )
    )[0]

    expected = standardizers.entity_hash("genome", CONTIGS)
    assert cs._compute_entity_hash(plan, {"contig_sequences": CONTIGS}) == expected

    with pytest.raises(cs.ShardValueError, match="must hold a LIST"):
        cs._compute_entity_hash(plan, {"contig_sequences": "ACGTACGTAC"})


_RESULT_MANIFEST = """
[[source]]
name = "r"
adapter = "file"
path = "/nonexistent.parquet"
format = "parquet"
entity_type = "protein"
kinds = ["result"]
  [source.hash]
  raw_column = "aa"
  [source.result]
  result_type = "@const(annotation)"
  result_type_version = "@const(1.0)"
  source = "@const(bakta/1.0)"
  payload = "@json(aa)"
  %s
"""


def test_parameter_set_column_plans_and_is_exclusive_with_the_static_form():
    plan = cm.shard_plan(
        cm.load_manifest(_RESULT_MANIFEST % 'parameter_set_column = "params"')
    )[0]
    assert plan.parameter_set_column == "params"
    assert plan.parameter_set_hash is None

    with pytest.raises(cm.ManifestError, match="EXACTLY ONE"):
        cm.shard_plan(
            cm.load_manifest(
                _RESULT_MANIFEST
                % 'parameter_set = {}\n  parameter_set_column = "params"'
            )
        )
    with pytest.raises(cm.ManifestError, match="is required"):
        cm.shard_plan(cm.load_manifest(_RESULT_MANIFEST % "# neither"))


def test_parameter_set_column_matches_the_static_form_byte_for_byte():
    """A per-row set equal to a static one must produce the SAME canonical text
    and the SAME hash -- they go through one shared rule, and this is the
    assertion that says so."""
    static = cm.shard_plan(
        cm.load_manifest(
            _RESULT_MANIFEST % 'parameter_set = { taxonomy_id = "562" }'
        )
    )[0]
    per_row = cm.shard_plan(
        cm.load_manifest(_RESULT_MANIFEST % 'parameter_set_column = "params"')
    )[0]

    collected: dict[str, str] = {}
    digest = cs._row_parameter_set(
        per_row,
        {"params": {"taxonomy_id": "562"}},
        source_value="bakta/1.0",
        collector=collected,
    )
    assert digest == static.parameter_set_hash
    assert collected[digest] == static.parameter_set_json


def test_parameter_set_column_rejects_a_float_and_a_non_mapping():
    plan = cm.shard_plan(
        cm.load_manifest(_RESULT_MANIFEST % 'parameter_set_column = "params"')
    )[0]
    for bad in ({"evalue": 1e-5}, {"x": None}, "not a mapping", None):
        with pytest.raises(cs.ShardParameterSetError):
            cs._row_parameter_set(
                plan, {"params": bad}, source_value="bakta/1.0", collector=None
            )
    # and a ShardParameterSetError IS a ShardValueError, so the sharder's
    # existing one-row-skip handling applies unchanged
    assert issubclass(cs.ShardParameterSetError, cs.ShardValueError)


def test_prefix_derivation_prepends_and_preserves_null():
    derivation = cm.parse_derivation("@prefix(obj://,fasta_object_id)", where="t")
    assert derivation.args == ("obj://", "fasta_object_id")
    assert cs._derive(derivation, {"fasta_object_id": FASTA_ID}) == (
        f"obj://{FASTA_ID}"
    )
    assert cs._derive(derivation, {"fasta_object_id": None}) is None
    with pytest.raises(cm.ManifestError, match="exactly two arguments"):
        cm.parse_derivation("@prefix(obj://)", where="t")


# ==========================================================================
# The adapter: the four emits
# ==========================================================================


def test_a_missing_db_path_raises_and_creates_no_file(tmp_path):
    """``KorosArcStore``/``RunDatabase`` would CREATE it, and a mistyped path
    would then read as an empty store and report a clean zero."""
    missing = tmp_path / "nope" / "runs.sqlite"

    class _Store:
        pass

    store = _Store()
    store.db_path = missing
    store.cache_dir = tmp_path / "objects"
    store.client = FakeKBDLClient({})

    adapter = _adapter("s", store, emit="genome")
    with pytest.raises(cs.ManifestSourceError, match="read-only"):
        list(adapter.iter_records())
    assert not missing.exists()
    assert not missing.parent.exists()


def test_emit_genome_yields_one_record_per_genome_object(arc_store):
    (row,) = list(_adapter("g", arc_store, emit="genome").iter_records())
    assert row["contig_sequences"] == CONTIGS
    assert row["fasta_object_id"] == FASTA_ID
    assert row["ncbi_taxid"] == "562"
    assert row["n_contigs"] == 2
    assert row["total_length"] == sum(len(c) for c in CONTIGS)
    # The EARLIEST build record's timestamp, not the annotation record's.
    assert row["created_at"] == "2026-09-20T10:00:00Z"


def test_emit_protein_and_gene_yield_one_record_per_feature(arc_store):
    proteins = list(_adapter("p", arc_store, emit="protein").iter_records())
    genes = list(_adapter("n", arc_store, emit="gene").iter_records())

    assert [r["protein_translation"] for r in proteins] == TRANSLATIONS
    assert [r["dna_sequence"] for r in genes] == DNA
    # the gene row carries the translation too, so protein_entity_hash can be
    # derived from the SAME text the protein source hashes
    assert [r["protein_translation"] for r in genes] == TRANSLATIONS
    assert {r["created_at"] for r in proteins} == {"2026-09-20T10:00:00Z"}


def test_emit_annotation_yields_one_row_per_feature_tool_and_none_for_empty(
    arc_store,
):
    rows = list(_adapter("a", arc_store, emit="annotation").iter_records())

    pairs = sorted((r["feature_id"], r["tool"]) for r in rows)
    assert pairs == [
        (f"{GENOME_ID}_0", "BAKTA"),
        (f"{GENOME_ID}_0", "KOFAMSCAN"),
        (f"{GENOME_ID}_0", "TRANSYT"),
        (f"{GENOME_ID}_2", "PROKKA"),
    ]
    # feature _1's map is {} -- unannotated, and NOT a skip: there is nothing
    # there to lose.
    assert all(f"{GENOME_ID}_1" != r["feature_id"] for r in rows)

    bakta = next(r for r in rows if r["tool"] == "BAKTA")
    assert bakta["terms"] == {"FUNCTION": ["hypothetical protein"]}
    assert bakta["kbdl_job_id"] == "annotation-job-1"
    assert bakta["genome_object_id"] == arc_store.object_id
    assert bakta["protein_translation"] == TRANSLATIONS[0]
    assert bakta["created_at"] == "2026-09-23T15:13:54Z"
    assert bakta["source_label"] == "bakta/1.0"
    assert bakta["parameter_set"] == {}


def test_transyt_carries_taxonomy_id_and_other_tools_do_not(arc_store):
    rows = list(_adapter("a", arc_store, emit="annotation").iter_records())
    transyt = next(r for r in rows if r["tool"] == "TRANSYT")
    assert transyt["parameter_set"] == {"taxonomy_id": "562"}
    assert all(
        r["parameter_set"] == {} for r in rows if r["tool"] != "TRANSYT"
    )


def test_transyt_is_skipped_without_a_taxonomy_id(arc_store, tmp_path):
    """There is no default TRANSYT run -- the taxonomy id is part of what the
    result MEANS -- so a genome with no taxid loses its TRANSYT rows and
    keeps the rest."""
    _retaxonomise(arc_store, None)
    adapter = _adapter("a", arc_store, emit="annotation",
                       cache_dir=str(tmp_path / "fresh"))
    rows = list(adapter.iter_records())

    assert all(r["tool"] != "TRANSYT" for r in rows)
    assert adapter.skipped()[cs.ARC_SKIP_TRANSYT_WITHOUT_TAXONOMY] == 1
    assert len(rows) == 3


def test_a_transyt_taxonomy_id_the_validator_refuses_is_skipped_and_counted(
    arc_store, tmp_path
):
    """lake-5's shared validator is CALLED, not restated: a taxid with a
    leading zero is not an NCBI id and the row never loads."""
    _retaxonomise(arc_store, "0562")
    adapter = _adapter("a", arc_store, emit="annotation",
                       cache_dir=str(tmp_path / "fresh2"))
    rows = list(adapter.iter_records())

    assert all(r["tool"] != "TRANSYT" for r in rows)
    assert adapter.skipped()[cs.ARC_SKIP_TRANSYT_INVALID] == 1


def test_non_backfillable_kinds_are_counted_by_kind(arc_store):
    adapter = _adapter("g", arc_store, emit="genome")
    list(adapter.iter_records())
    skipped = adapter.skipped()
    assert skipped[f"{cs.ARC_SKIP_KIND_PREFIX}kbdl.model_build"] == 1
    assert skipped[f"{cs.ARC_SKIP_KIND_PREFIX}kbdl.fitness_analysis"] == 1
    assert skipped[f"{cs.ARC_SKIP_KIND_PREFIX}kbutillib.reconstruct"] == 1


def test_a_missing_result_file_is_counted_and_not_retryable(arc_store):
    arc_store.annotation_result.unlink()
    adapter = _adapter("a", arc_store, emit="annotation")
    rows = list(adapter.iter_records())
    assert rows == []
    assert adapter.skipped()[cs.ARC_SKIP_RESULT_FILE_MISSING] >= 1
    # NOT retryable: reading it again reads the same absence.
    assert adapter.incomplete_reasons() == ()


def test_an_unavailable_object_is_counted_and_leaves_the_read_incomplete(
    arc_store,
):
    arc_store.client.objects.clear()
    adapter = _adapter("g", arc_store, emit="genome")
    rows = list(adapter.iter_records())

    assert rows == []
    assert adapter.skipped()[cs.ARC_SKIP_OBJECT_UNAVAILABLE] >= 1
    assert adapter.incomplete_reasons()  # non-empty -> the unit is incomplete


def test_an_annotation_whose_prefix_resolves_to_nothing_is_counted(
    arc_store, tmp_path
):
    with gzip.open(arc_store.annotation_result, "wt", encoding="utf-8") as handle:
        json.dump({"ffffffffffffffff_0": {"BAKTA": {"FUNCTION": ["x"]}}}, handle)
    adapter = _adapter("a", arc_store, emit="annotation")
    assert list(adapter.iter_records()) == []
    assert adapter.skipped()[cs.ARC_SKIP_NO_GENOME_FOR_PREFIX] == 1


def test_a_feature_not_in_its_genome_object_is_counted(arc_store):
    with gzip.open(arc_store.annotation_result, "wt", encoding="utf-8") as handle:
        json.dump({f"{GENOME_ID}_99": {"BAKTA": {"FUNCTION": ["x"]}}}, handle)
    adapter = _adapter("a", arc_store, emit="annotation")
    assert list(adapter.iter_records()) == []
    assert adapter.skipped()[cs.ARC_SKIP_FEATURE_NOT_IN_GENOME] == 1


def test_a_genome_with_no_fasta_pointer_is_skipped_not_invented(
    arc_store, tmp_path
):
    """A ledger-only genome emits proteins, genes and annotations, but a
    `genome` row needs a real fasta_object_id and one is never fabricated."""
    other = _canonical_genome("y")
    # DISTINCT feature ids: two objects claiming one feature id is a refusal
    # (see test_two_genome_objects_claiming_one_feature_id_are_refused), and
    # this test is about a missing FASTA pointer, not a conflict.
    for index, feature in enumerate(other["features"]):
        feature["id"] = f"ffffffffffffffff_{index}"
    for index, contig in enumerate(other["contigs"]):
        contig["id"] = f"ffffffffffffffff_c{index}"
    other_id = hashlib.sha256(_object_bytes(other) + b"salt").hexdigest()
    arc_store.client.objects[other_id] = _object_bytes(other)
    tarball = _make_tarball(
        tmp_path / "backup.tar.gz",
        ledger_key="ledger-only-genome",
        genome_object_id=other_id,
    )

    adapter = _adapter(
        "g", arc_store, emit="genome", tarballs=str(tarball),
        cache_dir=str(tmp_path / "obj2"),
    )
    rows = list(adapter.iter_records())

    assert [r["genome_object_id"] for r in rows] == [arc_store.object_id]
    assert adapter.skipped()[cs.ARC_SKIP_FASTA_POINTER_MISSING] == 1


def test_a_configured_tarball_that_does_not_exist_is_an_error(arc_store, tmp_path):
    adapter = _adapter(
        "g", arc_store, emit="genome", tarballs=str(tmp_path / "absent.tar.gz")
    )
    with pytest.raises(cs.ManifestSourceError, match="does not exist"):
        list(adapter.iter_records())


def test_tarball_annotations_are_read_and_dated_from_the_ledger(
    arc_store, tmp_path
):
    tarball = _make_tarball(
        tmp_path / "backup.tar.gz",
        ledger_key="ledger-genome",
        genome_object_id=arc_store.object_id,
    )
    adapter = _adapter("a", arc_store, emit="annotation", tarballs=str(tarball))
    rows = list(adapter.iter_records())

    rast = [r for r in rows if r["tool"] == "RAST"]
    assert len(rast) == 1
    assert rast[0]["terms"] == {"role": ["transporter"]}
    assert rast[0]["created_at"] == "2026-09-23T13:07:00Z"
    assert rast[0]["kbdl_job_id"] == "ann-job-9"
    assert rast[0]["protein_translation"] == TRANSLATIONS[1]
    assert rast[0]["source_label"] == "rast/1.0"


def test_an_unusable_ledger_done_ts_falls_back_to_the_run_start_and_counts_it(
    arc_store, tmp_path
):
    tarball = _make_tarball(
        tmp_path / "backup.tar.gz",
        ledger_key="ledger-genome",
        genome_object_id=arc_store.object_id,
        done_ts=None,
    )
    adapter = _adapter("a", arc_store, emit="annotation", tarballs=str(tarball))
    rows = list(adapter.iter_records())

    rast = next(r for r in rows if r["tool"] == "RAST")
    assert rast["created_at"] == adapter._run_start
    assert adapter.skipped()[cs.ARC_SKIP_TIMESTAMP_FALLBACK] == 1


def test_two_genome_objects_claiming_one_feature_id_are_refused(
    arc_store, tmp_path
):
    """Never last-write-wins: a feature id is the join key from an annotation
    to its protein, and guessing would attribute annotations to the wrong
    organism's sequence."""
    twin = _canonical_genome("twin")  # SAME feature ids
    twin_id = hashlib.sha256(_object_bytes(twin) + b"twin").hexdigest()
    arc_store.client.objects[twin_id] = _object_bytes(twin)
    tarball = _make_tarball(
        tmp_path / "backup.tar.gz",
        ledger_key="twin-genome",
        genome_object_id=twin_id,
    )
    adapter = _adapter(
        "g", arc_store, emit="genome", tarballs=str(tarball),
        cache_dir=str(tmp_path / "obj3"),
    )
    with pytest.raises(cs.ArcFeatureIdConflictError) as excinfo:
        list(adapter.iter_records())
    assert f"{GENOME_ID}_0" in str(excinfo.value)
    assert set(excinfo.value.object_ids) == {arc_store.object_id, twin_id}


def test_each_needed_object_is_fetched_exactly_once(arc_store):
    adapter = _adapter("a", arc_store, emit="annotation")
    list(adapter.iter_records())
    assert arc_store.client.requested == [arc_store.object_id]


def test_a_cached_object_whose_digest_matches_is_reused(arc_store):
    list(_adapter("g", arc_store, emit="genome").iter_records())
    assert arc_store.client.requested == [arc_store.object_id]
    # a second adapter over the same cache dir makes no request at all
    list(_adapter("p", arc_store, emit="protein").iter_records())
    assert arc_store.client.requested == [arc_store.object_id]


def test_a_cached_object_whose_digest_differs_is_refetched(arc_store):
    list(_adapter("g", arc_store, emit="genome").iter_records())
    (arc_store.cache_dir / f"{arc_store.object_id}.json").write_bytes(b"corrupt")

    list(_adapter("p", arc_store, emit="protein").iter_records())
    assert arc_store.client.requested == [arc_store.object_id] * 2


def test_the_watermark_is_the_max_updated_at_of_the_records_read(arc_store):
    adapter = _adapter("a", arc_store, emit="annotation")
    list(adapter.iter_records())
    assert adapter.watermark() == "2026-09-23T15:13:54Z"


# ==========================================================================
# Selection, --limit, the frozen list, and the watermark checkpoint
# ==========================================================================


class FakeRunContext:
    """The parts of ``BackfillRunContext`` an adapter actually touches.

    A fake rather than the real one because the real one needs a run id, an
    execution state directory and a unit plan to exist; the adapter's whole
    contract with it is ``record_limit``, ``get_state``, ``put_state`` and
    four attributes.
    """

    def __init__(self, *, limit=None, tenant="kbaseincubator", dataset="prod"):
        self.limit = limit
        self.tenant = tenant
        self.dataset = dataset
        self.out_dir = None
        self.state: dict[str, dict] = {}

    def record_limit(self, source: str):
        return self.limit

    def get_state(self, source: str) -> dict:
        return dict(self.state.get(source) or {})

    def put_state(self, source: str, value) -> None:
        self.state[source] = dict(value)


def _bound(name, store, context, *, emit, **locator):
    adapter = _adapter(name, store, emit=emit, **locator)
    adapter.bind_run_context(context)
    return adapter


def test_since_filters_the_selection_inclusively(arc_store):
    """`since` narrows what is EMITTED. The annotation record is at
    2026-09-23, the build record at 2026-09-20."""
    late = _adapter("a", arc_store, emit="annotation", since="2026-09-23T00:00:00Z")
    assert len(list(late.iter_records())) == 4

    after = _adapter("a", arc_store, emit="annotation", since="2026-09-24T00:00:00Z")
    assert list(after.iter_records()) == []

    # inclusive on the boundary -- fill-only ingest discards a row already
    # present, while an exclusive bound that was off by a tick would drop one
    # permanently
    exact = _adapter("a", arc_store, emit="annotation", since="2026-09-23T15:13:54Z")
    assert len(list(exact.iter_records())) == 4


def test_a_late_annotation_joins_without_emitting_an_unselected_genome(arc_store):
    """THE INDEX IGNORES `since`; THE SELECTION DOES NOT. The annotation is
    inside the window and joins to a genome built before it -- and that
    genome's own rows are NOT emitted, because its build record is outside."""
    since = "2026-09-23T00:00:00Z"
    annotations = _adapter("a", arc_store, emit="annotation", since=since)
    assert len(list(annotations.iter_records())) == 4

    genomes = _adapter("g", arc_store, emit="genome", since=since)
    emitted = list(genomes.iter_records())
    # The genome IS fetched (the annotations need its sequences) but no
    # genome row is emitted for it: the only genome rows are for SELECTED
    # build records, and there are none in this window.
    assert [r["genome_object_id"] for r in emitted] == [arc_store.object_id]
    proteins = _adapter("p", arc_store, emit="protein", since=since)
    assert len(list(proteins.iter_records())) == 3


def test_limit_applies_once_to_the_ordered_selection_shared_by_all_sources(
    arc_store,
):
    """`--limit 1` must select ONE arc record that ALL FOUR sources work from,
    so the loaded rows are one coherent record -- not four independent
    samples of four different records."""
    context = FakeRunContext(limit=1)
    genomes = _bound("g", arc_store, context, emit="genome")
    rows = list(genomes.iter_records())

    frozen = context.state["arc"]
    # (updated_at, record_id) order -> the build record at 2026-09-20 is first
    assert frozen[cs.ARC_STATE_SELECTION] == [1]
    assert frozen[cs.ARC_STATE_CAPPED] is True
    assert [r["genome_object_id"] for r in rows] == [arc_store.object_id]

    # the annotation source reads the SAME frozen selection and therefore
    # emits nothing: record 1 is a build record
    annotations = _bound("a", arc_store, context, emit="annotation")
    assert list(annotations.iter_records()) == []
    assert context.state["arc"][cs.ARC_STATE_SELECTION] == [1]

    # and a capped read is NOT exhaustion, so the unit is `partial`
    assert genomes.exhausted() is False


def test_the_frozen_selection_is_reused_and_never_requeried(arc_store):
    context = FakeRunContext()
    first = _bound("g", arc_store, context, emit="genome")
    list(first.iter_records())
    frozen = dict(context.state["arc"])

    # a record written AFTER the freeze must not appear in a resume
    connection = sqlite3.connect(arc_store.db_path)
    connection.execute(
        "INSERT INTO runs (record_id, run_uid, kind, artifacts, created_at, "
        "updated_at) VALUES (99, 'late', 'kbdl.build_genome', NULL, "
        "'2099-01-01T00:00:00Z', '2099-01-01T00:00:00Z')"
    )
    connection.commit()
    connection.close()

    second = _bound("g", arc_store, context, emit="genome")
    list(second.iter_records())
    assert context.state["arc"][cs.ARC_STATE_SELECTION] == frozen[
        cs.ARC_STATE_SELECTION
    ]
    assert context.state["arc"][cs.ARC_STATE_UPPER_BOUND] == frozen[
        cs.ARC_STATE_UPPER_BOUND
    ]


def test_an_uncapped_complete_run_writes_a_candidate_that_promotion_makes_real(
    arc_store,
):
    context = FakeRunContext()
    adapter = _bound("g", arc_store, context, emit="genome")
    list(adapter.iter_records())

    key = adapter._checkpoint_key()
    assert cs.arc_checkpoint_path(key, candidate=True).is_file()
    # UNPROMOTED is unreadable: whether the rows landed is not something the
    # adapter can know.
    assert cs.read_arc_checkpoint(key) == {}

    # the adapter reports the key it wrote, which is how it reaches the driver
    assert adapter.checkpoint_candidates() == (key,)
    cs.promote_arc_checkpoints(adapter.checkpoint_candidates())
    promoted = cs.read_arc_checkpoint(key)
    assert promoted[cs.ARC_STATE_UPPER_BOUND] == context.state["arc"][
        cs.ARC_STATE_UPPER_BOUND
    ]
    assert promoted["key"]["dataset"] == "prod"


def test_a_capped_run_writes_no_checkpoint_at_all(arc_store):
    context = FakeRunContext(limit=1)
    adapter = _bound("g", arc_store, context, emit="genome")
    list(adapter.iter_records())

    key = adapter._checkpoint_key()
    assert not cs.arc_checkpoint_path(key, candidate=True).exists()
    assert adapter.checkpoint_candidates() == ()
    # even NAMED explicitly, a key with no candidate moves nothing
    cs.promote_arc_checkpoints([key])
    assert cs.read_arc_checkpoint(key) == {}


def test_an_object_unavailable_run_is_incomplete_and_writes_no_checkpoint(
    arc_store,
):
    """Every batch it wrote would verify and its loop reached the end, so by
    both of the older signals this unit is `done`. It is not."""
    arc_store.client.objects.clear()
    context = FakeRunContext()
    adapter = _bound("g", arc_store, context, emit="genome")
    list(adapter.iter_records())

    assert adapter.exhausted() is True          # the loop DID run off the end
    assert adapter.incomplete_reasons()         # and yet: incomplete
    assert context.state["arc"][cs.ARC_STATE_INCOMPLETE] is True
    key = adapter._checkpoint_key()
    assert not cs.arc_checkpoint_path(key, candidate=True).exists()


def test_a_scratch_checkpoint_is_not_used_for_a_production_target(arc_store):
    """Keyed by tenant+dataset, so this is true BY CONSTRUCTION rather than by
    a flag someone has to remember to pass."""
    scratch = FakeRunContext(dataset="scratch")
    adapter = _bound("g", arc_store, scratch, emit="genome")
    list(adapter.iter_records())
    cs.promote_arc_checkpoints(adapter.checkpoint_candidates())
    scratch_key = adapter._checkpoint_key()
    assert cs.read_arc_checkpoint(scratch_key)[cs.ARC_STATE_UPPER_BOUND]

    production = FakeRunContext(dataset="prod")
    other = _bound("g", arc_store, production, emit="genome")
    assert other._checkpoint_key() != scratch_key
    # the production run therefore starts from nothing, not from scratch's
    # watermark, and emits the genome again
    assert other._since() is None
    assert len(list(other.iter_records())) == 1


def test_an_explicit_since_overrides_a_promoted_checkpoint(arc_store):
    context = FakeRunContext()
    adapter = _bound("g", arc_store, context, emit="genome")
    list(adapter.iter_records())
    cs.promote_arc_checkpoints(adapter.checkpoint_candidates())

    remembered = _bound("g", arc_store, FakeRunContext(), emit="genome")
    assert remembered._since() is not None  # the checkpoint supplies it

    explicit = _bound(
        "g", arc_store, FakeRunContext(), emit="genome", since="2020-01-01T00:00:00Z"
    )
    assert explicit._since() == "2020-01-01T00:00:00Z"


def test_an_arc_limit_does_not_limit_tarball_processing_and_says_so(
    arc_store, tmp_path
):
    tarball = _make_tarball(
        tmp_path / "backup.tar.gz",
        ledger_key="ledger-genome",
        genome_object_id=arc_store.object_id,
    )
    context = FakeRunContext(limit=1)
    adapter = cs.ArcSourceAdapter(
        _source(
            "a", arc_store, emit="annotation", tarballs=str(tarball)
        ),
        client=arc_store.client,
    )
    adapter.bind_run_context(context)
    notes: list[str] = []
    adapter.log_note = notes.append

    rows = list(adapter.iter_records())

    # record 1 is the build record, so NO sqlite annotations are selected --
    # but the tarball is selected by DIGEST and is processed in full.
    assert [r["tool"] for r in rows] == ["RAST"]
    assert notes and "tarball" in notes[0]


def test_tarball_completion_is_recorded_by_digest(arc_store, tmp_path):
    tarball = _make_tarball(
        tmp_path / "backup.tar.gz",
        ledger_key="ledger-genome",
        genome_object_id=arc_store.object_id,
    )
    context = FakeRunContext()
    adapter = _bound("a", arc_store, context, emit="annotation",
                     tarballs=str(tarball))
    list(adapter.iter_records())
    cs.promote_arc_checkpoints(adapter.checkpoint_candidates())

    digest = hashlib.sha256(tarball.read_bytes()).hexdigest()
    assert cs.read_arc_checkpoint(adapter._checkpoint_key())["tarballs"] == [digest]

    # a second run sees it as already completed, so it is not re-selected --
    # and renaming it changes nothing, because the digest is the identity
    moved = tarball.rename(tmp_path / "renamed.tar.gz")
    again = _bound("a", arc_store, FakeRunContext(), emit="annotation",
                   tarballs=str(moved))
    assert again._tarball_selection() == {}


# ==========================================================================
# The arc-kbdl preset, end to end
# ==========================================================================

PRESET = "arc-kbdl"

#: A SECOND genome, with a DIFFERENT taxonomy, so the run's per-row parameter
#: sets are two TRANSYT taxa plus the {} every other tool shares -- the three
#: registry rows the lake-5 amendment requires.
SECOND_PREFIX = "0f1e2d3c4b5a6978"
SECOND_FASTA = SECOND_PREFIX + "7" * 48
SECOND_TRANSLATIONS = ["MQIFVKTLTG", "MKVLATNQEE"]
SECOND_DNA = ["ATGCAAATTTTTGTGAAAACCCTGACCGGT", "ATGAAAGTGCTGGCGACCAATCAAGAAGAA"]


def _second_genome() -> dict:
    return {
        "genome_id": SECOND_PREFIX,
        "prefix": "bsu",
        "taxonomy": {"ncbi_taxid": 1423, "genus": "Bacillus"},
        "contigs": [
            {"id": f"{SECOND_PREFIX}_c0", "source_id": "c0", "length": 8,
             "sequence": "GGGGCCCC"}
        ],
        "features": [
            {
                "id": f"{SECOND_PREFIX}_{i}",
                "source_id": f"g{i}",
                "location": [],
                "dna_sequence": SECOND_DNA[i],
                "protein_translation": SECOND_TRANSLATIONS[i],
            }
            for i in range(2)
        ],
    }


@pytest.fixture
def two_genome_store(arc_store):
    """``arc_store`` plus a second genome with its own taxon and TRANSYT rows."""
    second = _second_genome()
    second_id = _object_id_for(second)
    arc_store.client.objects[second_id] = _object_bytes(second)

    results = arc_store.tmp_path / "job-results"
    build2 = results / "KBDLBuildGenome" / "build-job-2.json.gz"
    with gzip.open(build2, "wt", encoding="utf-8") as handle:
        json.dump(
            {
                "genomes": {
                    "B_subtilis": {
                        "genome_id": SECOND_PREFIX,
                        "prefix": "bsu",
                        "fasta_object_id": SECOND_FASTA,
                        "genome_object_id": second_id,
                        "taxonomy": second["taxonomy"],
                        "skani_hits": [],
                    }
                },
                "errors": {},
            },
            handle,
        )
    annotation2 = results / "KBDLGenomeAnnotation" / "annotation-job-2.json.gz"
    with gzip.open(annotation2, "wt", encoding="utf-8") as handle:
        json.dump(
            {f"{SECOND_PREFIX}_0": {"TRANSYT": {"EC": ["2.2.2.2"]}}}, handle
        )

    connection = sqlite3.connect(arc_store.db_path)
    for record_id, uid, kind, path, stamp in [
        (6, "build-job-2", "kbdl.build_genome", build2, "2026-09-21T10:00:00Z"),
        (7, "annotation-job-2", "kbdl.genome_annotation", annotation2,
         "2026-09-23T16:00:00Z"),
    ]:
        connection.execute(
            "INSERT INTO runs (record_id, run_uid, kind, artifacts, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (record_id, uid, kind, json.dumps({"result": str(path)}), stamp,
             stamp),
        )
    connection.commit()
    connection.close()
    arc_store.second_id = second_id
    return arc_store


@pytest.fixture
def preset_shard(two_genome_store, tmp_path, monkeypatch):
    """Shard the whole ``arc-kbdl`` preset over the fixture store.

    The preset is loaded AS SHIPPED -- package data, unedited -- and pointed
    at the fixture through the placeholders it already declares. Only the
    adapter CLASS is swapped, for the one thing a test cannot supply: a KBDL
    client. That is the same substitution
    ``test_clearinghouse_backfill.py`` makes for the ``file`` adapter.
    """
    store = two_genome_store
    # A REAL tarball, not an empty override: `${VAR:-default}` treats an
    # empty environment value as unset (see expand_placeholders), so the
    # preset's pod default would win and fail. Pointing the variable at a
    # fixture tarball is also the better test -- it drives the tarball path
    # through the shipped preset.
    tarball = _make_tarball(
        tmp_path / "preset-backup.tar.gz",
        ledger_key="ledger-genome",
        genome_object_id=store.object_id,
    )
    monkeypatch.setenv("KBU_ARC_DB", str(store.db_path))
    monkeypatch.setenv("KBU_ARC_TARBALL", str(tarball))
    monkeypatch.setenv("KBU_ARC_CACHE_DIR", str(tmp_path / "objects"))

    class _WiredArc(cs.ArcSourceAdapter):
        def __init__(self, source):
            super().__init__(source, client=store.client)

        def log_note(self, message):
            pass

    preset = next(p for p in cb.available_presets() if p.name == PRESET)
    manifest = cm.load_manifest(preset.path)
    out = tmp_path / "bronze"
    original = dict(cs._ADAPTER_CLASSES)
    cs._ADAPTER_CLASSES["arc"] = _WiredArc
    try:
        cs.shard_manifest(manifest, out, manifest_path=preset.path)
    finally:
        cs._ADAPTER_CLASSES.clear()
        cs._ADAPTER_CLASSES.update(original)
    return out


def _rows(out: Path, table: str) -> list[dict]:
    rows: list[dict] = []
    for path in sorted((out / table).glob("*.parquet")):
        rows.extend(pq.read_table(str(path)).to_pylist())
    return rows


def test_the_preset_shards_into_exactly_the_expected_tables(preset_shard):
    tables = sorted(p.name for p in preset_shard.iterdir() if p.is_dir())
    assert tables == sorted(
        [
            schema.table_name("genome", "entity"),
            schema.table_name("genome", "content"),
            schema.table_name("protein", "entity"),
            schema.table_name("protein", "content"),
            schema.table_name("gene", "entity"),
            schema.table_name("gene", "content"),
            schema.table_name("protein", "result"),
            schema.PARAMETER_SET_TABLE,
        ]
    )


def test_the_preset_writes_the_genome_pointer_taxon_and_contig_counts(
    preset_shard,
):
    content = {r["entity_hash"]: r for r in _rows(preset_shard, "genome_content")}
    assert len(content) == 2
    first = content[standardizers.entity_hash("genome", CONTIGS)]
    assert first["fasta_reference"] == f"obj://{FASTA_ID}"
    assert first["taxon_id"] == "562"
    assert first["n_contigs"] == 2
    assert first["total_length"] == sum(len(c) for c in CONTIGS)
    # unmapped columns are null -- the arc store carries neither
    assert first["assembly_accession"] is None
    assert first["is_closed"] is None


def test_the_preset_links_each_gene_to_its_protein_by_entity_hash(preset_shard):
    proteins = {r["entity_hash"] for r in _rows(preset_shard, "protein_entity")}
    genes = _rows(preset_shard, "gene_content")
    assert genes
    for gene in genes:
        assert gene["protein_entity_hash"] in proteins
    # and the link is the hash of the SAME translation the protein row used
    by_sequence = {
        r["sequence"]: r["entity_hash"]
        for r in _rows(preset_shard, "protein_content")
    }
    assert by_sequence[TRANSLATIONS[0]] == standardizers.entity_hash(
        "protein", TRANSLATIONS[0]
    )


def test_the_preset_labels_each_result_source_from_the_tool_versions_table(
    preset_shard,
):
    results = _rows(preset_shard, "protein_result")
    assert {r["source"] for r in results} == {
        "bakta/1.0",
        "kofamscan/1.0",
        "transyt/1.0",
        "prokka/1.0",
        # from the backup tarball, whose annotations the arc attributed to
        # RAST under the `role` ontology
        "rast/1.0",
    }
    assert {r["result_type"] for r in results} == {"annotation"}
    assert {r["result_type_version"] for r in results} == {"1.0"}
    payload = json.loads(
        next(r for r in results if r["source"] == "bakta/1.0")["payload"]
    )
    assert payload["terms"] == {"FUNCTION": ["hypothetical protein"]}
    assert payload["feature_id"] == f"{GENOME_ID}_0"


def test_two_transyt_taxa_plus_the_default_yield_exactly_three_registry_rows(
    preset_shard,
):
    """One registry row per DISTINCT per-row parameter set, exactly as for a
    static one."""
    registry = _rows(preset_shard, schema.PARAMETER_SET_TABLE)
    canonical = sorted(r["canonical_json"] for r in registry)
    assert canonical == ['{"taxonomy_id":"1423"}', '{"taxonomy_id":"562"}', "{}"]
    assert len({r["parameter_set_hash"] for r in registry}) == 3

    # and EVERY result row's hash is explained by one of them
    explained = {r["parameter_set_hash"] for r in registry}
    for result in _rows(preset_shard, "protein_result"):
        assert result["parameter_set_hash"] in explained


def test_the_registry_batch_is_listed_before_the_result_batches(preset_shard):
    run = cs.read_run_manifest(preset_shard)
    tables = [batch["table"] for batch in run["batches"]]
    assert tables.index(schema.PARAMETER_SET_TABLE) < tables.index(
        "protein_result"
    )


def test_run_json_carries_the_adapter_watermark(preset_shard):
    run = cs.read_run_manifest(preset_shard)
    watermarks = {
        outcome["source"]: outcome["watermark"] for outcome in run["sources"]
    }
    # the greatest updated_at among the records READ -- the later annotation
    assert set(watermarks.values()) == {"2026-09-23T16:00:00Z"}
    assert all(
        outcome["incomplete_reasons"] == [] for outcome in run["sources"]
    )


def test_the_preset_declares_every_tool_at_1_0_with_the_op_bf4_note():
    preset = next(p for p in cb.available_presets() if p.name == PRESET)
    manifest = cm.load_manifest(preset.path)
    assert manifest.backfill.tool_versions == {
        "BAKTA": "1.0",
        "PROKKA": "1.0",
        "DRAM2": "1.0",
        "KOFAMSCAN": "1.0",
        "TRANSYT": "1.0",
        "RAST": "1.0",
    }
    assert "OP-BF4" in preset.path.read_text(encoding="utf-8")
    assert manifest.backfill.pulls_from == ("kbhub arc store", "KBDL on poplar")
    assert manifest.backfill.needs_env == ()


def test_a_tool_the_versions_table_omits_defaults_to_1_0(arc_store):
    adapter = _adapter("a", arc_store, emit="annotation")
    assert adapter.tool_version("SOMETHING_NEW") == cm.DEFAULT_TOOL_VERSION
    adapter.bind_backfill_info(cm.BackfillInfo(tool_versions={"BAKTA": "1.9"}))
    assert adapter.tool_version("bakta") == "1.9"
    assert adapter.tool_version("DRAM2") == "1.0"


# ==========================================================================
# Skip accounting, estimates, units
# ==========================================================================


def test_every_arc_skip_reason_is_classified(arc_store):
    """An unclassified reason reconciles into nothing, so each one must land
    in exactly one group."""
    reasons = [
        value
        for name, value in vars(cs).items()
        if name.startswith("ARC_SKIP_") and isinstance(value, str)
        and not name.endswith("_PREFIX")
    ]
    assert reasons
    for reason in reasons:
        assert cs.classify_arc_skip(reason) != "unknown", reason
    assert cs.classify_arc_skip(f"{cs.ARC_SKIP_KIND_PREFIX}anything") == (
        "source-record"
    )
    assert cs.classify_arc_skip("something-invented") == "unknown"


def test_source_record_failures_are_counted_apart_from_row_drops(arc_store):
    """FOUR SOURCES READ ONE STORE: a per-row drop in one source says nothing
    about the other three, while a source-record failure applies to every
    source that reads the record. Summing them together would
    quadruple-count."""
    groups = {
        cs.classify_arc_skip(cs.ARC_SKIP_RESULT_FILE_MISSING),
        cs.classify_arc_skip(cs.ARC_SKIP_OBJECT_UNAVAILABLE),
    }
    assert groups == {"source-record", "target-row"}
    assert cs.ARC_SOURCE_RECORD_SKIPS.isdisjoint(cs.ARC_TARGET_ROW_SKIPS)
    assert cs.ARC_NOTED_REASONS.isdisjoint(cs.ARC_TARGET_ROW_SKIPS)
    # and the one retryable reason is a row drop, not a record failure
    assert cs.ARC_RETRYABLE_SKIPS <= cs.ARC_TARGET_ROW_SKIPS


def test_estimate_bytes_counts_the_store_and_estimate_rows_declines(arc_store):
    adapter = _adapter("g", arc_store, emit="genome")
    assert adapter.estimate_bytes() > arc_store.db_path.stat().st_size
    # An arc record is not a row for ANY of the four emits, so a record count
    # reported as a row count would be a confident wrong number.
    assert adapter.estimate_rows() is None
    assert adapter.estimate_rows_method() == "unavailable"


def test_the_arc_adapter_is_one_unit_and_refuses_a_slice(arc_store):
    adapter = _adapter("g", arc_store, emit="genome")
    assert adapter.units() == (cs.SINGLE_UNIT,)
    adapter.restrict_to_unit(None)
    adapter.restrict_to_unit(cs.SINGLE_UNIT)
    with pytest.raises(cs.ManifestSourceError, match="runs as ONE unit"):
        adapter.restrict_to_unit("0f")


def test_an_unknown_emit_is_refused_by_name(arc_store):
    with pytest.raises(cs.ManifestSourceError, match="genome, protein, gene"):
        list(_adapter("g", arc_store, emit="transcript").iter_records())


def test_get_adapter_builds_the_arc_adapter_and_binds_the_backfill_table(
    arc_store,
):
    source = _source("g", arc_store, emit="genome")
    info = cm.BackfillInfo(tool_versions={"BAKTA": "1.9"})
    adapter = cs.get_adapter(source, backfill=info)
    assert isinstance(adapter, cs.ArcSourceAdapter)
    assert adapter.backfill_info is info
    assert adapter.tool_version("BAKTA") == "1.9"


def test_unit_outcome_turns_a_retryable_reason_into_incomplete(tmp_path):
    """Exhausted, verified, and still not done -- the case neither of the
    older signals could express."""
    unit_dir = tmp_path / "unit"
    unit_dir.mkdir()
    (unit_dir / cs.RUN_MANIFEST_FILE).write_text(
        json.dumps(
            {
                "run_manifest_version": cs.RUN_MANIFEST_VERSION,
                "sources": [
                    {
                        "source": "arc-genomes",
                        "table": "genome_entity",
                        "emitted": 3,
                        "accepted": 3,
                        "exhausted": True,
                        "incomplete_reasons": ["genome object abc is gone"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    state, reasons = cb.unit_outcome(
        unit_dir, verification={"discrepancies": []}, limit=None
    )
    assert state == cb.UNIT_INCOMPLETE
    assert any("abc is gone" in reason for reason in reasons)

    # with no retryable reason the same unit is done
    (unit_dir / cs.RUN_MANIFEST_FILE).write_text(
        json.dumps(
            {
                "run_manifest_version": cs.RUN_MANIFEST_VERSION,
                "sources": [
                    {
                        "source": "arc-genomes",
                        "table": "genome_entity",
                        "emitted": 3,
                        "accepted": 3,
                        "exhausted": True,
                        "incomplete_reasons": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert cb.unit_outcome(
        unit_dir, verification={"discrepancies": []}, limit=None
    )[0] == cb.UNIT_DONE


# ==========================================================================
# The sharder's handling of a bad per-row parameter set, end to end
# ==========================================================================


def test_a_bad_per_row_parameter_set_skips_one_row_and_keeps_the_rest(tmp_path):
    """One unusable record is not a reason to abandon the other hundreds: a
    per-row set is source DATA, so the row is dropped and COUNTED under its
    own reason."""
    import pyarrow as pa

    src = tmp_path / "rows.parquet"
    pq.write_table(
        pa.table(
            {
                "aa": ["MKTAYIAKQR", "MSTNPKPQRK", "MVLSPADKTN"],
                "observed_at": ["2026-01-01T00:00:00Z"] * 3,
            }
        ),
        str(src),
    )

    # The parquet fixture cannot carry a mapping column, so the parameter set
    # is supplied by a tiny adapter over the same file -- which is exactly how
    # the arc adapter supplies it.
    class _ParamAdapter(cs.FileSourceAdapter):
        adapter_name = "file"
        _SETS = [
            {"taxonomy_id": "562"},
            {"evalue": 1e-5},  # a FLOAT -- refused by the parameter-set rule
            {},
        ]

        def iter_records(self):
            for index, row in enumerate(super().iter_records()):
                yield {**row, "params": self._SETS[index]}

    manifest = cm.load_manifest(
        f"""
[[source]]
name = "per-row"
adapter = "file"
path = "{src}"
format = "parquet"
entity_type = "protein"
kinds = ["result"]
observed_at_column = "observed_at"
  [source.hash]
  raw_column = "aa"
  [source.result]
  result_type = "@const(annotation)"
  result_type_version = "@const(1.0)"
  source = "@const(bakta/1.0)"
  payload = "@json(aa)"
  parameter_set_column = "params"
"""
    )
    out = tmp_path / "bronze"
    original = dict(cs._ADAPTER_CLASSES)
    cs._ADAPTER_CLASSES["file"] = _ParamAdapter
    try:
        cs.shard_manifest(manifest, out)
    finally:
        cs._ADAPTER_CLASSES.clear()
        cs._ADAPTER_CLASSES.update(original)

    results = _rows(out, "protein_result")
    assert len(results) == 2  # the float row is gone, the other two stay

    run = cs.read_run_manifest(out)
    skips = {entry["reason"]: entry["count"] for entry in run["skipped"]}
    assert skips[cs.SKIP_PARAMETER_SET] == 1

    # TWO distinct sets, TWO registry rows, and every result row explained
    registry = _rows(out, schema.PARAMETER_SET_TABLE)
    assert sorted(r["canonical_json"] for r in registry) == [
        '{"taxonomy_id":"562"}',
        "{}",
    ]
    explained = {r["parameter_set_hash"] for r in registry}
    assert all(r["parameter_set_hash"] in explained for r in results)


# ==========================================================================
# Checkpoint promotion is SCOPED TO THE UNIT THAT EARNED IT
# ==========================================================================
#
# `arc_checkpoint_dir()` is ONE directory under the clearinghouse state root,
# shared by every arc source of every manifest on the machine -- a watermark
# has to outlive the shard output it came from, so it cannot live in the unit
# directory. The driver promotes a candidate after a unit verifies, and the
# only thing that makes "after THIS unit verifies" different from "after ANY
# unit verifies" is that the keys travel with the unit, in its own run.json.
# These tests are about that difference. Without it: store B's run leaves a
# candidate, an unrelated manifest's unit finishes, B's watermark advances
# past rows that never landed, and nothing comes back for them -- the lake is
# append-only and fill-only, so the gap is permanent and reports clean.


class _FakeCap:
    """The two methods ``run_backfill`` calls on an ingest capability.

    Deliberately not the ledger-writing fake in
    ``test_clearinghouse_backfill.py``: nothing here reads a ledger, and the
    one thing these tests need from a capability is the knob that decides
    whether a unit reaches the promotion guard at all -- whether the run
    verified.
    """

    def __init__(self, *, verify_ok=True):
        self.verify_ok = verify_ok

    def _locus(self):
        return "in_pod"

    def ingest_shards(
        self,
        shard_dir,
        *,
        run_id=None,
        dry_run=False,
        reconcile=False,
        tenant=None,
        dataset=None,
        unit=None,
    ):
        run = cs.read_run_manifest(shard_dir)
        return {
            "tables": [
                {
                    "table": batch.get("table"),
                    "rows_ingested": batch.get("rows") or 0,
                }
                for batch in run.get("batches") or []
            ],
            "source_run_id": run_id,
            "namespace": f"{tenant}.{dataset}",
        }

    def verify_run(self, shard_dir, *, run_id=None, unit=None, dataset=None):
        return {"discrepancies": [] if self.verify_ok else ["a batch is short"]}


def _drive_preset(cap, preset_path, out, run_id, *, limit=None, dataset="prod"):
    """Run one manifest through the real driver, start to finish."""
    manifest = cm.load_manifest(preset_path)
    plan = cb.plan_units(manifest, limit=limit)
    cb.register_run(
        run_id=run_id,
        out=out,
        tenant="kbaseincubator",
        dataset=dataset,
        manifest=manifest,
        manifest_path=preset_path,
        slice_spec=None,
        limit=limit,
        preset=None,
        unit_kind=plan.kind,
        resume=False,
    )
    Path(out).mkdir(parents=True, exist_ok=True)
    cb.record_planned_units(out, run_id=run_id, units=plan.units)
    result = cb.run_backfill(
        cap,
        manifest=manifest,
        manifest_path=preset_path,
        plan=plan,
        out=out,
        run_id=run_id,
        limit=limit,
        resume=False,
        tenant="kbaseincubator",
        dataset=dataset,
    )
    return plan, result


def _recorded_keys(out, plan):
    """The checkpoint keys every unit of a finished run recorded, in order."""
    keys: list[str] = []
    for unit in plan.units:
        run = cs.read_run_manifest(cb.unit_dir_for(out, unit))
        for key in cs.arc_checkpoint_candidates(run):
            if key not in keys:
                keys.append(key)
    return keys


@pytest.fixture
def arc_preset(arc_store, tmp_path, monkeypatch):
    """The shipped ``arc-kbdl`` preset, wired to the fixture store.

    The same substitution ``preset_shard`` makes -- the preset AS SHIPPED,
    pointed at the fixture through the placeholders it already declares, with
    only the adapter CLASS swapped so it gets a fake KBDL client.
    """
    tarball = _make_tarball(
        tmp_path / "scope-backup.tar.gz",
        ledger_key="ledger-genome",
        genome_object_id=arc_store.object_id,
    )
    monkeypatch.setenv("KBU_ARC_DB", str(arc_store.db_path))
    monkeypatch.setenv("KBU_ARC_TARBALL", str(tarball))
    monkeypatch.setenv("KBU_ARC_CACHE_DIR", str(tmp_path / "scope-objects"))

    class _WiredArc(cs.ArcSourceAdapter):
        def __init__(self, source):
            super().__init__(source, client=arc_store.client)

        def log_note(self, message):
            pass

    preset = next(p for p in cb.available_presets() if p.name == PRESET)
    original = dict(cs._ADAPTER_CLASSES)
    cs._ADAPTER_CLASSES["arc"] = _WiredArc
    try:
        yield preset.path
    finally:
        cs._ADAPTER_CLASSES.clear()
        cs._ADAPTER_CLASSES.update(original)


@pytest.fixture
def file_preset():
    """The shipped ``file-example`` preset: a manifest with NO arc source.

    It needs no wiring -- its parquet fixture ships beside it -- and it is the
    second manifest in the two-manifest test: an ordinary unrelated backfill,
    sharing nothing with the arc run but the checkpoint directory.
    """
    return next(p for p in cb.available_presets() if p.name == "file-example").path


def test_another_manifests_completed_unit_never_promotes_a_foreign_candidate(
    arc_preset, file_preset, tmp_path
):
    """THE DEFECT, end to end, through the real driver on both manifests.

    The arc run leaves a candidate and does NOT reach `done` (its batches did
    not verify, so records may be missing from the window its watermark would
    claim). An unrelated manifest then finishes a unit. The arc candidate must
    still be a candidate afterwards: the file manifest earned nothing about
    the arc store, and promoting what happened to be lying in the shared
    checkpoint directory would advance the arc watermark past rows that never
    landed.
    """
    arc_plan, arc_result = _drive_preset(
        _FakeCap(verify_ok=False), arc_preset, tmp_path / "arc-out", "RARC"
    )
    # Not done -- so the promotion guard was never reached for this unit.
    assert {u["state"] for u in arc_result["units"]} == {cb.UNIT_INCOMPLETE}
    # ...and yet the adapter DID write a candidate: the read was uncapped,
    # exhausted and clean, which is all the adapter can know.
    stranded = _recorded_keys(tmp_path / "arc-out", arc_plan)
    assert len(stranded) == 1, stranded
    key = stranded[0]
    assert cs.arc_checkpoint_path(key, candidate=True).is_file()
    assert cs.read_arc_checkpoint(key) == {}

    file_plan, file_result = _drive_preset(
        _FakeCap(), file_preset, tmp_path / "file-out", "RFILE"
    )
    # The second manifest's unit really is done, so it really did reach the
    # promotion guard -- the test would be vacuous otherwise.
    assert {u["state"] for u in file_result["units"]} == {cb.UNIT_DONE}
    # It recorded no checkpoint keys, because it has no arc source.
    assert _recorded_keys(tmp_path / "file-out", file_plan) == []

    # THE ASSERTION: the arc candidate survived, unpromoted.
    assert cs.arc_checkpoint_path(key, candidate=True).is_file()
    assert not cs.arc_checkpoint_path(key).exists()
    assert cs.read_arc_checkpoint(key) == {}


def test_a_completed_arc_unit_promotes_its_own_key_and_no_other(
    arc_preset, tmp_path
):
    """Exactly one candidate moves, and it is the one this unit earned."""
    foreign = "f" * 64
    directory = cs.arc_checkpoint_dir()
    directory.mkdir(parents=True, exist_ok=True)
    cs.arc_checkpoint_path(foreign, candidate=True).write_text(
        json.dumps({cs.ARC_STATE_UPPER_BOUND: "2099-01-01T00:00:00Z"}),
        encoding="utf-8",
    )

    plan, result = _drive_preset(
        _FakeCap(), arc_preset, tmp_path / "arc-out", "ROWN"
    )
    assert {u["state"] for u in result["units"]} == {cb.UNIT_DONE}

    keys = _recorded_keys(tmp_path / "arc-out", plan)
    assert len(keys) == 1, keys
    mine = keys[0]
    assert mine != foreign
    # mine: promoted, and readable
    assert cs.read_arc_checkpoint(mine)[cs.ARC_STATE_UPPER_BOUND]
    assert not cs.arc_checkpoint_path(mine, candidate=True).exists()
    # the foreign one: untouched, and still unreadable
    assert cs.arc_checkpoint_path(foreign, candidate=True).is_file()
    assert cs.read_arc_checkpoint(foreign) == {}


def test_the_four_arc_sources_of_one_unit_report_one_shared_key(
    arc_preset, tmp_path
):
    """One store, one identity: the preset's four sources earn ONE watermark.

    The key is the store's identity, not the source's, so a unit that read it
    through four sources must not promote four times or record four keys.
    """
    plan, _ = _drive_preset(_FakeCap(), arc_preset, tmp_path / "arc-out", "RFOUR")
    unit = plan.units[0]
    run = cs.read_run_manifest(cb.unit_dir_for(tmp_path / "arc-out", unit))
    reported = [
        outcome[cs.ARC_CHECKPOINT_CANDIDATES_FIELD]
        for outcome in run["sources"]
    ]
    assert len(reported) > 1               # several sources read the one store
    assert all(entry == reported[0] for entry in reported)
    assert cs.arc_checkpoint_candidates(run) == reported[0]


def test_promotion_of_an_unknown_key_moves_nothing_and_is_idempotent(arc_store):
    context = FakeRunContext()
    adapter = _bound("g", arc_store, context, emit="genome")
    list(adapter.iter_records())
    key = adapter._checkpoint_key()

    # an unknown key: nothing to move, and NOT an error
    assert cs.promote_arc_checkpoints(["0" * 64]) == []
    assert cs.arc_checkpoint_path(key, candidate=True).is_file()

    first = cs.promote_arc_checkpoints([key])
    assert first == [cs.arc_checkpoint_path(key)]
    promoted = cs.read_arc_checkpoint(key)

    # twice: the candidate is gone, so the second call moves nothing and
    # leaves the promoted checkpoint exactly as it was.
    assert cs.promote_arc_checkpoints([key, key]) == []
    assert cs.read_arc_checkpoint(key) == promoted


def test_promotion_has_no_zero_argument_form_anywhere(arc_store):
    """The glob-everything form is GONE, not merely unused.

    Asserted twice over, because either alone would pass while the defect was
    reachable: the signature refuses the call, and no caller in the shipped
    tree makes it.
    """
    with pytest.raises(TypeError):
        cs.promote_arc_checkpoints()

    package = Path(cs.__file__).resolve().parents[3]
    assert package.name == "kbutillib", package
    offenders = [
        f"{path}:{number}"
        for path in sorted(package.rglob("*.py"))
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        )
        if "promote_arc_checkpoints()" in line
    ]
    assert offenders == []
