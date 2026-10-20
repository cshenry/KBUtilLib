"""Offline tests for layer 3: batched Maestro research producing gene facts.

NO REAL MAESTRO IS EVER INVOKED HERE, by two independent mechanisms:

1. Every test that submits passes ``maestro_exe`` explicitly, pointing at a
   script this module writes under ``tmp_path``.
2. ``tests/literature/conftest.py`` puts a *refusing* fake named ``maestro``
   first on PATH for every test in this package, so a call that forgot the
   argument resolves to a script that exits 97 instead of queueing a real,
   paid task on the fleet.  :class:`TestPathGuard` asserts that guard is
   actually in force, which is the only way to know mechanism 2 is live.

The submit tests use the REAL subprocess path to a fake executable -- actual
process, actual argv, actual stdout -- because the argv is the product here
and a Python-level stub of ``subprocess.run`` would assert nothing about it.
Collection in most tests uses an injected runner answering ``git show`` from a
dict; :class:`TestNoInjection` is the one that matters most and injects
nothing: a real temporary git repository, a real commit on a real branch, the
real default ``subprocess`` runner, a real
:class:`~kbutillib.domains.literature.europepmc.EuropePMCClient` with only the
HTTP transport replayed, and a real cache file on disk.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
import requests
import requests.adapters

from kbutillib.core import shared_env_utils
from kbutillib.domains.literature.annotate import (
    EVIDENCE_CLASSES,
    Evidence,
    GeneEntry,
    PaperAnnotation,
    PaperEntry,
    PaperIds,
)
from kbutillib.domains.literature.cache import LiteratureCache
from kbutillib.domains.literature.europepmc import EuropePMCClient
from kbutillib.domains.literature.facts import (
    FACT_TYPES,
    FACTS_JSON_SCHEMA,
    GENE_FACTS_TABLE,
    GeneFacts,
)
from kbutillib.domains.literature.maestro_batch import (
    BATCH_SCHEMA_VERSION,
    DEFAULT_BATCH_SIZE,
    DEFAULT_MACHINE,
    DEFAULT_MAESTRO_EXE,
    DEFAULT_OUT_DIR,
    FORBIDDEN_MAESTRO_FLAGS,
    MAESTRO_API_TYPE,
    MANIFEST_VERSION,
    MAX_PROMPT_BYTES,
    WORKER_ENV_KEYS,
    BatchHandle,
    MaestroBatchUtils,
    MaestroBatchUtilsImpl,
    build_batch_prompt,
    collect_gene_facts_batches,
    submit_gene_facts_batches,
)

from .test_europepmc import load_fixture

ORGANISM: Dict[str, Any] = {
    "name": "Acinetobacter baylyi",
    "synonyms": ["ADP1"],
    "taxon_id": 62977,
}

WORKER_ENV: Dict[str, str] = {
    "kbutillib_revision": "06214d3",
    "cache_path": "/home/worker/.kbutillib/gene_literature.sqlite",
    "paperblast_data_dir": "/data/paperblast",
    "mmseqs_executable": "/usr/local/bin/mmseqs",
    "snapshot_digest": "sha256:feed0001",
}


def genes(count: int, *, sequence: Optional[str] = "MKTAYIAKQRQ") -> List[Dict[str, Any]]:
    """*count* simple genes, ``g1`` .. ``gN``."""
    return [
        {
            "id": f"g{index}",
            "aliases": [f"sym{index}"],
            "sequence": sequence,
        }
        for index in range(1, count + 1)
    ]


# ── the fake maestro executable ──────────────────────────────────────────

#: Written to ``tmp_path`` and executed through the real ``subprocess``.
#: Records every argv it was given, one JSON array per line, then prints what
#: the test told it to print.  ``@STDOUT@`` is a Python expression evaluated
#: in the script with ``calls`` (this invocation's 1-based number) in scope,
#: so one fake can answer a different task id per batch.
_FAKE_MAESTRO = '''#!@INTERPRETER@
"""A FAKE maestro. Submits nothing, anywhere. Written by test_maestro_batch."""
import json
import sys

LOG = @LOG@
with open(LOG, "a") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
with open(LOG) as handle:
    calls = sum(1 for line in handle if line.strip())
sys.stdout.write(@STDOUT@)
sys.exit(@EXIT@)
'''

_DEFAULT_STDOUT_EXPR = (
    'json.dumps({"task_id": "task-%03d" % calls, '
    '"branch_name": "fake/branch-%03d" % calls})'
)


def fake_maestro(
    tmp_path: Path,
    *,
    stdout_expr: str = _DEFAULT_STDOUT_EXPR,
    exit_code: int = 0,
    name: str = "fake-maestro",
) -> SimpleNamespace:
    """Write an executable fake submitter and return a handle on it.

    Args:
        tmp_path: The test's own temporary directory.
        stdout_expr: Python expression for what it prints (``calls`` is in
            scope).
        exit_code: What it exits with.
        name: File name.  Deliberately NOT ``maestro``: nothing in this
            module should ever be satisfied by a PATH lookup.

    Returns:
        ``SimpleNamespace(path, log, calls())`` -- ``calls()`` returns the
        recorded argv lists, in order.
    """
    directory = tmp_path / "fake-bin"
    directory.mkdir(exist_ok=True)
    log = directory / f"{name}.argv.jsonl"
    log.write_text("")
    script = directory / name
    script.write_text(
        _FAKE_MAESTRO.replace("@INTERPRETER@", sys.executable)
        .replace("@LOG@", repr(str(log)))
        .replace("@STDOUT@", stdout_expr)
        .replace("@EXIT@", str(exit_code))
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def calls() -> List[List[str]]:
        return [
            json.loads(line)
            for line in log.read_text().splitlines()
            if line.strip()
        ]

    return SimpleNamespace(path=str(script), log=log, calls=calls)


def fixed_stdout(task_id: str, branch: str) -> str:
    """A ``stdout_expr`` printing one fixed task id and branch."""
    return repr(json.dumps({"task_id": task_id, "branch_name": branch}))


# ── test doubles for collection ──────────────────────────────────────────


class FakeGit:
    """Answers ``git show <branch>:<path>`` from a dict, records every call.

    Refuses any argv whose first element is not git.  That refusal is the
    point: collection must never shell out to a submitter, and a future edit
    that made it do so would fail here rather than on the fleet.
    """

    def __init__(self, outputs: Optional[Dict[str, str]] = None) -> None:
        #: ``{"<branch>:<path>": text}``.  An absent key is a missing output.
        self.outputs = dict(outputs or {})
        self.calls: List[List[str]] = []

    def __call__(self, argv, timeout=None):
        argv = list(argv)
        self.calls.append(argv)
        if Path(argv[0]).name != "git":
            raise AssertionError(
                f"the collection runner was asked to run {argv[0]!r}; only "
                "git is allowed, and a maestro submitter never is"
            )
        assert argv[1] == "-C" and argv[3] == "show", argv
        spec = argv[4]
        if spec not in self.outputs:
            return subprocess.CompletedProcess(
                argv,
                128,
                "",
                f"fatal: path does not exist in '{spec}'",
            )
        return subprocess.CompletedProcess(argv, 0, self.outputs[spec], "")

    def specs(self) -> List[str]:
        """The ``branch:path`` specs asked for, in order."""
        return [call[4] for call in self.calls]


class FakeEuropePMC:
    """Stands in for the one method collection uses: ``resolve``."""

    def __init__(self, known=()) -> None:
        #: Canonical ids that resolve.  Anything else returns None.
        self.known = set(known)
        self.resolve_calls: List[str] = []

    def resolve(self, id):  # noqa: A002 - matches EuropePMCClient.resolve
        self.resolve_calls.append(id)
        if id not in self.known:
            return None
        return {"canonical_id": id, "canonical_ids": [id], "title": "A paper"}


def make_utils(tmp_path, *, runner=None, europepmc=None, **kwargs) -> MaestroBatchUtils:
    """A MaestroBatchUtils with an injected cache (and whatever else)."""
    return MaestroBatchUtils(
        cache=LiteratureCache(tmp_path / "gene_literature.sqlite"),
        europepmc=europepmc,
        runner=runner,
        config={},
        config_file=False,
        token_file=None,
        kbase_token_file=None,
        **kwargs,
    )


def submit(utils, *, exe, count=3, batch_size=DEFAULT_BATCH_SIZE, **kwargs):
    """``submit_gene_facts_batches`` with this module's defaults.

    ``genes=[]`` means exactly that -- an empty list is not replaced by
    *count* genes, because "submitting nothing" is one of the behaviours
    under test.
    """
    gene_list = kwargs.pop("genes", None)
    if gene_list is None:
        gene_list = genes(count)
    slices = kwargs.pop("papers_by_gene", None)
    options: Dict[str, Any] = {
        "repo": "KBUtilLib",
        "worker_env": dict(WORKER_ENV),
        "batch_size": batch_size,
        "maestro_exe": exe,
    }
    options.update(kwargs)
    return utils.submit_gene_facts_batches(gene_list, ORGANISM, slices, **options)


def fact(
    paper_id: str = "PMID:15210978",
    *,
    type: str = "function",  # noqa: A002 - mirrors the schema's field name
    statement: str = "DnaA binds oriC and initiates replication.",
    evidence_class: str = "direct",
    confidence: str = "high",
) -> Dict[str, Any]:
    """One fact object as a worker would write it."""
    return {
        "type": type,
        "statement": statement,
        "citations": [
            {
                "paper_id": paper_id,
                "evidence_class": evidence_class,
                "quote": "DnaA binds oriC.",
            }
        ],
        "confidence": confidence,
    }


def document(handle: BatchHandle, results: Dict[str, Any], **extra) -> str:
    """A worker output document for *handle*."""
    payload: Dict[str, Any] = {
        "schema_version": BATCH_SCHEMA_VERSION,
        "batch_id": handle.batch_id,
        "results": results,
    }
    payload.update(extra)
    return json.dumps(payload, indent=2)


def spec(handle: BatchHandle) -> str:
    """The ``git show`` spec collection will ask for."""
    return f"{handle.branch}:{handle.output_path}"


def annotation_for(gene_id: str, paper_id: str, **evidence) -> PaperAnnotation:
    """A one-gene layer-1 slice carrying one paper."""
    record = Evidence(
        source=evidence.pop("source", "paperblast"),
        evidence_class=evidence.pop("evidence_class", "homolog"),
        **evidence,
    )
    return PaperAnnotation(
        papers={
            paper_id: PaperEntry(
                ids=PaperIds(pmid=paper_id.partition(":")[2]),
                title="A homolog paper",
                genes={gene_id: [record]},
            )
        },
        genes={gene_id: GeneEntry(paper_ids=[paper_id])},
    )


# ── the PATH guard ───────────────────────────────────────────────────────


class TestPathGuard:
    """A missing ``maestro_exe`` cannot silently reach the real binary.

    The module's default is the bare name ``"maestro"``, which ``subprocess``
    resolves on PATH exactly as a shell would -- and this machine has a real
    one that submits real paid tasks.  ``tests/literature/conftest.py`` puts a
    refusing fake first on PATH for every test in this package; these two
    tests are what prove that guard is in force rather than merely written
    down.
    """

    def test_the_name_maestro_resolves_to_the_suites_fake(
        self, guard_maestro_on_path
    ):
        resolved = shutil.which(DEFAULT_MAESTRO_EXE)
        assert resolved is not None
        assert Path(resolved) == guard_maestro_on_path.script
        assert str(guard_maestro_on_path.directory) in resolved
        # The real submitter on an h100-class machine. Named explicitly so
        # the assertion is about the thing we are afraid of.
        assert resolved != "/home/chenry/.local/bin/maestro"

    def test_a_submit_with_no_maestro_exe_hits_the_guard_and_fails(
        self, tmp_path, guard_maestro_on_path
    ):
        utils = make_utils(tmp_path)
        before = guard_maestro_on_path.marker.read_text().count("\n")

        with pytest.raises(RuntimeError) as problem:
            utils.submit_gene_facts_batches(
                genes(1),
                ORGANISM,
                repo="KBUtilLib",
                worker_env=dict(WORKER_ENV),
            )

        message = str(problem.value)
        assert f"exited {guard_maestro_on_path.exit_code}" in message
        assert "PATH guard" in message
        # The guard, not the fleet, received the call.
        assert guard_maestro_on_path.marker.read_text().count("\n") == before + 1


# ── worker_env ───────────────────────────────────────────────────────────


class TestWorkerEnv:
    """``worker_env`` is required and must be complete, or nothing is sent."""

    def test_it_is_a_required_keyword(self, tmp_path):
        exe = fake_maestro(tmp_path)
        with pytest.raises(TypeError):
            make_utils(tmp_path).submit_gene_facts_batches(
                genes(1), ORGANISM, repo="KBUtilLib", maestro_exe=exe.path
            )
        assert exe.calls() == []

    @pytest.mark.parametrize("missing", WORKER_ENV_KEYS)
    def test_an_incomplete_mapping_is_refused_before_submission(
        self, tmp_path, missing
    ):
        exe = fake_maestro(tmp_path)
        incomplete = {
            key: value for key, value in WORKER_ENV.items() if key != missing
        }
        with pytest.raises(ValueError) as problem:
            submit(make_utils(tmp_path), exe=exe.path, worker_env=incomplete)
        assert missing in str(problem.value)
        assert exe.calls() == []

    def test_an_empty_value_counts_as_missing(self, tmp_path):
        exe = fake_maestro(tmp_path)
        blank = dict(WORKER_ENV, snapshot_digest="   ")
        with pytest.raises(ValueError) as problem:
            submit(make_utils(tmp_path), exe=exe.path, worker_env=blank)
        assert "snapshot_digest" in str(problem.value)
        assert exe.calls() == []

    def test_a_non_mapping_is_refused(self, tmp_path):
        exe = fake_maestro(tmp_path)
        with pytest.raises(ValueError):
            submit(make_utils(tmp_path), exe=exe.path, worker_env=["a", "b"])
        assert exe.calls() == []

    def test_the_manifest_records_exactly_the_required_keys(self, tmp_path):
        exe = fake_maestro(tmp_path)
        handles = submit(
            make_utils(tmp_path),
            exe=exe.path,
            count=1,
            worker_env=dict(WORKER_ENV, extra="ignored"),
        )
        assert handles[0].input_manifest.worker_env == WORKER_ENV


# ── submission ───────────────────────────────────────────────────────────


class TestSubmission:
    """One real subprocess per batch, with the argv Maestro expects."""

    def test_one_submit_per_batch_and_batch_size_is_honoured(self, tmp_path):
        exe = fake_maestro(tmp_path)
        handles = submit(make_utils(tmp_path), exe=exe.path, count=7, batch_size=3)

        assert len(exe.calls()) == 3
        assert [handle.gene_ids for handle in handles] == [
            ["g1", "g2", "g3"],
            ["g4", "g5", "g6"],
            ["g7"],
        ]
        # Every gene is in exactly one batch, and nothing else is.
        assert sorted(
            gene_id for handle in handles for gene_id in handle.gene_ids
        ) == sorted(gene["id"] for gene in genes(7))

    def test_the_default_batch_size_is_25(self, tmp_path):
        exe = fake_maestro(tmp_path)
        handles = submit(
            make_utils(tmp_path), exe=exe.path, count=26, batch_size=DEFAULT_BATCH_SIZE
        )
        assert DEFAULT_BATCH_SIZE == 25
        assert [len(handle.gene_ids) for handle in handles] == [25, 1]

    def test_the_argv_is_exactly_the_documented_submit_call(self, tmp_path):
        exe = fake_maestro(tmp_path)
        handles = submit(
            make_utils(tmp_path),
            exe=exe.path,
            count=1,
            machine="h100",
            backend="codex",
            timeout=1800,
            out_dir="agent-io/gene-facts",
        )
        argv = exe.calls()[0]
        prompt = argv[-1]
        assert argv[:-1] == [
            "submit",
            "--role",
            "researcher",
            "--backend",
            "codex",
            "--machine",
            "h100",
            "--repo",
            "KBUtilLib",
            "--success-criterion",
            "output-signal",
            "--expected-output",
            handles[0].output_path,
            "--timeout",
            "1800",
        ]
        assert prompt == build_batch_prompt(
            handles[0].input_manifest,
            batch_id=handles[0].batch_id,
            output_path=handles[0].output_path,
        )

    def test_no_auto_review_or_auto_merge_flag_is_ever_passed(self, tmp_path):
        exe = fake_maestro(tmp_path)
        submit(make_utils(tmp_path), exe=exe.path, count=5, batch_size=2)
        assert FORBIDDEN_MAESTRO_FLAGS == (
            "--auto-review",
            "--auto-merge",
            "--auto-merge-on-verdict",
        )
        for argv in exe.calls():
            for flag in FORBIDDEN_MAESTRO_FLAGS:
                assert flag not in argv
            # Not as a substring either -- '--auto-merge=true' would not be
            # caught by the membership test above.
            assert "--auto" not in " ".join(argv[:-1])

    def test_the_machine_and_backend_defaults_are_h100_and_codex(self, tmp_path):
        exe = fake_maestro(tmp_path)
        handles = submit(make_utils(tmp_path), exe=exe.path, count=1)
        argv = exe.calls()[0]
        assert DEFAULT_MACHINE == "h100"
        assert argv[argv.index("--machine") + 1] == "h100"
        assert argv[argv.index("--backend") + 1] == "codex"
        assert handles[0].machine == "h100"
        assert handles[0].backend == "codex"

    def test_the_handle_carries_what_the_submitter_said(self, tmp_path):
        exe = fake_maestro(tmp_path)
        handles = submit(make_utils(tmp_path), exe=exe.path, count=3, batch_size=1)
        assert [handle.task_id for handle in handles] == [
            "task-001",
            "task-002",
            "task-003",
        ]
        assert [handle.branch for handle in handles] == [
            "fake/branch-001",
            "fake/branch-002",
            "fake/branch-003",
        ]

    def test_the_batch_id_is_twelve_hex_characters_and_names_the_output(
        self, tmp_path
    ):
        exe = fake_maestro(tmp_path)
        handles = submit(make_utils(tmp_path), exe=exe.path, count=4, batch_size=1)
        for handle in handles:
            assert len(handle.batch_id) == 12
            int(handle.batch_id, 16)  # hex, or this raises
            assert handle.output_path == f"{DEFAULT_OUT_DIR}/{handle.batch_id}.json"
        assert len({handle.batch_id for handle in handles}) == 4

    def test_out_dir_is_honoured_and_never_absolute(self, tmp_path):
        exe = fake_maestro(tmp_path)
        handles = submit(
            make_utils(tmp_path), exe=exe.path, count=1, out_dir="/results/facts/"
        )
        assert handles[0].output_path.startswith("results/facts/")
        argv = exe.calls()[0]
        assert argv[argv.index("--expected-output") + 1] == handles[0].output_path

    def test_submitted_at_is_an_iso_timestamp(self, tmp_path):
        exe = fake_maestro(tmp_path)
        handles = submit(make_utils(tmp_path), exe=exe.path, count=1)
        assert datetime.fromisoformat(handles[0].submitted_at).tzinfo is not None

    def test_a_handle_round_trips_through_json(self, tmp_path):
        exe = fake_maestro(tmp_path)
        handle = submit(
            make_utils(tmp_path),
            exe=exe.path,
            count=1,
            papers_by_gene={"g1": annotation_for("g1", "PMID:15210978", identity=0.91)},
        )[0]
        restored = BatchHandle.model_validate_json(handle.model_dump_json())
        assert restored == handle
        # And as a plain mapping, which is what a caller writing JSON gets.
        assert BatchHandle.model_validate(json.loads(handle.model_dump_json())) == handle

    def test_noise_before_the_json_is_tolerated(self, tmp_path):
        exe = fake_maestro(
            tmp_path,
            stdout_expr=(
                '"queued; waiting for a worker\\n" + '
                'json.dumps({"task_id": "t-9", "branch_name": "b-9"})'
            ),
        )
        handles = submit(make_utils(tmp_path), exe=exe.path, count=1)
        assert (handles[0].task_id, handles[0].branch) == ("t-9", "b-9")

    def test_a_failed_submit_raises_and_names_the_live_batches(self, tmp_path):
        # Exits non-zero on its SECOND call, so one batch is already live.
        exe = fake_maestro(
            tmp_path,
            stdout_expr=(
                'json.dumps({"task_id": "t-1", "branch_name": "b-1"}) '
                'if calls == 1 else sys.exit(3)'
            ),
        )
        with pytest.raises(RuntimeError) as problem:
            submit(make_utils(tmp_path), exe=exe.path, count=2, batch_size=1)
        message = str(problem.value)
        assert "exited 3" in message
        assert "still live" in message and "task t-1" in message

    def test_stdout_with_no_task_id_raises(self, tmp_path):
        exe = fake_maestro(
            tmp_path, stdout_expr='json.dumps({"branch_name": "b"})'
        )
        with pytest.raises(RuntimeError, match="no task_id"):
            submit(make_utils(tmp_path), exe=exe.path, count=1)

    def test_stdout_with_no_branch_raises(self, tmp_path):
        exe = fake_maestro(tmp_path, stdout_expr='json.dumps({"task_id": "t"})')
        with pytest.raises(RuntimeError, match="no .*branch"):
            submit(make_utils(tmp_path), exe=exe.path, count=1)

    def test_unparseable_stdout_raises(self, tmp_path):
        exe = fake_maestro(tmp_path, stdout_expr='"submitted, I think"')
        with pytest.raises(RuntimeError, match="no task_id"):
            submit(make_utils(tmp_path), exe=exe.path, count=1)

    def test_a_missing_executable_raises_and_submits_nothing(self, tmp_path):
        with pytest.raises(RuntimeError, match="submission failed"):
            submit(
                make_utils(tmp_path),
                exe=str(tmp_path / "no-such-maestro"),
                count=1,
            )

    @pytest.mark.parametrize(
        "bad,message",
        [
            ({"repo": ""}, "repo is required"),
            ({"batch_size": 0}, "batch_size"),
        ],
    )
    def test_bad_arguments_are_refused_before_submission(
        self, tmp_path, bad, message
    ):
        exe = fake_maestro(tmp_path)
        with pytest.raises(ValueError, match=message):
            submit(make_utils(tmp_path), exe=exe.path, count=1, **bad)
        assert exe.calls() == []

    def test_a_repeated_gene_id_is_refused_before_submission(self, tmp_path):
        exe = fake_maestro(tmp_path)
        with pytest.raises(ValueError, match="duplicate gene id"):
            submit(
                make_utils(tmp_path),
                exe=exe.path,
                genes=[{"id": "g1"}, {"id": "g2"}, {"id": "g1"}],
            )
        assert exe.calls() == []

    def test_no_genes_submits_nothing(self, tmp_path):
        exe = fake_maestro(tmp_path)
        assert submit(make_utils(tmp_path), exe=exe.path, genes=[]) == []
        assert exe.calls() == []

    def test_an_oversized_prompt_fails_before_ANY_batch_is_submitted(
        self, tmp_path
    ):
        exe = fake_maestro(tmp_path)
        # The first batch is small; the second carries a protein sequence far
        # past the limit.  Nothing may be submitted, including the first.
        payload = [
            {"id": "g1", "sequence": "MKT"},
            {"id": "g2", "sequence": "M" * (MAX_PROMPT_BYTES + 10_000)},
        ]
        with pytest.raises(ValueError) as problem:
            submit(
                make_utils(tmp_path), exe=exe.path, genes=payload, batch_size=1
            )
        message = str(problem.value)
        assert str(MAX_PROMPT_BYTES) in message
        assert "Nothing was submitted" in message
        assert exe.calls() == []

    def test_the_module_level_function_is_the_same_submission(self, tmp_path):
        exe = fake_maestro(tmp_path)
        handles = submit_gene_facts_batches(
            genes(2),
            ORGANISM,
            repo="KBUtilLib",
            worker_env=dict(WORKER_ENV),
            batch_size=1,
            maestro_exe=exe.path,
            # utils kwargs -- the seam a caller/test uses instead of patching
            cache=LiteratureCache(tmp_path / "gene_literature.sqlite"),
            config={},
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )
        assert len(handles) == 2
        assert len(exe.calls()) == 2


# ── the prompt ───────────────────────────────────────────────────────────


@pytest.fixture
def prompt_handle(tmp_path) -> BatchHandle:
    """One submitted handle whose prompt the content tests read."""
    exe = fake_maestro(tmp_path)
    return submit(
        make_utils(tmp_path),
        exe=exe.path,
        genes=[
            {"id": "ACIAD0380", "aliases": ["dnaA"], "sequence": "MKTAYIAKQRQ"},
            {"id": "ACIAD0381", "aliases": ["gyrB"]},
        ],
        papers_by_gene={
            "ACIAD0380": annotation_for(
                "ACIAD0380",
                "PMID:15210978",
                evidence_class="homolog",
                homolog_id="b3702",
                homolog_organism="Escherichia coli",
                identity=0.62,
                snippet="DnaA is the replication initiator.",
            )
        },
    )[0]


@pytest.fixture
def prompt_text(prompt_handle) -> str:
    """The exact prompt text that was submitted for ``prompt_handle``."""
    return build_batch_prompt(
        prompt_handle.input_manifest,
        batch_id=prompt_handle.batch_id,
        output_path=prompt_handle.output_path,
    )


class TestPrompt:
    """The prompt is self-contained: the worker can read nothing we read."""

    def test_it_carries_the_output_json_schema(self, prompt_text):
        rendered = json.dumps(FACTS_JSON_SCHEMA, indent=2, sort_keys=True)
        assert rendered in prompt_text

    def test_it_carries_every_fact_type(self, prompt_text):
        for name in FACT_TYPES:
            assert f"`{name}`" in prompt_text

    def test_it_carries_the_genes_and_their_layer_one_slices_as_json(
        self, prompt_text
    ):
        assert '"gene_id": "ACIAD0380"' in prompt_text
        assert '"gene_id": "ACIAD0381"' in prompt_text
        assert '"aliases": [\n      "dnaA"\n    ]' in prompt_text
        assert '"protein_sequence": "MKTAYIAKQRQ"' in prompt_text
        assert '"paper_id": "PMID:15210978"' in prompt_text
        assert '"homolog_organism": "Escherichia coli"' in prompt_text
        assert '"identity": 0.62' in prompt_text
        # And the gene with no slice still says so, rather than being absent.
        assert prompt_text.count('"layer1_papers"') == 2

    def test_it_states_the_evidence_class_rules_the_task_fixes(self, prompt_text):
        for name in EVIDENCE_CLASSES:
            assert f"`{name}`" in prompt_text
        assert "characterises a HOMOLOG, in another organism" in prompt_text
        assert "genome-wide table" in prompt_text
        assert "is a `mention`" in prompt_text

    def test_it_names_the_kbutillib_tools_the_worker_has(self, prompt_text):
        for name in (
            "GeneLiteratureUtils",
            "EuropePMCClient",
            "PaperBLASTUtils",
            "kbutillib.domains.literature",
        ):
            assert name in prompt_text

    def test_it_says_KBUtilLib_has_NO_full_text_or_supplement_methods(
        self, prompt_text
    ):
        # C7: the methods do not exist in this version.  A prompt that let the
        # worker believe otherwise costs the batch on an AttributeError.
        assert "no full-text method and no supplementary-file method" in prompt_text
        assert "do not assume the existence of" in prompt_text
        assert "get_full_text" in prompt_text
        # ... but it may fetch open access itself, with its own tools.
        assert (
            "https://www.ebi.ac.uk/europepmc/webservices/rest/<PMCID>/fullTextXML"
            in prompt_text
        )
        assert "supplementaryFiles" in prompt_text
        assert "material you needed was unavailable" in prompt_text

    def test_it_demands_ids_only_for_citations(self, prompt_text):
        assert "`PMID:<digits>`" in prompt_text
        assert "`PMCID:PMC<digits>`" in prompt_text
        assert "`DOI:<doi>`" in prompt_text
        assert "bare URL" in prompt_text

    def test_it_makes_the_worker_check_its_environment_first(self, prompt_text):
        # C8
        assert "python -c 'import kbutillib;" in prompt_text
        for key, value in WORKER_ENV.items():
            assert key in prompt_text
            assert value in prompt_text
        assert "error entry for EVERY" in prompt_text
        assert "environment mismatch" in prompt_text

    def test_it_demands_one_entry_per_gene_written_and_committed(
        self, prompt_text, prompt_handle
    ):
        # C9
        assert "exactly one entry per assigned gene id" in prompt_text
        assert "including genes you failed" in prompt_text
        assert f'"batch_id": "{prompt_handle.batch_id}"' in prompt_text
        assert f"git add {prompt_handle.output_path}" in prompt_text
        assert "git commit" in prompt_text
        assert "Do NOT create or switch branches" in prompt_text
        assert "do NOT push" in prompt_text

    def test_it_tells_the_worker_not_to_look_for_a_prd(self, prompt_text):
        assert "self-contained" in prompt_text
        assert "Do not look for a PRD" in prompt_text

    def test_the_manifest_carries_everything_collection_needs(
        self, prompt_handle
    ):
        # C10
        manifest = prompt_handle.input_manifest
        assert manifest.manifest_version == MANIFEST_VERSION
        assert [gene.id for gene in manifest.genes] == ["ACIAD0380", "ACIAD0381"]
        assert manifest.organism.name == ORGANISM["name"]
        assert set(manifest.paper_slices) == {"ACIAD0380"}
        assert manifest.worker_env == WORKER_ENV
        assert manifest.backend == "codex" and manifest.model == "codex"
        assert manifest.verification_mode == "web"
        assert manifest.repo == "KBUtilLib" and manifest.machine == "h100"
        assert len(manifest.prompt_sha256) == 64


# ── collection ───────────────────────────────────────────────────────────


@pytest.fixture
def two_batches(tmp_path):
    """Two submitted single-gene batches, ready to be collected."""
    exe = fake_maestro(tmp_path)
    utils = make_utils(tmp_path)
    handles = submit(
        utils,
        exe=exe.path,
        count=2,
        batch_size=1,
        papers_by_gene={"g1": annotation_for("g1", "PMID:15210978", identity=0.9)},
    )
    return handles


class TestCollect:
    """Outputs are read from the branch with git show, then verified."""

    def collect(self, tmp_path, handles, outputs, *, known=("PMID:15210978",)):
        """Collect *handles* against a fake git and a fake Europe PMC."""
        runner = FakeGit(outputs)
        europepmc = FakeEuropePMC(known=known)
        utils = make_utils(tmp_path, runner=runner, europepmc=europepmc)
        results = utils.collect_gene_facts_batches(
            handles, repo_path=str(tmp_path / "repo")
        )
        return results, runner, europepmc, utils

    def test_one_valid_and_one_missing_output(self, tmp_path, two_batches):
        good, missing = two_batches
        results, runner, _, _ = self.collect(
            tmp_path,
            two_batches,
            {spec(good): document(good, {"g1": {"facts": [fact()], "notes": "n"}})},
        )

        assert set(results) == {"g1", "g2"}
        assert results["g1"].status == "ok"
        assert [item.statement for item in results["g1"].facts] == [
            "DnaA binds oriC and initiates replication."
        ]
        assert results["g1"].notes == "n"
        # The missing one: pending_or_failed, per gene of that batch.
        assert results["g2"].status == "error"
        assert results["g2"].error.startswith("pending_or_failed: ")
        assert missing.task_id in results["g2"].error
        assert results["g2"].facts == []
        # Both branches were asked for, and only with git show.
        assert runner.specs() == [spec(good), spec(missing)]

    def test_the_git_argv_reads_the_task_branch_and_nothing_else(
        self, tmp_path, two_batches
    ):
        good, _ = two_batches
        _, runner, _, _ = self.collect(
            tmp_path, [good], {spec(good): document(good, {"g1": {"facts": []}})}
        )
        assert runner.calls[0] == [
            "git",
            "-C",
            str(tmp_path / "repo"),
            "show",
            f"{good.branch}:{good.output_path}",
        ]

    def test_the_result_is_attributed_to_maestro(self, tmp_path, two_batches):
        good, _ = two_batches
        results, _, _, _ = self.collect(
            tmp_path, [good], {spec(good): document(good, {"g1": {"facts": [fact()]}})}
        )
        answer = results["g1"]
        assert answer.api_type == MAESTRO_API_TYPE == "maestro"
        assert answer.model == "codex"
        assert answer.mode == "web"
        assert answer.from_cache is False
        assert answer.papers_used.selected == ["PMID:15210978"]

    def test_an_unresolvable_citation_rejects_the_fact(self, tmp_path, two_batches):
        good, _ = two_batches
        results, _, europepmc, _ = self.collect(
            tmp_path,
            [good],
            {
                spec(good): document(
                    good,
                    {
                        "g1": {
                            "facts": [
                                fact("PMID:15210978"),
                                fact(
                                    "PMID:99999999",
                                    statement="An invented claim.",
                                ),
                            ]
                        }
                    },
                )
            },
        )
        answer = results["g1"]
        assert [item.statement for item in answer.facts] == [
            "DnaA binds oriC and initiates replication."
        ]
        assert [item.reason for item in answer.rejected_facts] == [
            "unresolved_citations"
        ]
        assert answer.rejected_facts[0].statement == "An invented claim."
        # EVERY cited id went through resolve -- including the layer-1 one we
        # put in the prompt, which this layer does not treat as trusted.
        assert set(europepmc.resolve_calls) == {"PMID:15210978", "PMID:99999999"}

    def test_a_fact_type_outside_FACT_TYPES_is_rejected_on_its_own(
        self, tmp_path, two_batches
    ):
        good, _ = two_batches
        results, _, _, _ = self.collect(
            tmp_path,
            [good],
            {
                spec(good): document(
                    good,
                    {
                        "g1": {
                            "facts": [
                                fact(type="speculation", statement="Maybe."),
                                fact(),
                            ]
                        }
                    },
                )
            },
        )
        answer = results["g1"]
        assert len(answer.facts) == 1
        assert [item.reason for item in answer.rejected_facts] == ["bad_type"]

    def test_the_result_is_cached_under_a_maestro_key(self, tmp_path, two_batches):
        good, _ = two_batches
        results, _, _, utils = self.collect(
            tmp_path, [good], {spec(good): document(good, {"g1": {"facts": [fact()]}})}
        )
        answer = results["g1"]
        assert answer.cache_key
        stored = utils.cache.get(GENE_FACTS_TABLE, answer.cache_key)
        assert stored is not None
        assert stored["api_type"] == "maestro"
        assert stored["mode"] == "web"
        assert stored["gene_id"] == "g1"
        assert "from_cache" not in stored
        assert GeneFacts.model_validate(stored).facts[0].citations[0].paper_id == (
            "PMID:15210978"
        )

    def test_an_error_result_is_never_cached(self, tmp_path, two_batches):
        good, missing = two_batches
        results, _, _, utils = self.collect(
            tmp_path, two_batches, {spec(good): document(good, {"g1": {"facts": []}})}
        )
        assert results["g2"].status == "error"
        assert results["g2"].cache_key is None
        with sqlite3.connect(str(utils.cache.path)) as connection:
            rows = connection.execute(
                f"SELECT key FROM {GENE_FACTS_TABLE}"  # noqa: S608 - fixed name
            ).fetchall()
        assert len(rows) == 1  # g1 only

    def test_the_cache_key_separates_genes_machines_and_outputs(
        self, tmp_path, two_batches
    ):
        good, _ = two_batches
        text = document(good, {"g1": {"facts": [fact()]}})
        first, _, _, _ = self.collect(tmp_path, [good], {spec(good): text})

        # Same handle, a different committed document -> a different key.
        other = document(good, {"g1": {"facts": [fact(confidence="low")]}})
        second, _, _, _ = self.collect(tmp_path, [good], {spec(good): other})
        assert first["g1"].cache_key != second["g1"].cache_key

        # Same document, a different machine -> a different key, because the
        # endpoint is part of it.
        moved = good.model_copy(deep=True)
        moved.machine = "primary-laptop"
        third, _, _, _ = self.collect(tmp_path, [moved], {spec(moved): text})
        assert third["g1"].cache_key != first["g1"].cache_key

    def test_an_absent_assigned_gene_is_reported_on_its_own(self, tmp_path):
        exe = fake_maestro(tmp_path)
        handle = submit(make_utils(tmp_path), exe=exe.path, count=3, batch_size=3)[0]
        results, _, _, _ = self.collect(
            tmp_path,
            [handle],
            {
                spec(handle): document(
                    handle,
                    {
                        "g1": {"facts": [fact()]},
                        "g3": {"facts": [], "status": "no_evidence"},
                    },
                )
            },
        )
        assert set(results) == {"g1", "g2", "g3"}
        assert results["g1"].status == "ok"
        assert results["g3"].status == "no_evidence"
        assert results["g2"].status == "error"
        assert results["g2"].error.startswith("missing_from_output: ")
        assert "g2" in results["g2"].error

    def test_a_foreign_gene_id_is_rejected(self, tmp_path, two_batches):
        good, _ = two_batches
        results, _, _, _ = self.collect(
            tmp_path,
            [good],
            {
                spec(good): document(
                    good,
                    {
                        "g1": {"facts": [fact()]},
                        "g2": {"facts": [fact(statement="Not this batch's gene.")]},
                        "ACIAD9999": {"facts": [fact()]},
                    },
                )
            },
        )
        # Only the assigned gene comes back.  g2 belongs to the other batch
        # and must not be answered from this batch's provenance.
        assert set(results) == {"g1"}

    def test_the_same_gene_twice_in_one_document_is_refused(
        self, tmp_path, two_batches
    ):
        good, _ = two_batches
        text = (
            '{"schema_version": "1", "batch_id": "%s", "results": '
            '{"g1": {"facts": []}, "g1": {"facts": [%s]}}}'
            % (good.batch_id, json.dumps(fact()))
        )
        results, _, _, _ = self.collect(tmp_path, [good], {spec(good): text})
        assert results["g1"].status == "error"
        assert results["g1"].error.startswith("duplicate_in_output: ")

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "not json",
            "{}",
            '{"schema_version": "1"}',
            '{"results": []}',
            "[1, 2, 3]",
        ],
    )
    def test_an_unparseable_or_shapeless_document_is_pending_or_failed(
        self, tmp_path, two_batches, text
    ):
        good, _ = two_batches
        results, _, _, _ = self.collect(tmp_path, [good], {spec(good): text})
        assert results["g1"].status == "error"
        assert results["g1"].error.startswith("pending_or_failed: ")

    def test_another_batchs_document_is_not_read_as_this_ones(
        self, tmp_path, two_batches
    ):
        good, other = two_batches
        results, _, _, _ = self.collect(
            tmp_path,
            [good],
            {spec(good): document(other, {"g1": {"facts": [fact()]}})},
        )
        assert results["g1"].status == "error"
        assert results["g1"].error.startswith("batch_mismatch: ")

    def test_a_worker_reported_error_is_carried_through(self, tmp_path, two_batches):
        good, _ = two_batches
        results, _, _, _ = self.collect(
            tmp_path,
            [good],
            {
                spec(good): document(
                    good,
                    {
                        "g1": {
                            "status": "error",
                            "error": "environment mismatch: kbutillib at deadbee",
                            "facts": [],
                        }
                    },
                )
            },
        )
        answer = results["g1"]
        assert answer.status == "error"
        assert answer.error == (
            "worker_error: environment mismatch: kbutillib at deadbee"
        )

    @pytest.mark.parametrize(
        "entry",
        [
            {"notes": "no facts key at all"},
            {"facts": {"not": "an array"}},
            {"facts": [{"type": "function"}]},
            {"facts": [{"type": 1, "statement": "s", "citations": [], "confidence": "high"}]},
            "a string, not an object",
        ],
    )
    def test_an_entry_that_does_not_match_the_schema_is_invalid(
        self, tmp_path, two_batches, entry
    ):
        good, _ = two_batches
        results, _, _, _ = self.collect(
            tmp_path, [good], {spec(good): document(good, {"g1": entry})}
        )
        assert results["g1"].status == "error"
        assert results["g1"].error.startswith("invalid_result: ")

    def test_handles_may_be_plain_mappings(self, tmp_path, two_batches):
        good, _ = two_batches
        payload = json.loads(good.model_dump_json())
        results, _, _, _ = self.collect(
            tmp_path, [payload], {spec(good): document(good, {"g1": {"facts": [fact()]}})}
        )
        assert results["g1"].status == "ok"

    def test_two_handles_claiming_one_gene_is_refused(self, tmp_path, two_batches):
        good, other = two_batches
        clash = other.model_copy(deep=True)
        clash.gene_ids = ["g1"]
        with pytest.raises(ValueError, match="assigned to two handles"):
            self.collect(tmp_path, [good, clash], {})

    def test_no_repo_path_is_refused(self, tmp_path, two_batches):
        utils = make_utils(tmp_path, runner=FakeGit({}))
        with pytest.raises(ValueError, match="repo_path is required"):
            utils.collect_gene_facts_batches(two_batches, repo_path="")

    def test_maestro_status_is_never_consulted(self, tmp_path, two_batches):
        good, missing = two_batches
        _, runner, _, _ = self.collect(
            tmp_path, two_batches, {spec(good): document(good, {"g1": {"facts": []}})}
        )
        # FakeGit raises on anything that is not git; this asserts the shape
        # of what WAS run, so a future 'maestro status' call fails loudly.
        assert all(Path(call[0]).name == "git" for call in runner.calls)
        assert all(call[3] == "show" for call in runner.calls)

    def test_the_module_level_function_collects_the_same_way(
        self, tmp_path, two_batches
    ):
        good, _ = two_batches
        results = collect_gene_facts_batches(
            [good],
            repo_path=str(tmp_path / "repo"),
            runner=FakeGit({spec(good): document(good, {"g1": {"facts": [fact()]}})}),
            europepmc=FakeEuropePMC(known=["PMID:15210978"]),
            cache=LiteratureCache(tmp_path / "gene_literature.sqlite"),
            config={},
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )
        assert results["g1"].status == "ok"
        assert results["g1"].api_type == "maestro"


class TestImplWrapper:
    """The composition wrapper delegates, like its siblings in this package."""

    def test_it_delegates_and_exposes_env(self, tmp_path, shared_env):
        impl = MaestroBatchUtilsImpl(
            shared_env, cache=LiteratureCache(tmp_path / "c.sqlite")
        )
        assert impl.env is shared_env
        assert callable(impl.submit_gene_facts_batches)
        assert callable(impl.collect_gene_facts_batches)
        assert impl.cache.path == tmp_path / "c.sqlite"


# ── the no-injection check ───────────────────────────────────────────────


class TestNoInjection:
    """Real subprocess, real fake SCRIPT, real git repo, real cache file.

    Nothing is injected into :class:`MaestroBatchUtils` here: no runner, no
    cache, no Europe PMC client.  It resolves its cache path from config
    under a relocated HOME, builds its own ``EuropePMCClient``, executes the
    fake maestro SCRIPT through the real :func:`subprocess.run`, and reads the
    committed output with the real ``git show`` out of a real temporary
    repository on a real branch.  The only interception is at the HTTP
    transport (``requests.adapters.HTTPAdapter.send``) -- a socket, not a
    seam of the code under test.

    What it proves that the injected tests cannot: that the argv this module
    builds really runs, that ``git show <branch>:<path>`` really finds a
    worker's commit, and that the row that ends up on disk is the row we
    believe we wrote.
    """

    BRANCH = "maestro/researcher/t6-no-injection"

    def wire_home(self, tmp_path, monkeypatch) -> Path:
        """A relocated HOME whose config names the cache path."""
        home = tmp_path / "home"
        (home / ".kbutillib").mkdir(parents=True)
        cache_path = home / ".kbutillib" / "gene_literature.sqlite"
        config_path = home / ".kbutillib" / "config.yaml"
        config_path.write_text(
            "gene_literature:\n"
            f"  cache_path: {cache_path}\n"
            # A high rate keeps the real Europe PMC throttle's real sleep
            # negligible; the throttle itself is not stubbed out.
            "  europepmc_rps: 50\n"
        )
        monkeypatch.setenv("HOME", str(home))
        # SharedEnvUtils binds the user-config location at import time, so a
        # relocated HOME has to be pointed at explicitly.
        monkeypatch.setattr(shared_env_utils, "DEFAULT_CONFIG_FILE", config_path)
        return cache_path

    def intercept_http(self, monkeypatch) -> List[Any]:
        """Replay the recorded Europe PMC batch at the transport layer."""
        payload = load_fixture("get_batch.json")
        sent: List[Any] = []

        def fake_send(adapter, request, **kwargs):
            sent.append(request)
            response = requests.Response()
            response.status_code = 200
            response.encoding = "utf-8"
            response.headers["Content-Type"] = "application/json"
            response._content = json.dumps(payload).encode("utf-8")
            response.url = request.url
            response.request = request
            return response

        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fake_send)
        return sent

    def git(self, repo: Path, *args: str) -> None:
        """Run a real git command in *repo*, with identity forced."""
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=Test Worker",
                "-c",
                "user.email=worker@example.invalid",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def make_repo(self, tmp_path) -> Path:
        """A real git repository with one commit on ``main``."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(
            ["git", "init", "--initial-branch=main", str(repo)],
            check=True,
            capture_output=True,
            text=True,
        )
        (repo / "README.md").write_text("# a repository a worker commits into\n")
        self.git(repo, "add", "README.md")
        self.git(repo, "commit", "-m", "initial")
        return repo

    def worker_commits(self, repo: Path, handle: BatchHandle, text: str) -> None:
        """Do what the worker is told to do: write, add, commit, on a branch."""
        self.git(repo, "checkout", "-b", handle.branch)
        target = repo / handle.output_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        self.git(repo, "add", handle.output_path)
        self.git(repo, "commit", "-m", f"facts(batch {handle.batch_id})")
        # Back to main, so the file is NOT in the working tree: collection
        # has to read it out of the branch or not at all.
        self.git(repo, "checkout", "main")
        assert not target.exists()

    def test_submit_then_collect_end_to_end_with_no_seams(
        self, tmp_path, monkeypatch
    ):
        cache_path = self.wire_home(tmp_path, monkeypatch)
        repo = self.make_repo(tmp_path)
        exe = fake_maestro(
            tmp_path, stdout_expr=fixed_stdout("task-no-injection", self.BRANCH)
        )

        # No seams: no runner, no cache, no europepmc, no config dict.
        # token_file/kbase_token_file are not seams of this class; they are
        # silenced only to keep the real HOME's token files out of the test.
        utils = MaestroBatchUtils(token_file=None, kbase_token_file=None)
        assert utils.cache.path == cache_path

        handles = utils.submit_gene_facts_batches(
            [{"id": "ACIAD0380", "aliases": ["dnaA"], "sequence": "MKTAYIAKQRQ"}],
            ORGANISM,
            {"ACIAD0380": ["PMID:15210978"]},
            repo="KBUtilLib",
            worker_env=dict(WORKER_ENV),
            maestro_exe=exe.path,  # a SCRIPT in tmp, run by real subprocess
            out_dir="agent-io/gene-facts",
            timeout=900,
        )
        handle = handles[0]
        assert handle.task_id == "task-no-injection"
        assert handle.branch == self.BRANCH
        # The real process really ran, and really got the argv we built.
        argv = exe.calls()[0]
        assert argv[0] == "submit" and "--role" in argv and "researcher" in argv
        assert argv[argv.index("--expected-output") + 1] == handle.output_path
        assert not [flag for flag in argv if flag.startswith("--auto")]

        # The worker's half: one good citation (in the recorded fixture) and
        # one invented id (not in it), so verification has something to do.
        self.worker_commits(
            repo,
            handle,
            document(
                handle,
                {
                    "ACIAD0380": {
                        "status": "ok",
                        "facts": [
                            fact("15210978"),
                            fact(
                                "PMID:99999999",
                                statement="An id that does not resolve.",
                            ),
                        ],
                        "notes": "full text of one paper was paywalled",
                    }
                },
            ),
        )

        sent = self.intercept_http(monkeypatch)
        collector = MaestroBatchUtils(token_file=None, kbase_token_file=None)
        assert isinstance(collector.europepmc, EuropePMCClient)
        results = collector.collect_gene_facts_batches(
            handles, repo_path=str(repo)
        )

        answer = results["ACIAD0380"]
        assert answer.status == "ok"
        assert answer.api_type == "maestro"
        assert answer.model == "codex"
        assert answer.mode == "web"
        assert answer.notes == "full text of one paper was paywalled"
        # The resolvable citation survived, normalised to its canonical id;
        # the invented one took its fact down with it.
        assert [item.citations[0].paper_id for item in answer.facts] == [
            "PMID:15210978"
        ]
        assert [item.reason for item in answer.rejected_facts] == [
            "unresolved_citations"
        ]
        # Real HTTP was attempted (and replayed) for the resolve calls.
        assert sent, "no Europe PMC request was made, so nothing was verified"

        # The row on disk, read with sqlite3 rather than through the cache.
        assert cache_path.exists()
        with sqlite3.connect(str(cache_path)) as connection:
            rows = connection.execute(
                f"SELECT key, value FROM {GENE_FACTS_TABLE}"  # noqa: S608
            ).fetchall()
        assert len(rows) == 1
        key, value = rows[0]
        assert key == answer.cache_key
        stored = json.loads(value)
        assert stored["api_type"] == "maestro"
        assert stored["gene_id"] == "ACIAD0380"
        assert stored["mode"] == "web"
        assert "from_cache" not in stored
        assert GeneFacts.model_validate(stored).facts[0].citations[0].paper_id == (
            "PMID:15210978"
        )

    def test_a_task_that_committed_nothing_reads_as_pending_or_failed(
        self, tmp_path, monkeypatch
    ):
        self.wire_home(tmp_path, monkeypatch)
        repo = self.make_repo(tmp_path)
        exe = fake_maestro(
            tmp_path, stdout_expr=fixed_stdout("task-silent", self.BRANCH)
        )
        utils = MaestroBatchUtils(token_file=None, kbase_token_file=None)
        handles = utils.submit_gene_facts_batches(
            [{"id": "ACIAD0380"}],
            ORGANISM,
            repo="KBUtilLib",
            worker_env=dict(WORKER_ENV),
            maestro_exe=exe.path,
        )
        # The branch does not even exist: a task still queued looks exactly
        # like one that failed, which is why there is one status for both.
        results = utils.collect_gene_facts_batches(handles, repo_path=str(repo))
        answer = results["ACIAD0380"]
        assert answer.status == "error"
        assert answer.error.startswith("pending_or_failed: ")
        assert "task-silent" in answer.error
