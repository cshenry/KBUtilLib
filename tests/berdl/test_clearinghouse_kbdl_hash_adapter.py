"""The ``kbdl_hash`` adapter, its job lifecycle, and the preset it ships.

NOTHING HERE REACHES THE NETWORK. Every KBDL call goes through
:class:`FakeKbdlClient`, which serves object listings and job records out of
dicts and records every submission, poll and result read -- which is how
"one job per archive", "a recorded job is READ, not resubmitted" and "a
mismatched probe refuses before any OTHER job is submitted" are asserted at
all. The real client's own ``submit_hash_genomes`` is exercised separately
against a recording ``_request``, so the posted job type and params shape are
checked against the method that actually builds them.

The job output shape is the contract (KBDLJobRunningPrototype
``src/kbdl_service/schemas/hash_genomes.py`` and
``job_types/hash_genomes.py``): ``{"genomes": {<key>: {...}}, "errors":
{<key>: {"code", "message"}}}``, and the fixtures below are written in it
rather than in a convenient reduction of it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from kbutillib.domains.external import kbdl_service_utils as ksu
from kbutillib.domains.identity import standardizers
from kbutillib.domains.kbase.berdl import clearinghouse_backfill as cb
from kbutillib.domains.kbase.berdl import clearinghouse_manifest as cm
from kbutillib.domains.kbase.berdl import clearinghouse_schema as schema
from kbutillib.domains.kbase.berdl import clearinghouse_shard as cs

PRESET = "kbdl-genome-archives"

#: Two archive object ids, deliberately ordered so that LEXICOGRAPHIC order
#: and the order the fake lists them in disagree -- KBDL's ObjectStore.list
#: orders by added_at with no tie-break, so a test whose two orders agreed
#: could not tell a sorted unit list from an unsorted one.
ARCHIVE_A = "a" * 64
ARCHIVE_B = "b" * 64
ARCHIVE_C = "c" * 64

VERSION = standardizers.STANDARDIZER_VERSION


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _entry(
    name: str,
    *,
    archive: str = ARCHIVE_A,
    version: str = VERSION,
    n_contigs: int = 2,
    total_length: int = 26,
) -> dict:
    """One ``genomes`` entry, in the job's own shape."""
    return {
        "entity_hash": _digest(f"{archive}:{name}"),
        "standardizer_version": version,
        "archive_object_id": archive,
        "entry_name": name,
        "n_contigs": n_contigs,
        "total_length": total_length,
        "longest_contig": 16,
        "shortest_contig": 10,
    }


def _result(archive: str = ARCHIVE_A, *, entries=("g1.fna", "g2.fna"), **kw) -> dict:
    return {
        "genomes": {name: _entry(name, archive=archive, **kw) for name in entries},
        "errors": {},
    }


def _archive_object(object_id: str, *, object_type: str = "GenomeArchive") -> dict:
    return {
        "object_id": object_id,
        "object_type": object_type,
        "name": f"archive-{object_id[:4]}",
        "added_at": "2026-10-01T00:00:00Z",
    }


class FakeKbdlClient:
    """The KBDL surface the ``kbdl_hash`` adapter uses, and nothing else.

    ``outcomes`` queues per-submission job records, consumed in submission
    order: each entry is merged over the default (a ``completed`` job whose
    result hashes two members of the submitted archive). That is how a failed
    job, a reclaimed result and a wrong standardizer version are arranged
    without the adapter being told which case it is in.
    """

    def __init__(self, *, objects=None, outcomes=None):
        self.objects: dict[str, dict] = {
            record["object_id"]: record for record in (objects or [])
        }
        self.outcomes = list(outcomes or [])
        self.jobs: dict[str, dict] = {}
        self.submitted: list[tuple[str, list[str]]] = []
        self.checked: list[str] = []
        self.polled: list[str] = []
        self.result_reads: list[str] = []
        self.list_calls = 0
        self.metadata_calls: list[str] = []
        #: Called with the job id on every ``check_job``; the hook a test uses
        #: to look at durable state at the instant the first poll happens.
        self.on_check = None
        self._counter = 0

    # -- objects ---------------------------------------------------------
    def list_objects(self):
        self.list_calls += 1
        return list(self.objects.values())

    def get_object_metadata(self, object_id):
        self.metadata_calls.append(object_id)
        if object_id not in self.objects:
            raise ksu.KBDLNotFoundError(f"no object {object_id}")
        return dict(self.objects[object_id])

    # -- jobs ------------------------------------------------------------
    def submit_hash_genomes(self, archive_ids):
        ids = list(archive_ids)
        if not ids:
            raise ValueError("KBDLHashGenomes takes a non-empty archives list")
        self._counter += 1
        job_id = f"job-{self._counter}"
        record = {
            "job_id": job_id,
            "job_type": ksu.JOB_TYPE_HASH_GENOMES,
            "state": "completed",
            "completed_at": "2026-10-08T12:00:00Z",
            "result": _result(ids[0]),
        }
        if self.outcomes:
            record.update(self.outcomes.pop(0))
        self.jobs[job_id] = record
        self.submitted.append((job_id, ids))
        return job_id

    def _status(self, job_id):
        record = self.jobs[job_id]
        return {
            key: value
            for key, value in record.items()
            if key not in ("result", "job_record_gone", "result_reclaimed",
                           "terminal_state")
        }

    def check_job(self, job_id):
        self.checked.append(job_id)
        if self.on_check is not None:
            self.on_check(job_id)
        record = self.jobs.get(job_id)
        if record is None or record.get("job_record_gone"):
            raise ksu.KBDLNotFoundError(f"no job {job_id}")
        return self._status(job_id)

    def poll_until_terminal(self, job_id, interval=None, timeout=None, **_kw):
        self.polled.append(job_id)
        record = self.jobs.get(job_id)
        if record is None:
            raise ksu.KBDLNotFoundError(f"no job {job_id}")
        record["state"] = record.get("terminal_state", "completed")
        return self._status(job_id)

    def get_job_result(self, job_id):
        self.result_reads.append(job_id)
        record = self.jobs.get(job_id)
        if record is None:
            raise ksu.KBDLNotFoundError(f"no job {job_id}")
        if record.get("result_reclaimed"):
            raise ksu.KBDLNotFoundError(
                f"result for {job_id} was reclaimed (30-day retention)"
            )
        return record["result"]


def _source(name: str = "kbdl-archives", **locator) -> cm.Source:
    """A ``[[source]]`` block for the adapter, in the preset's own shape."""
    return cm.Source(
        name=name,
        adapter="kbdl_hash",
        entity_type="genome",
        kinds=("entity", "content"),
        locator=dict(locator),
        hash_spec={"precomputed": "entity_hash"},
        content={
            "fasta_reference": "@concat(kbdl:archive:,archive_object_id,:,entry_name)",
            "n_contigs": "n_contigs",
            "total_length": "total_length",
        },
        result={},
        observed_at_column="observed_at",
    )


def _context(tmp_path: Path, unit: str, **kw) -> cb.BackfillRunContext:
    """A REAL run context, so get_state/put_state are the shipped ones."""
    return cb.BackfillRunContext(
        run_id="run-1",
        unit=unit,
        unit_kind=cb.UNIT_KIND_ADAPTER,
        out_dir=tmp_path / "units" / unit,
        state_dir=tmp_path / "state",
        tenant="kbase",
        dataset="scratch",
        **kw,
    )


def _adapter(
    client, *, unit=None, context=None, name="kbdl-archives", **locator
) -> cs.KbdlHashSourceAdapter:
    adapter = cs.KbdlHashSourceAdapter(_source(name, **locator), client=client)
    if context is not None:
        adapter.bind_run_context(context)
    if unit is not None:
        adapter.restrict_to_unit(unit)
    return adapter


@pytest.fixture(autouse=True)
def state_root(tmp_path, monkeypatch):
    """Keep any state this module writes inside tmp_path."""
    root = tmp_path / "clearinghouse-state"
    monkeypatch.setenv("KBU_CLEARINGHOUSE_STATE_DIR", str(root))
    return root


# ==========================================================================
# The client: submit_hash_genomes
# ==========================================================================


class _RecordingClient(ksu.KBDLServiceUtils):
    """A real client with ``_request`` replaced, so the method under test runs."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.base_url = "http://example.invalid"
        self.timeout = 1.0
        self.session = None

    def _request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))

        class _Response:
            @staticmethod
            def json():
                return {"job_id": "job-abc"}

        return _Response()


def test_submit_hash_genomes_posts_the_job_type_and_the_archives_list():
    client = _RecordingClient()

    job_id = client.submit_hash_genomes([ARCHIVE_A, ARCHIVE_B])

    assert job_id == "job-abc"
    method, path, kwargs = client.calls[0]
    assert (method, path) == ("POST", "/jobs")
    assert kwargs["json"] == {
        "schema_version": ksu.SCHEMA_VERSION,
        "job_type": "KBDLHashGenomes",
        "params": {
            "archives": [{"object_id": ARCHIVE_A}, {"object_id": ARCHIVE_B}],
        },
    }


def test_submit_hash_genomes_refuses_an_empty_list_before_any_request():
    """At least one archive is required, and being told so by the service
    costs a round trip that hides the caller's bug behind a transport error."""
    client = _RecordingClient()

    with pytest.raises(ValueError) as excinfo:
        client.submit_hash_genomes([])

    assert "at least one archive" in str(excinfo.value)
    assert client.calls == []


def test_submit_hash_genomes_refuses_a_non_string_id_before_any_request():
    client = _RecordingClient()
    with pytest.raises(ValueError):
        client.submit_hash_genomes([ARCHIVE_A, None])
    assert client.calls == []


# ==========================================================================
# The @concat derivation
# ==========================================================================


def test_concat_builds_the_archive_member_pointer():
    derivation = cm.parse_derivation(
        "@concat(kbdl:archive:,archive_object_id,:,entry_name)", where="t"
    )
    row = {"archive_object_id": ARCHIVE_A, "entry_name": "g1.fna"}
    assert cs._derive(derivation, row) == f"kbdl:archive:{ARCHIVE_A}:g1.fna"


@pytest.mark.parametrize(
    "row",
    [
        {"archive_object_id": None, "entry_name": "g1.fna"},
        {"archive_object_id": ARCHIVE_A, "entry_name": None},
        {},
    ],
)
def test_concat_is_null_preserving_across_every_named_column(row):
    """A pointer assembled from a missing half is a reference to nothing, and
    in the lake it is indistinguishable from a real one."""
    derivation = cm.parse_derivation(
        "@concat(kbdl:archive:,archive_object_id,:,entry_name)", where="t"
    )
    assert cs._derive(derivation, row) is None


def test_concat_allows_an_empty_literal_but_not_an_empty_column_name():
    derivation = cm.parse_derivation("@concat(,a,:,b)", where="t")
    assert cs._derive(derivation, {"a": "x", "b": "y"}) == "x:y"
    with pytest.raises(cm.ManifestError):
        cm.parse_derivation("@concat(p,a,:,)", where="t")


@pytest.mark.parametrize(
    "raw", ["@concat(a)", "@concat(a,b,c)", "@concat(a,b)", "@concat()"]
)
def test_concat_refuses_an_odd_or_too_short_argument_list(raw):
    """Two arguments IS @prefix, and two spellings of one rule is how a
    manifest grammar stops being readable."""
    with pytest.raises(cm.ManifestError) as excinfo:
        cm.parse_derivation(raw, where="t")
    assert "@concat" in str(excinfo.value)


def test_concat_is_in_the_closed_vocabulary():
    assert "concat" in cm.DERIVATIONS


# ==========================================================================
# units(): the archive selection
# ==========================================================================


def test_all_visible_filters_to_the_genome_archive_type():
    client = FakeKbdlClient(
        objects=[
            _archive_object(ARCHIVE_B),
            {"object_id": "m" * 64, "object_type": "Media", "name": "media"},
            _archive_object(ARCHIVE_A),
            {"object_id": "r" * 64, "object_type": "CanonicalGenome"},
        ]
    )

    adapter = _adapter(client, all_visible=True)

    assert adapter.units() == (ARCHIVE_A, ARCHIVE_B)


def test_units_are_sorted_lexicographically_not_left_in_listing_order():
    """KBDL's ObjectStore.list orders by added_at with NO tie-break, so the
    listing order is not a stable basis for a --limit window or a resume."""
    client = FakeKbdlClient(
        objects=[_archive_object(ARCHIVE_C), _archive_object(ARCHIVE_A),
                 _archive_object(ARCHIVE_B)]
    )
    assert [record["object_id"] for record in client.list_objects()] == [
        ARCHIVE_C, ARCHIVE_A, ARCHIVE_B
    ]

    assert _adapter(client, all_visible=True).units() == (
        ARCHIVE_A, ARCHIVE_B, ARCHIVE_C
    )


def test_units_is_resolved_once_per_adapter():
    client = FakeKbdlClient(objects=[_archive_object(ARCHIVE_A)])
    adapter = _adapter(client, all_visible=True)

    adapter.units()
    adapter.units()

    assert client.list_calls == 1


def test_an_explicit_archives_list_needs_no_network_at_all():
    """Which is what makes planning this preset possible off pod with an
    explicit list: no GET /objects, no job, no record read."""
    client = FakeKbdlClient()

    adapter = _adapter(client, archives=[ARCHIVE_B, ARCHIVE_A])

    assert adapter.units() == (ARCHIVE_A, ARCHIVE_B)
    assert client.list_calls == 0
    assert client.submitted == []


def test_an_archives_placeholder_string_is_split_on_commas():
    client = FakeKbdlClient()
    adapter = _adapter(client, archives=f"{ARCHIVE_B}, {ARCHIVE_A}")
    assert adapter.units() == (ARCHIVE_A, ARCHIVE_B)


@pytest.mark.parametrize(
    "locator",
    [
        {},
        {"archives": [ARCHIVE_A], "all_visible": True},
    ],
)
def test_neither_or_both_selection_keys_is_refused(locator):
    with pytest.raises(cs.ManifestSourceError):
        _adapter(FakeKbdlClient(), **locator).units()


def test_an_empty_visible_archive_set_is_refused_not_planned_as_zero_units():
    """A zero-unit run reports a clean success having loaded nothing, which is
    the one outcome indistinguishable from a working backfill."""
    client = FakeKbdlClient(objects=[{"object_id": "m" * 64, "object_type": "Media"}])

    with pytest.raises(cs.ManifestSourceError) as excinfo:
        _adapter(client, all_visible=True).units()

    assert "GenomeArchive" in str(excinfo.value)


def test_max_in_flight_defaults_to_one():
    assert cs.KBDL_HASH_DEFAULT_MAX_IN_FLIGHT == 1
    adapter = _adapter(FakeKbdlClient(), archives=[ARCHIVE_A])
    assert adapter._max_in_flight() == 1


@pytest.mark.parametrize("value", [0, -1, "many"])
def test_max_in_flight_refuses_a_non_positive_or_unparsable_value(value):
    adapter = _adapter(FakeKbdlClient(), archives=[ARCHIVE_A], max_in_flight=value)
    with pytest.raises(cs.ManifestSourceError):
        adapter._max_in_flight()


def test_a_malformed_max_in_flight_refuses_before_anything_is_spent(tmp_path):
    client = FakeKbdlClient(objects=[_archive_object(ARCHIVE_A)])
    adapter = _adapter(
        client,
        unit=ARCHIVE_A,
        context=_context(tmp_path, ARCHIVE_A),
        archives=[ARCHIVE_A],
        max_in_flight=0,
    )

    with pytest.raises(cs.ManifestSourceError):
        list(adapter.iter_records())

    assert client.submitted == []
    assert client.metadata_calls == []


# ==========================================================================
# The job lifecycle: one job per archive, read-not-resubmit, retention
# ==========================================================================


def _run_unit(client, tmp_path, archive, **context_kw) -> list[dict]:
    adapter = _adapter(
        client,
        unit=archive,
        context=_context(tmp_path, archive, **context_kw),
        all_visible=True,
    )
    return adapter, list(adapter.iter_records())


def test_one_job_is_submitted_per_archive_naming_only_that_archive(tmp_path):
    client = FakeKbdlClient(
        objects=[_archive_object(ARCHIVE_A), _archive_object(ARCHIVE_B)]
    )

    for archive in (ARCHIVE_A, ARCHIVE_B):
        adapter, rows = _run_unit(client, tmp_path, archive)
        assert len(rows) == 2
        assert adapter.exhausted() is True

    assert client.submitted == [("job-1", [ARCHIVE_A]), ("job-2", [ARCHIVE_B])]


def test_each_row_carries_the_entry_the_job_reported(tmp_path):
    client = FakeKbdlClient(objects=[_archive_object(ARCHIVE_A)])

    _adapter_obj, rows = _run_unit(client, tmp_path, ARCHIVE_A)

    by_name = {row["entry_name"]: row for row in rows}
    assert sorted(by_name) == ["g1.fna", "g2.fna"]
    first = by_name["g1.fna"]
    assert first["entity_hash"] == _digest(f"{ARCHIVE_A}:g1.fna")
    assert first["standardizer_version"] == VERSION
    assert first["archive_object_id"] == ARCHIVE_A
    assert first["n_contigs"] == 2
    assert first["total_length"] == 26
    assert first["genome_key"] == "g1.fna"
    # observed_at is the JOB's completion time -- the hash is a property of
    # the archive as the job read it, not of when the backfill loaded it.
    assert first["observed_at"] == "2026-10-08T12:00:00Z"


def test_the_job_id_is_recorded_in_run_state_BEFORE_the_first_poll(tmp_path):
    """The whole reason the run context has durable state: the job runs for
    hours, and an id that only ever existed in this process is an id a pod
    restart turns into a duplicate submission."""
    client = FakeKbdlClient(objects=[_archive_object(ARCHIVE_A)])
    context = _context(tmp_path, ARCHIVE_A)
    seen: list[list[str]] = []

    def _capture(job_id):
        state = context.get_state("kbdl-archives")
        seen.append(
            [
                attempt["job_id"]
                for attempt in state.get(cs.KBDL_HASH_STATE_ATTEMPTS) or []
            ]
        )

    client.on_check = _capture
    adapter = _adapter(client, unit=ARCHIVE_A, context=context, all_visible=True)

    list(adapter.iter_records())

    # The FIRST status check already saw the id on disk.
    assert seen[0] == ["job-1"]


def test_a_recorded_finished_job_is_read_and_not_resubmitted(tmp_path):
    client = FakeKbdlClient(objects=[_archive_object(ARCHIVE_A)])
    context = _context(tmp_path, ARCHIVE_A)
    # Pretend an earlier invocation submitted and the pod died before loading.
    client.jobs["job-earlier"] = {
        "job_id": "job-earlier",
        "state": "completed",
        "completed_at": "2026-10-07T01:02:03Z",
        "result": _result(ARCHIVE_A),
    }
    context.put_state(
        "kbdl-archives",
        {
            cs.KBDL_HASH_STATE_ARCHIVE: ARCHIVE_A,
            cs.KBDL_HASH_STATE_ATTEMPTS: [
                {"job_id": "job-earlier", "archive": ARCHIVE_A,
                 "reason": cs.KBDL_HASH_ATTEMPT_INITIAL,
                 "submitted_at": "2026-10-07T00:00:00Z"},
            ],
        },
    )
    adapter = _adapter(client, unit=ARCHIVE_A, context=context, all_visible=True)

    rows = list(adapter.iter_records())

    assert len(rows) == 2
    assert client.submitted == []
    assert client.result_reads == ["job-earlier"]
    # ...and nothing was re-checked for visibility either, because a recorded
    # result stands on its own.
    assert client.metadata_calls == []


def test_a_queued_job_is_reattached_to_and_polled_on_resume(tmp_path):
    client = FakeKbdlClient(objects=[_archive_object(ARCHIVE_A)])
    context = _context(tmp_path, ARCHIVE_A)
    client.jobs["job-queued"] = {
        "job_id": "job-queued",
        "state": "queued",
        "terminal_state": "completed",
        "completed_at": "2026-10-08T09:00:00Z",
        "result": _result(ARCHIVE_A),
    }
    context.put_state(
        "kbdl-archives",
        {cs.KBDL_HASH_STATE_ATTEMPTS: [{"job_id": "job-queued"}]},
    )
    adapter = _adapter(client, unit=ARCHIVE_A, context=context, all_visible=True)

    rows = list(adapter.iter_records())

    assert len(rows) == 2
    assert client.polled == ["job-queued"]
    assert client.submitted == []


def test_a_reclaimed_result_is_resubmitted_exactly_once(tmp_path):
    """KBDL deletes a job's result 30 days after completion, and a reclaimed
    one reads as not-found. That is the ONLY case that resubmits by itself."""
    client = FakeKbdlClient(objects=[_archive_object(ARCHIVE_A)])
    context = _context(tmp_path, ARCHIVE_A)
    client.jobs["job-old"] = {
        "job_id": "job-old",
        "state": "completed",
        "completed_at": "2026-09-01T00:00:00Z",
        "result_reclaimed": True,
        "result": _result(ARCHIVE_A),
    }
    context.put_state(
        "kbdl-archives",
        {cs.KBDL_HASH_STATE_ATTEMPTS: [{"job_id": "job-old"}]},
    )
    adapter = _adapter(client, unit=ARCHIVE_A, context=context, all_visible=True)

    rows = list(adapter.iter_records())

    assert len(rows) == 2
    assert client.submitted == [("job-1", [ARCHIVE_A])]
    state = context.get_state("kbdl-archives")
    assert [a["job_id"] for a in state[cs.KBDL_HASH_STATE_ATTEMPTS]] == [
        "job-old", "job-1"
    ]
    assert state[cs.KBDL_HASH_STATE_ATTEMPTS][-1]["reason"] == (
        cs.KBDL_HASH_ATTEMPT_RETENTION_LOSS
    )
    assert state[cs.KBDL_HASH_STATE_RETENTION_RESUBMITS] == 1


def test_a_second_retention_loss_refuses_instead_of_resubmitting_again(tmp_path):
    client = FakeKbdlClient(
        objects=[_archive_object(ARCHIVE_A)],
        outcomes=[{"result_reclaimed": True}],
    )
    context = _context(tmp_path, ARCHIVE_A)
    client.jobs["job-old"] = {
        "job_id": "job-old",
        "state": "completed",
        "result_reclaimed": True,
        "result": _result(ARCHIVE_A),
    }
    context.put_state(
        "kbdl-archives",
        {cs.KBDL_HASH_STATE_ATTEMPTS: [{"job_id": "job-old"}]},
    )
    adapter = _adapter(client, unit=ARCHIVE_A, context=context, all_visible=True)

    with pytest.raises(cs.KbdlHashRetentionLostError):
        list(adapter.iter_records())

    assert len(client.submitted) == 1
    assert adapter.incomplete_reasons()


def test_a_transport_error_does_not_resubmit(tmp_path):
    """Round 1: only a CONFIRMED retention loss resubmits. A transport or
    auth error says nothing about the job."""
    client = FakeKbdlClient(objects=[_archive_object(ARCHIVE_A)])
    context = _context(tmp_path, ARCHIVE_A)
    client.jobs["job-x"] = {"job_id": "job-x", "state": "completed",
                            "result": _result(ARCHIVE_A)}
    context.put_state(
        "kbdl-archives", {cs.KBDL_HASH_STATE_ATTEMPTS: [{"job_id": "job-x"}]}
    )

    def _boom(job_id):
        raise ksu.KBDLAuthenticationError("token expired")

    client.get_job_result = _boom
    adapter = _adapter(client, unit=ARCHIVE_A, context=context, all_visible=True)

    with pytest.raises(ksu.KBDLAuthenticationError):
        list(adapter.iter_records())

    assert client.submitted == []


# ==========================================================================
# Terminal job outcomes
# ==========================================================================


@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
def test_a_failed_or_cancelled_job_stops_the_command_with_the_cause(
    tmp_path, terminal
):
    client = FakeKbdlClient(
        objects=[_archive_object(ARCHIVE_A)],
        outcomes=[{"state": terminal, "failure": {"code": "oom"}}],
    )
    adapter = _adapter(
        client,
        unit=ARCHIVE_A,
        context=_context(tmp_path, ARCHIVE_A),
        all_visible=True,
    )

    with pytest.raises(cs.KbdlHashJobFailedError) as excinfo:
        list(adapter.iter_records())

    assert terminal in str(excinfo.value)
    assert "--retry-failed-archives" in str(excinfo.value)
    assert adapter.skipped() == {
        f"{cs.KBDL_HASH_SKIP_JOB_FAILED_PREFIX}{terminal}:oom": 1
    }
    assert adapter.incomplete_reasons()
    # and nothing was resubmitted on its own
    assert len(client.submitted) == 1


def test_retry_failed_archives_submits_a_recorded_new_attempt(tmp_path):
    client = FakeKbdlClient(
        objects=[_archive_object(ARCHIVE_A)],
        outcomes=[{"state": "failed", "failure": {"code": "oom"}}],
    )
    context = _context(tmp_path, ARCHIVE_A, retry_failed_archives=True)
    adapter = _adapter(client, unit=ARCHIVE_A, context=context, all_visible=True)

    rows = list(adapter.iter_records())

    assert len(rows) == 2
    attempts = context.get_state("kbdl-archives")[cs.KBDL_HASH_STATE_ATTEMPTS]
    assert [a["job_id"] for a in attempts] == ["job-1", "job-2"]
    assert [a["reason"] for a in attempts] == [
        cs.KBDL_HASH_ATTEMPT_INITIAL,
        cs.KBDL_HASH_ATTEMPT_OPERATOR_RETRY,
    ]
    # the failure is still counted -- a retried archive is not an archive that
    # never failed
    assert adapter.skipped()["hash-job-failed:failed:oom"] == 1


def test_a_succeeded_job_is_never_resubmitted_even_with_retry_failed_archives(
    tmp_path,
):
    client = FakeKbdlClient(objects=[_archive_object(ARCHIVE_A)])
    context = _context(tmp_path, ARCHIVE_A, retry_failed_archives=True)
    client.jobs["job-ok"] = {
        "job_id": "job-ok", "state": "completed", "result": _result(ARCHIVE_A)
    }
    context.put_state(
        "kbdl-archives", {cs.KBDL_HASH_STATE_ATTEMPTS: [{"job_id": "job-ok"}]}
    )
    adapter = _adapter(client, unit=ARCHIVE_A, context=context, all_visible=True)

    assert len(list(adapter.iter_records())) == 2
    assert client.submitted == []


# ==========================================================================
# Member errors and version mismatches
# ==========================================================================


def test_an_invalid_input_member_is_a_terminal_reported_exclusion(tmp_path):
    """It will read the same way forever, so the archive is still DONE."""
    result = _result(ARCHIVE_A, entries=("g1.fna",))
    result["errors"] = {
        "broken.fna": {"code": "invalid_input", "message": "not FASTA"}
    }
    client = FakeKbdlClient(
        objects=[_archive_object(ARCHIVE_A)], outcomes=[{"result": result}]
    )
    adapter = _adapter(
        client, unit=ARCHIVE_A, context=_context(tmp_path, ARCHIVE_A),
        all_visible=True,
    )

    rows = list(adapter.iter_records())

    assert len(rows) == 1
    assert adapter.skipped() == {"hash-error:invalid_input": 1}
    assert adapter.incomplete_reasons() == ()
    assert adapter.exhausted() is True


def test_an_internal_error_member_leaves_the_archive_incomplete_with_rows_loaded(
    tmp_path,
):
    """Round 2: the successful rows are still emitted (and ingested, and
    verified); the archive is incomplete and nothing is auto-resubmitted."""
    result = _result(ARCHIVE_A, entries=("g1.fna",))
    result["errors"] = {
        "truncated.fna": {"code": "internal_error", "message": "stream read failed"}
    }
    client = FakeKbdlClient(
        objects=[_archive_object(ARCHIVE_A)], outcomes=[{"result": result}]
    )
    adapter = _adapter(
        client, unit=ARCHIVE_A, context=_context(tmp_path, ARCHIVE_A),
        all_visible=True,
    )

    rows = list(adapter.iter_records())

    assert [row["entry_name"] for row in rows] == ["g1.fna"]
    assert adapter.skipped() == {"hash-error:internal_error": 1}
    reasons = adapter.incomplete_reasons()
    assert len(reasons) == 1 and "internal_error" in reasons[0]
    assert len(client.submitted) == 1  # no auto-resubmission


def test_an_unknown_error_code_is_treated_as_retryable_not_terminal(tmp_path):
    result = _result(ARCHIVE_A, entries=("g1.fna",))
    result["errors"] = {"odd.fna": {"code": "weather", "message": "?"}}
    client = FakeKbdlClient(
        objects=[_archive_object(ARCHIVE_A)], outcomes=[{"result": result}]
    )
    adapter = _adapter(
        client, unit=ARCHIVE_A, context=_context(tmp_path, ARCHIVE_A),
        all_visible=True,
    )

    list(adapter.iter_records())

    assert adapter.skipped() == {"hash-error:weather": 1}
    assert adapter.incomplete_reasons()


def test_a_per_entry_version_mismatch_is_skipped_counted_and_incomplete(tmp_path):
    """The probed version agrees (the first entry does), so this is a PER-ENTRY
    problem: the row is dropped rather than loaded under a rule this pod does
    not implement, and the archive is not finished."""
    result = {
        "genomes": {
            "ok.fna": _entry("ok.fna"),
            "stale.fna": _entry("stale.fna", version="0.9"),
        },
        "errors": {},
    }
    client = FakeKbdlClient(
        objects=[_archive_object(ARCHIVE_A)], outcomes=[{"result": result}]
    )
    adapter = _adapter(
        client, unit=ARCHIVE_A, context=_context(tmp_path, ARCHIVE_A),
        all_visible=True,
    )

    rows = list(adapter.iter_records())

    assert [row["entry_name"] for row in rows] == ["ok.fna"]
    assert adapter.skipped() == {cs.KBDL_HASH_SKIP_VERSION_MISMATCH: 1}
    assert adapter.incomplete_reasons()


def test_an_entry_missing_its_hash_or_name_is_counted_and_incomplete(tmp_path):
    broken = _entry("x.fna")
    broken["entity_hash"] = None
    client = FakeKbdlClient(
        objects=[_archive_object(ARCHIVE_A)],
        outcomes=[{"result": {"genomes": {"x.fna": broken}, "errors": {}}}],
    )
    adapter = _adapter(
        client, unit=ARCHIVE_A, context=_context(tmp_path, ARCHIVE_A),
        all_visible=True,
    )

    assert list(adapter.iter_records()) == []
    assert adapter.skipped() == {cs.KBDL_HASH_SKIP_ENTRY_UNUSABLE: 1}
    assert adapter.incomplete_reasons()


# ==========================================================================
# The standardizer-version probe
# ==========================================================================


def test_a_version_mismatch_refuses_before_any_other_job_is_submitted(tmp_path):
    """The probe IS the first archive's own job, and the driver runs units
    sequentially -- so a mismatched poplar costs ONE job, not one per
    archive."""
    client = FakeKbdlClient(
        objects=[_archive_object(ARCHIVE_A), _archive_object(ARCHIVE_B)],
        outcomes=[{"result": _result(ARCHIVE_A, version="9.9")}],
    )
    plan_units = (ARCHIVE_A, ARCHIVE_B)

    raised: list[Exception] = []
    for archive in plan_units:  # exactly what run_backfill's loop does
        adapter = _adapter(
            client, unit=archive, context=_context(tmp_path, archive),
            all_visible=True,
        )
        try:
            list(adapter.iter_records())
        except cs.KbdlHashVersionMismatchError as exc:
            raised.append(exc)
            break

    assert len(raised) == 1
    assert "9.9" in str(raised[0]) and VERSION in str(raised[0])
    assert client.submitted == [("job-1", [ARCHIVE_A])]


def test_the_probe_records_the_version_it_saw_when_it_agrees(tmp_path):
    client = FakeKbdlClient(objects=[_archive_object(ARCHIVE_A)])
    context = _context(tmp_path, ARCHIVE_A)
    adapter = _adapter(client, unit=ARCHIVE_A, context=context, all_visible=True)

    list(adapter.iter_records())

    assert context.get_state("kbdl-archives")[cs.KBDL_HASH_STATE_PROBE] == VERSION


def test_a_top_level_standardizer_version_is_what_the_probe_reads(tmp_path):
    """A version field on the RESULT is a statement about the service; an
    entry's is a statement about one member."""
    result = _result(ARCHIVE_A)
    result["standardizer_version"] = "7.7"
    client = FakeKbdlClient(
        objects=[_archive_object(ARCHIVE_A)], outcomes=[{"result": result}]
    )
    adapter = _adapter(
        client, unit=ARCHIVE_A, context=_context(tmp_path, ARCHIVE_A),
        all_visible=True,
    )

    with pytest.raises(cs.KbdlHashVersionMismatchError) as excinfo:
        list(adapter.iter_records())
    assert "7.7" in str(excinfo.value)


def test_an_errors_only_job_claims_no_version_and_still_counts_its_errors(
    tmp_path,
):
    client = FakeKbdlClient(
        objects=[_archive_object(ARCHIVE_A)],
        outcomes=[
            {
                "result": {
                    "genomes": {},
                    "errors": {"a.fna": {"code": "invalid_input", "message": "x"}},
                }
            }
        ],
    )
    context = _context(tmp_path, ARCHIVE_A)
    adapter = _adapter(client, unit=ARCHIVE_A, context=context, all_visible=True)

    assert list(adapter.iter_records()) == []
    assert context.get_state("kbdl-archives")[cs.KBDL_HASH_STATE_PROBE] is None
    assert adapter.skipped() == {"hash-error:invalid_input": 1}


# ==========================================================================
# A vanished archive
# ==========================================================================


def test_a_vanished_archive_leaves_the_unit_incomplete_and_stops_the_run(tmp_path):
    """The archive list was FROZEN when the run was created, so an archive
    that has since gone is a change to the corpus the run claims to cover."""
    client = FakeKbdlClient(objects=[_archive_object(ARCHIVE_A)])
    adapter = _adapter(
        client,
        unit=ARCHIVE_B,  # frozen into the run, no longer in KBDL
        context=_context(tmp_path, ARCHIVE_B),
        all_visible=True,
    )

    with pytest.raises(cs.KbdlHashArchiveUnavailableError) as excinfo:
        list(adapter.iter_records())

    assert ARCHIVE_B in str(excinfo.value)
    assert adapter.incomplete_reasons()
    assert client.submitted == []


def test_an_archive_that_is_no_longer_a_genome_archive_is_refused(tmp_path):
    client = FakeKbdlClient(
        objects=[_archive_object(ARCHIVE_A, object_type="Media")]
    )
    adapter = _adapter(
        client, unit=ARCHIVE_A, context=_context(tmp_path, ARCHIVE_A),
        archives=[ARCHIVE_A],
    )

    with pytest.raises(cs.KbdlHashArchiveUnavailableError):
        list(adapter.iter_records())
    assert client.submitted == []


def test_an_unrestricted_read_over_several_archives_is_refused():
    """One archive is one UNIT; this source is driven through `backfill run`."""
    client = FakeKbdlClient(
        objects=[_archive_object(ARCHIVE_A), _archive_object(ARCHIVE_B)]
    )
    adapter = _adapter(client, all_visible=True)

    with pytest.raises(cs.ManifestSourceError) as excinfo:
        list(adapter.iter_records())
    assert "one UNIT" in str(excinfo.value)


# ==========================================================================
# The frozen archive list, through the backfill driver
# ==========================================================================


@pytest.fixture
def wired(monkeypatch):
    """Register a ``kbdl_hash`` class bound to one fake client, as the
    driver's own ``get_adapter`` will build it."""
    client = FakeKbdlClient(
        objects=[_archive_object(ARCHIVE_B), _archive_object(ARCHIVE_A)]
    )

    class _WiredKbdlHash(cs.KbdlHashSourceAdapter):
        def __init__(self, source):
            super().__init__(source, client=client)

    monkeypatch.setitem(cs._ADAPTER_CLASSES, "kbdl_hash", _WiredKbdlHash)
    return client


def _preset_manifest() -> tuple[cm.Manifest, Path]:
    preset = next(p for p in cb.available_presets() if p.name == PRESET)
    return cm.load_manifest(preset.path), preset.path


def test_the_preset_plans_one_unit_per_archive_without_reading_a_record(wired):
    manifest, _path = _preset_manifest()

    plan = cb.plan_units(manifest)

    assert plan.kind == cb.UNIT_KIND_ADAPTER
    assert plan.units == (ARCHIVE_A, ARCHIVE_B)
    # ONE listing, NO job, NO result read: planning is cheap and submits
    # nothing.
    assert wired.list_calls == 1
    assert wired.submitted == []
    assert wired.result_reads == []


def test_backfill_list_describes_the_preset_with_no_client_at_all():
    """`backfill list` runs anywhere -- it must not reach KBDL to describe a
    preset that would."""
    preset = next(p for p in cb.available_presets() if p.name == PRESET)
    row = cb.describe_preset(preset)
    assert row["adapters"] == ["kbdl_hash"]
    assert row["tables"] == [
        schema.table_name("genome", "entity"),
        schema.table_name("genome", "content"),
    ]
    assert row["pulls_from"] == ["KBDL on poplar"]
    assert "error" not in row


def test_the_frozen_list_is_persisted_before_any_unit_runs(wired, tmp_path):
    manifest, _path = _preset_manifest()
    out = tmp_path / "out"

    plan = cb.freeze_adapter_units(
        out, run_id="run-1", plan=cb.plan_units(manifest)
    )

    assert plan.units == (ARCHIVE_A, ARCHIVE_B)
    state = cb.read_state(out)
    assert state[cb.FROZEN_UNITS_FIELD] == [ARCHIVE_A, ARCHIVE_B]
    assert state[cb.FROZEN_UNITS_AT_FIELD]
    assert wired.submitted == []


def test_a_changed_list_objects_result_on_resume_is_ignored(wired, tmp_path):
    manifest, _path = _preset_manifest()
    out = tmp_path / "out"
    cb.freeze_adapter_units(out, run_id="run-1", plan=cb.plan_units(manifest))

    # The store moves on: B is gone, C appears.
    del wired.objects[ARCHIVE_B]
    wired.objects[ARCHIVE_C] = _archive_object(ARCHIVE_C)

    resumed = cb.freeze_adapter_units(
        out, run_id="run-1", plan=cb.plan_units(manifest)
    )

    # The run still covers exactly what it froze -- C did not join it.
    assert resumed.units == (ARCHIVE_A, ARCHIVE_B)
    # ...and the disappearance is RECORDED rather than silently absorbed.
    assert resumed.vanished_units == (ARCHIVE_B,)
    assert ARCHIVE_B in cb.read_state(out)[cb.VANISHED_UNITS_FIELD]


def test_limit_is_reapplied_to_the_frozen_list_not_to_a_fresh_one(wired, tmp_path):
    manifest, _path = _preset_manifest()
    out = tmp_path / "out"

    plan = cb.freeze_adapter_units(
        out, run_id="run-1", plan=cb.plan_units(manifest, limit=1), limit=1
    )

    assert plan.units == (ARCHIVE_A,)
    assert plan.all_units == (ARCHIVE_A, ARCHIVE_B)
    assert plan.units_dropped_by_limit == 1
    # The FULL list is what was frozen, so dropping --limit on a later
    # invocation picks up B rather than re-freezing a one-element list.
    assert cb.read_state(out)[cb.FROZEN_UNITS_FIELD] == [ARCHIVE_A, ARCHIVE_B]
    assert cb.freeze_adapter_units(
        out, run_id="run-1", plan=cb.plan_units(manifest)
    ).units == (ARCHIVE_A, ARCHIVE_B)


def test_freeze_is_inert_for_a_single_unit_plan(tmp_path):
    manifest, _path = _preset_manifest()
    plan = cb.UnitPlan(kind=cb.UNIT_KIND_SINGLE, units=(cs.SINGLE_UNIT,))
    assert cb.freeze_adapter_units(
        tmp_path / "out", run_id="r", plan=plan
    ) is plan


# ==========================================================================
# The preset, sharded end to end
# ==========================================================================


def _rows(out: Path, table: str) -> list[dict]:
    rows: list[dict] = []
    for path in sorted((out / table).glob("*.parquet")):
        rows.extend(pq.read_table(str(path)).to_pylist())
    return rows


@pytest.fixture
def preset_shard(wired, tmp_path):
    """Shard ONE archive of the shipped preset over a fake hash result.

    The preset is loaded AS SHIPPED -- package data, unedited. Only the
    adapter CLASS is swapped, for the one thing a test cannot supply: a KBDL
    client.
    """
    manifest, path = _preset_manifest()
    out = tmp_path / "bronze"
    context = _context(tmp_path, ARCHIVE_A)
    cs.shard_manifest(
        manifest,
        out,
        manifest_path=path,
        run_context=context,
        adapter_unit=ARCHIVE_A,
    )
    return out


def test_the_preset_shards_into_genome_entity_and_genome_content(preset_shard):
    tables = sorted(p.name for p in preset_shard.iterdir() if p.is_dir())
    assert tables == sorted(
        [schema.table_name("genome", "entity"), schema.table_name("genome", "content")]
    )


def test_the_preset_writes_the_precomputed_entity_hash_on_both_tables(preset_shard):
    expected = {
        _digest(f"{ARCHIVE_A}:g1.fna"),
        _digest(f"{ARCHIVE_A}:g2.fna"),
    }
    assert {r["entity_hash"] for r in _rows(preset_shard, "genome_entity")} == expected
    assert {r["entity_hash"] for r in _rows(preset_shard, "genome_content")} == expected
    # standardizer_version is a RESERVED column the sharder fills with the
    # version this pod runs; the job's reported version gated the rows and is
    # not itself a mapped column.
    assert {r["standardizer_version"] for r in _rows(preset_shard, "genome_entity")} == {
        VERSION
    }


def test_the_preset_writes_the_exact_archive_member_pointer(preset_shard):
    content = {r["entity_hash"]: r for r in _rows(preset_shard, "genome_content")}
    first = content[_digest(f"{ARCHIVE_A}:g1.fna")]
    assert first["fasta_reference"] == f"kbdl:archive:{ARCHIVE_A}:g1.fna"
    assert first["n_contigs"] == 2
    assert first["total_length"] == 26
    # UNMAPPED, and null is the honest value: a hash job reads FASTA and
    # assigns no taxonomy, consults no assembly registry and makes no claim
    # about closure.
    assert first["taxon_id"] is None
    assert first["assembly_accession"] is None
    assert first["is_closed"] is None


def test_the_preset_submits_one_job_for_both_of_its_kinds(preset_shard, wired):
    """Two plans, two adapter instances, ONE job: the first persists its id in
    the run context and the second reads the recorded attempt."""
    assert len(wired.submitted) == 1


def test_the_shipped_preset_declares_what_the_task_requires():
    manifest, _path = _preset_manifest()
    source = manifest.sources[0]
    assert source.adapter == "kbdl_hash"
    assert source.locator["all_visible"] is True
    assert source.locator["max_in_flight"] == 1
    assert source.hash_spec == {"precomputed": "entity_hash"}
    assert source.content["fasta_reference"] == (
        "@concat(kbdl:archive:,archive_object_id,:,entry_name)"
    )
    assert set(source.kinds) == {"entity", "content"}
    assert manifest.backfill.pulls_from == ("KBDL on poplar",)
