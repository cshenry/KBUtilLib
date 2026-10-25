"""``council_disposition`` -- which council jobs are a RESULT, and the gate.

NOTHING IS STUBBED. Every test below seeds from the CHECKED-IN provenance
fixture (``tests/berdl/fixtures/AUTHORITATIVE-JOB-PROVENANCE.json``, a
verbatim copy of the file the agent that ran the jobs wrote) through the real
``seed_from_provenance``, writes the real ``job_disposition`` rows into a real
SQL sink built from ``council_schema``'s own DDL, and resolves them through
the real ``published_jobs`` / ``require_published``. The gate tests
additionally drive ``kbu council disposition`` end to end with Click's
``CliRunner``, because "fails non-zero" is a property of the process and not
of a caught exception.

No test hands the gate a prebuilt list of published ids and no test
monkeypatches the resolver. That is the whole no-injection bar for this
layer: the dangerous default being removed is "a job nobody declared looks
publishable", and a test that injects the answer cannot see it.

The one thing these tests do NOT establish: SQLite is not Iceberg. The sink
proves the SQL, the row shapes and the refusals; live acceptance of this
namespace on kbhub is operator-owned (KBUtilLib#91).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from kbutillib.cli import main
from kbutillib.domains.kbase.berdl.council_disposition import (
    JOB_DISPOSITIONS,
    PUBLISHED,
    DispositionError,
    DispositionLedger,
    JobNotPublishableError,
    ProvenanceFormatError,
    UnknownDispositionError,
    declare,
    disposition_rows,
    dispositions,
    published_jobs,
    read_provenance,
    require_published,
    resolve_ledger_path,
    seed_from_provenance,
    validate_disposition,
    write_dispositions,
)
from kbutillib.domains.kbase.berdl.council_schema import (
    JOB_DISPOSITION_TABLE,
    column_names,
)
from kbutillib.domains.kbase.berdl.council_sink import SqliteCouncilSink

#: The checked-in seed. Copied into the repo rather than read from Dropbox:
#: a test that reads a synced path passes according to whether a sync client
#: has caught up.
PROVENANCE = Path(__file__).parent / "fixtures" / "AUTHORITATIVE-JOB-PROVENANCE.json"

#: The declared identity every test uses.
STORE_ID = "poplar/council-1"

#: One snapshot's identity. The digest is not a real snapshot's -- these tests
#: are about dispositions, and the archiver's tests cover real manifests.
SNAPSHOT_ID = "a" * 64
SNAPSHOT_SEQ = 1
STORE_HOST = "poplar"
BATCH = "01JBATCHDISPOSITION00000001"

#: Two jobs from the fixture, named here ONLY as read-back assertions about
#: what the file says. The seed never transcribes an id into code -- it is
#: derived -- but a test has to be able to point at a specific job to show
#: that the biggest, most authoritative-looking one is excluded.
#:
#: 477d6c53... is the CANCELLED 19,276-item monolith: 'done'-looking label,
#: full size, 49 genuine verdicts, and it must never publish.
CANCELLED_MONOLITH = "477d6c53cf3b4547b2a4b750aabe5adf"
#: 2c94eb1b... is chunk01 of the real dataset.
PUBLISHED_CHUNK = "2c94eb1b7abd41dca337a1eb9d78d652"
#: A job id that is in no fixture and has no declaration anywhere.
UNDECLARED = "ffffffffffffffffffffffffffffffff"


@pytest.fixture
def ledger(tmp_path: Path) -> DispositionLedger:
    return DispositionLedger(resolve_ledger_path(STORE_ID, root=tmp_path / "ledgers"))


@pytest.fixture
def sink(tmp_path: Path):
    opened = SqliteCouncilSink(tmp_path / "lake" / "council.sqlite")
    opened.bootstrap()
    try:
        yield opened
    finally:
        opened.close()


def _seeded(ledger: DispositionLedger, sink: SqliteCouncilSink, **extra) -> dict:
    """Seed from the FIXTURE FILE and write the rows. The real path, twice."""
    seed_from_provenance(ledger, PROVENANCE, declared_by="miles")
    return write_dispositions(
        sink,
        ledger,
        store_id=STORE_ID,
        snapshot_id=SNAPSHOT_ID,
        snapshot_seq=SNAPSHOT_SEQ,
        store_host=STORE_HOST,
        load_batch_id=BATCH,
        **extra,
    )


# --------------------------------------------------------------------------
# The enum
# --------------------------------------------------------------------------


class TestTheEnum:
    def test_the_five_declared_values_are_accepted(self) -> None:
        assert len(JOB_DISPOSITIONS) == 5
        for value in JOB_DISPOSITIONS:
            assert validate_disposition(value) == value

    def test_a_sixth_value_is_rejected(self) -> None:
        with pytest.raises(UnknownDispositionError) as excinfo:
            validate_disposition("archived")
        assert "archived" in str(excinfo.value)
        assert "published" in str(excinfo.value)

    @pytest.mark.parametrize("bad", ["", None, "Published", "PUBLISHED", 7, "load"])
    def test_near_misses_are_rejected_too(self, bad) -> None:
        """Case matters: a STRING comparison is case-sensitive downstream."""
        with pytest.raises(UnknownDispositionError):
            validate_disposition(bad)

    def test_the_enum_is_rejected_at_the_write_boundary(
        self, ledger: DispositionLedger
    ) -> None:
        with pytest.raises(UnknownDispositionError):
            declare(
                ledger,
                job_id="j1",
                disposition="archived",
                reason="nope",
                declared_by="miles",
            )
        assert ledger.records() == []


# --------------------------------------------------------------------------
# Reading the real provenance file
# --------------------------------------------------------------------------


class TestTheProvenanceFile:
    def test_the_fixture_is_the_21_job_file(self) -> None:
        raw = json.loads(PROVENANCE.read_text(encoding="utf-8"))
        assert len(raw["jobs"]) == 21
        assert len(raw["LOAD_THESE"]) == 12

    def test_twelve_published_and_nine_excluded(self) -> None:
        declarations = read_provenance(PROVENANCE)
        assert len(declarations) == 21
        published = [d for d in declarations if d.disposition == PUBLISHED]
        excluded = [d for d in declarations if d.disposition != PUBLISHED]
        assert len(published) == 12
        assert len(excluded) == 9

    def test_every_disposition_is_derived_not_transcribed(self) -> None:
        """The counts per excluding kind, as the file's own prose classifies."""
        by_kind: dict[str, int] = {}
        for declaration in read_provenance(PROVENANCE):
            by_kind[declaration.disposition] = by_kind.get(declaration.disposition, 0) + 1
        assert by_kind == {"published": 12, "trial": 4, "pilot": 3, "cancelled": 2}

    def test_the_cancelled_monolith_is_cancelled_not_superseded(self) -> None:
        """Its prose names BOTH words; first occurrence decides, and it leads
        with 'cancelled'."""
        by_id = {d.job_id: d for d in read_provenance(PROVENANCE)}
        row = by_id[CANCELLED_MONOLITH]
        assert row.disposition == "cancelled"
        assert "superseded" in row.reason.lower()
        assert "cancelled" in row.reason.lower()

    def test_the_reason_prose_is_kept_whole(self) -> None:
        by_id = {d.job_id: d for d in read_provenance(PROVENANCE)}
        assert by_id[CANCELLED_MONOLITH].reason.startswith("DO NOT LOAD")
        assert "claim-cost defect" in by_id[CANCELLED_MONOLITH].reason

    def test_an_unclassifiable_prose_is_refused_not_guessed(self, tmp_path: Path) -> None:
        path = tmp_path / "p.json"
        path.write_text(
            json.dumps(
                {"jobs": [{"job_id": "j1", "disposition": "DO NOT LOAD -- reasons"}]}
            ),
            encoding="utf-8",
        )
        with pytest.raises(ProvenanceFormatError) as excinfo:
            read_provenance(path)
        assert "j1" in str(excinfo.value)

    def test_a_prose_with_neither_prefix_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "p.json"
        path.write_text(
            json.dumps({"jobs": [{"job_id": "j1", "disposition": "maybe"}]}),
            encoding="utf-8",
        )
        with pytest.raises(ProvenanceFormatError):
            read_provenance(path)

    def test_a_job_with_no_prose_is_refused(self, tmp_path: Path) -> None:
        """Silence about publishability is an error, never 'published'."""
        path = tmp_path / "p.json"
        path.write_text(json.dumps({"jobs": [{"job_id": "j1"}]}), encoding="utf-8")
        with pytest.raises(ProvenanceFormatError):
            read_provenance(path)

    def test_a_file_contradicting_its_own_load_these_is_refused(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "p.json"
        path.write_text(
            json.dumps(
                {
                    "LOAD_THESE": ["j1"],
                    "jobs": [
                        {"job_id": "j1", "disposition": "DO NOT LOAD -- cancelled"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ProvenanceFormatError) as excinfo:
            read_provenance(path)
        assert "LOAD_THESE" in str(excinfo.value)

    def test_load_these_naming_an_absent_job_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "p.json"
        path.write_text(
            json.dumps(
                {
                    "LOAD_THESE": ["j1", "ghost"],
                    "jobs": [{"job_id": "j1", "disposition": "LOAD -- yes"}],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ProvenanceFormatError) as excinfo:
            read_provenance(path)
        assert "ghost" in str(excinfo.value)

    def test_a_duplicated_job_id_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "p.json"
        path.write_text(
            json.dumps(
                {
                    "jobs": [
                        {"job_id": "j1", "disposition": "LOAD -- yes"},
                        {"job_id": "j1", "disposition": "DO NOT LOAD -- pilot"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ProvenanceFormatError):
            read_provenance(path)

    def test_an_empty_jobs_list_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "p.json"
        path.write_text(json.dumps({"jobs": []}), encoding="utf-8")
        with pytest.raises(ProvenanceFormatError):
            read_provenance(path)


# --------------------------------------------------------------------------
# Seeding: idempotency and provenance
# --------------------------------------------------------------------------


class TestSeeding:
    def test_the_seed_lands_twelve_published_and_nine_excluded(
        self, ledger: DispositionLedger
    ) -> None:
        report = seed_from_provenance(ledger, PROVENANCE, declared_by="miles")
        assert report["jobs"] == 21
        assert report["counts"]["published"] == 12
        assert sum(report["counts"].values()) == 21
        assert len(report["published"]) == 12
        assert len(report["excluded"]) == 9
        assert len(report["added"]) == 21
        assert report["changed"] == [] and report["unchanged"] == []

    def test_re_seeding_the_same_file_is_a_no_op(self, ledger: DispositionLedger) -> None:
        seed_from_provenance(ledger, PROVENANCE, declared_by="miles")
        before = ledger.path.read_bytes()
        again = seed_from_provenance(ledger, PROVENANCE, declared_by="miles")

        assert again["added"] == [] and again["changed"] == []
        assert len(again["unchanged"]) == 21
        # NOTHING was appended and nothing was restamped: byte-identical.
        assert ledger.path.read_bytes() == before
        assert len(ledger.records()) == 21

    def test_re_seeding_by_a_different_person_is_still_a_no_op(
        self, ledger: DispositionLedger
    ) -> None:
        """An unchanged declaration does not get re-attributed."""
        seed_from_provenance(ledger, PROVENANCE, declared_by="miles")
        before = ledger.path.read_bytes()
        seed_from_provenance(ledger, PROVENANCE, declared_by="somebody-else")
        assert ledger.path.read_bytes() == before
        assert ledger.current()[PUBLISHED_CHUNK]["declared_by"] == "miles"

    def test_the_ledger_records_who_and_when_and_the_source(
        self, ledger: DispositionLedger
    ) -> None:
        seed_from_provenance(ledger, PROVENANCE, declared_by="miles")
        record = ledger.current()[PUBLISHED_CHUNK]
        assert record["declared_by"] == "miles"
        assert record["declared_at"]
        assert record["source"].endswith("AUTHORITATIVE-JOB-PROVENANCE.json")

    def test_a_declaration_with_no_author_is_refused(
        self, ledger: DispositionLedger
    ) -> None:
        with pytest.raises(DispositionError):
            declare(
                ledger,
                job_id="j1",
                disposition=PUBLISHED,
                reason="r",
                declared_by="  ",
            )


class TestAnExplicitChangeKeepsWhoSaidWhat:
    def test_re_declaring_a_pilot_as_published_is_an_explicit_update(
        self, ledger: DispositionLedger
    ) -> None:
        first = declare(
            ledger,
            job_id="j1",
            disposition="pilot",
            reason="500-item pilot",
            declared_by="miles",
        )
        second = declare(
            ledger,
            job_id="j1",
            disposition=PUBLISHED,
            reason="roster re-verified",
            declared_by="chenry",
        )

        assert first["outcome"] == "added"
        assert second["outcome"] == "changed"
        # The NEW declaration carries the new author and its own timestamp,
        # and names what it superseded.
        assert second["record"]["declared_by"] == "chenry"
        assert second["record"]["declared_at"] >= first["record"]["declared_at"]
        assert second["record"]["supersedes"]["disposition"] == "pilot"
        assert second["record"]["supersedes"]["declared_by"] == "miles"

    def test_the_superseded_declaration_is_not_lost(
        self, ledger: DispositionLedger
    ) -> None:
        declare(ledger, job_id="j1", disposition="pilot", reason="p", declared_by="miles")
        declare(
            ledger, job_id="j1", disposition=PUBLISHED, reason="q", declared_by="chenry"
        )

        history = ledger.history("j1")
        assert [r["disposition"] for r in history] == ["pilot", PUBLISHED]
        assert [r["declared_by"] for r in history] == ["miles", "chenry"]
        # The CURRENT projection is the last one only.
        assert ledger.current()["j1"]["disposition"] == PUBLISHED

    def test_an_identical_re_declaration_appends_nothing(
        self, ledger: DispositionLedger
    ) -> None:
        declare(ledger, job_id="j1", disposition="pilot", reason="p", declared_by="miles")
        result = declare(
            ledger, job_id="j1", disposition="pilot", reason="p", declared_by="chenry"
        )
        assert result["outcome"] == "unchanged"
        assert len(ledger.history("j1")) == 1

    def test_a_changed_reason_alone_is_still_a_new_declaration(
        self, ledger: DispositionLedger
    ) -> None:
        """Why a job is excluded is part of the declaration, not a comment."""
        declare(ledger, job_id="j1", disposition="pilot", reason="p", declared_by="miles")
        result = declare(
            ledger,
            job_id="j1",
            disposition="pilot",
            reason="p, and the roster differed",
            declared_by="chenry",
        )
        assert result["outcome"] == "changed"
        assert len(ledger.history("j1")) == 2

    def test_a_truncated_trailing_line_is_skipped_not_fatal(
        self, ledger: DispositionLedger
    ) -> None:
        declare(ledger, job_id="j1", disposition="pilot", reason="p", declared_by="miles")
        with ledger.path.open("a", encoding="utf-8") as handle:
            handle.write('{"job_id": "j2", "disp')
        assert list(ledger.current()) == ["j1"]


# --------------------------------------------------------------------------
# The lakehouse rows
# --------------------------------------------------------------------------


class TestTheArchivedRows:
    def test_the_rows_carry_exactly_the_declared_columns(
        self, ledger: DispositionLedger
    ) -> None:
        seed_from_provenance(ledger, PROVENANCE, declared_by="miles")
        rows = disposition_rows(
            ledger,
            store_id=STORE_ID,
            snapshot_id=SNAPSHOT_ID,
            snapshot_seq=SNAPSHOT_SEQ,
            store_host=STORE_HOST,
            load_batch_id=BATCH,
        )
        assert len(rows) == 21
        for row in rows:
            assert tuple(row) == column_names(JOB_DISPOSITION_TABLE)

    def test_the_rows_land_in_the_table_and_read_back(
        self, ledger: DispositionLedger, sink: SqliteCouncilSink
    ) -> None:
        report = _seeded(ledger, sink)
        assert report["rows"] == 21
        assert len(report["published"]) == 12
        assert sink.count_rows(JOB_DISPOSITION_TABLE) == 21

        archived = dispositions(sink, store_id=STORE_ID)
        assert len(archived) == 21
        by_id = {row["job_id"]: row for row in archived}
        assert by_id[PUBLISHED_CHUNK]["disposition"] == PUBLISHED
        assert by_id[CANCELLED_MONOLITH]["disposition"] == "cancelled"
        assert by_id[CANCELLED_MONOLITH]["declared_by"] == "miles"

    def test_one_row_per_job_per_snapshot(
        self, ledger: DispositionLedger, sink: SqliteCouncilSink
    ) -> None:
        """The table's declared key. A history would break it for every reader."""
        declare(ledger, job_id="j1", disposition="pilot", reason="p", declared_by="miles")
        declare(
            ledger, job_id="j1", disposition=PUBLISHED, reason="q", declared_by="chenry"
        )
        write_dispositions(
            sink,
            ledger,
            store_id=STORE_ID,
            snapshot_id=SNAPSHOT_ID,
            snapshot_seq=SNAPSHOT_SEQ,
            store_host=STORE_HOST,
            load_batch_id=BATCH,
        )
        rows = sink.select_rows(JOB_DISPOSITION_TABLE, where={"job_id": "j1"})
        assert len(rows) == 1
        assert rows[0]["disposition"] == PUBLISHED
        assert rows[0]["declared_by"] == "chenry"

    def test_a_second_write_for_the_same_snapshot_is_refused(
        self, ledger: DispositionLedger, sink: SqliteCouncilSink
    ) -> None:
        _seeded(ledger, sink)
        with pytest.raises(DispositionError) as excinfo:
            _seeded(ledger, sink)
        assert SNAPSHOT_ID in str(excinfo.value)
        assert sink.count_rows(JOB_DISPOSITION_TABLE) == 21


# --------------------------------------------------------------------------
# THE GATE, from both sides
# --------------------------------------------------------------------------


class TestTheResolver:
    def test_a_published_job_resolves(
        self, ledger: DispositionLedger, sink: SqliteCouncilSink
    ) -> None:
        _seeded(ledger, sink)
        resolved = published_jobs(sink, store_id=STORE_ID)
        assert len(resolved) == 12
        assert PUBLISHED_CHUNK in resolved

    def test_the_same_job_declared_pilot_does_NOT_resolve(
        self, ledger: DispositionLedger, sink: SqliteCouncilSink
    ) -> None:
        """Same job, same seed path, one declaration changed. It drops out."""
        seed_from_provenance(ledger, PROVENANCE, declared_by="miles")
        declare(
            ledger,
            job_id=PUBLISHED_CHUNK,
            disposition="pilot",
            reason="re-declared: roster under review",
            declared_by="chenry",
        )
        write_dispositions(
            sink,
            ledger,
            store_id=STORE_ID,
            snapshot_id=SNAPSHOT_ID,
            snapshot_seq=SNAPSHOT_SEQ,
            store_host=STORE_HOST,
            load_batch_id=BATCH,
        )
        resolved = published_jobs(sink, store_id=STORE_ID)
        assert PUBLISHED_CHUNK not in resolved
        assert len(resolved) == 11

    def test_the_cancelled_look_alikes_never_resolve(
        self, ledger: DispositionLedger, sink: SqliteCouncilSink
    ) -> None:
        _seeded(ledger, sink)
        assert CANCELLED_MONOLITH not in published_jobs(sink, store_id=STORE_ID)

    def test_an_undeclared_job_does_not_resolve(
        self, ledger: DispositionLedger, sink: SqliteCouncilSink
    ) -> None:
        _seeded(ledger, sink)
        assert UNDECLARED not in published_jobs(sink, store_id=STORE_ID)

    def test_an_empty_table_resolves_NOTHING_not_everything(
        self, sink: SqliteCouncilSink
    ) -> None:
        """The failure mode that matters: no data must not read as consent."""
        assert published_jobs(sink, store_id=STORE_ID) == []
        assert dispositions(sink, store_id=STORE_ID) == []

    def test_the_resolver_reads_the_CURRENT_snapshot_only(
        self, ledger: DispositionLedger, sink: SqliteCouncilSink
    ) -> None:
        """A job published in snapshot 1 and re-declared pilot in snapshot 2
        is NOT publishable. A union over snapshots would carry it forever."""
        seed_from_provenance(ledger, PROVENANCE, declared_by="miles")
        write_dispositions(
            sink,
            ledger,
            store_id=STORE_ID,
            snapshot_id="1" * 64,
            snapshot_seq=1,
            store_host=STORE_HOST,
            load_batch_id=BATCH,
        )
        declare(
            ledger,
            job_id=PUBLISHED_CHUNK,
            disposition="superseded",
            reason="a later council overlapped these functions",
            declared_by="chenry",
        )
        write_dispositions(
            sink,
            ledger,
            store_id=STORE_ID,
            snapshot_id="2" * 64,
            snapshot_seq=2,
            store_host=STORE_HOST,
            load_batch_id=BATCH + "2",
        )

        assert PUBLISHED_CHUNK not in published_jobs(sink, store_id=STORE_ID)
        # Snapshot 1 is still independently readable and still says published.
        assert PUBLISHED_CHUNK in published_jobs(
            sink, store_id=STORE_ID, snapshot_id="1" * 64
        )

    def test_query_name_filtering_needs_the_archived_jobs_table(
        self, ledger: DispositionLedger, sink: SqliteCouncilSink
    ) -> None:
        """job_disposition has no query_name column, so the filter joins
        `jobs` for the same snapshot -- and says NOTHING when it is absent
        rather than guessing."""
        _seeded(ledger, sink)
        assert published_jobs(sink, store_id=STORE_ID, query_name="go_mf") == []

        sink.append_rows(
            "jobs",
            [
                {
                    "store_id": STORE_ID,
                    "snapshot_id": SNAPSHOT_ID,
                    "snapshot_seq": SNAPSHOT_SEQ,
                    "store_host": STORE_HOST,
                    "loaded_at": None,
                    "load_batch_id": BATCH,
                    "job_id": PUBLISHED_CHUNK,
                    "query_name": "go_mf",
                    "query_version": "4",
                }
            ],
        )
        assert published_jobs(sink, store_id=STORE_ID, query_name="go_mf") == [
            PUBLISHED_CHUNK
        ]
        assert published_jobs(sink, store_id=STORE_ID, query_name="other") == []


class TestRequirePublished:
    def test_a_published_job_passes(
        self, ledger: DispositionLedger, sink: SqliteCouncilSink
    ) -> None:
        _seeded(ledger, sink)
        assert require_published(sink, [PUBLISHED_CHUNK], store_id=STORE_ID) == [
            PUBLISHED_CHUNK
        ]

    def test_a_declared_pilot_is_refused_and_the_disposition_is_named(
        self, ledger: DispositionLedger, sink: SqliteCouncilSink
    ) -> None:
        _seeded(ledger, sink)
        pilot = [
            d.job_id for d in read_provenance(PROVENANCE) if d.disposition == "pilot"
        ][0]
        with pytest.raises(JobNotPublishableError) as excinfo:
            require_published(sink, [pilot], store_id=STORE_ID)
        assert pilot in str(excinfo.value)
        assert "pilot" in str(excinfo.value)

    def test_an_UNDECLARED_job_is_refused_and_said_to_be_undeclared(
        self, ledger: DispositionLedger, sink: SqliteCouncilSink
    ) -> None:
        """The two refusals are distinguishable: 'never declared' and
        'declared cancelled' need different fixes."""
        _seeded(ledger, sink)
        with pytest.raises(JobNotPublishableError) as excinfo:
            require_published(sink, [UNDECLARED], store_id=STORE_ID)
        message = str(excinfo.value)
        assert UNDECLARED in message
        assert "NO disposition row" in message

    def test_one_bad_job_in_a_good_set_refuses_the_whole_set(
        self, ledger: DispositionLedger, sink: SqliteCouncilSink
    ) -> None:
        _seeded(ledger, sink)
        with pytest.raises(JobNotPublishableError) as excinfo:
            require_published(
                sink, [PUBLISHED_CHUNK, CANCELLED_MONOLITH], store_id=STORE_ID
            )
        assert CANCELLED_MONOLITH in str(excinfo.value)
        assert "1 of 2" in str(excinfo.value)

    def test_the_twelve_published_jobs_all_pass_together(
        self, ledger: DispositionLedger, sink: SqliteCouncilSink
    ) -> None:
        _seeded(ledger, sink)
        resolved = published_jobs(sink, store_id=STORE_ID)
        assert require_published(sink, resolved, store_id=STORE_ID) == resolved

    def test_an_empty_set_is_refused_rather_than_passing(
        self, sink: SqliteCouncilSink
    ) -> None:
        with pytest.raises(DispositionError):
            require_published(sink, [], store_id=STORE_ID)


# --------------------------------------------------------------------------
# NO-INJECTION: the gate through the REAL CLI, exit codes and all
# --------------------------------------------------------------------------


class TestTheGateThroughTheRealCli:
    """``kbu council disposition`` end to end: seed the fixture, publish the
    rows, then ask the gate. Nothing is patched and no list of published ids
    is handed to anything."""

    @staticmethod
    def _invoke(*args: str):
        return CliRunner().invoke(main, ["council", *args])

    @pytest.fixture
    def seeded_cli(self, tmp_path: Path):
        """Seed and publish through the CLI; yield the two paths it used."""
        ledger_root = tmp_path / "ledgers"
        lake = tmp_path / "lake" / "council.sqlite"
        seed = self._invoke(
            "disposition",
            "seed",
            str(PROVENANCE),
            "--store-id",
            STORE_ID,
            "--declared-by",
            "miles",
            "--ledger-root",
            str(ledger_root),
            "--json",
        )
        assert seed.exit_code == 0, seed.output
        publish = self._invoke(
            "disposition",
            "publish",
            "--store-id",
            STORE_ID,
            "--snapshot-id",
            SNAPSHOT_ID,
            "--snapshot-seq",
            "1",
            "--load-batch-id",
            BATCH,
            "--store-host",
            STORE_HOST,
            "--ledger-root",
            str(ledger_root),
            "--lakehouse",
            str(lake),
            "--json",
        )
        assert publish.exit_code == 0, publish.output
        return {"ledger_root": ledger_root, "lake": lake, "seed": seed, "publish": publish}

    def test_the_seed_reports_twelve_published_and_nine_excluded(self, seeded_cli) -> None:
        data = json.loads(seeded_cli["seed"].output)["data"]
        assert data["counts"]["published"] == 12
        assert len(data["excluded"]) == 9
        assert data["jobs"] == 21

    def test_re_seeding_through_the_cli_is_a_no_op(self, seeded_cli, tmp_path) -> None:
        ledger_path = resolve_ledger_path(STORE_ID, root=seeded_cli["ledger_root"])
        before = ledger_path.read_bytes()
        again = self._invoke(
            "disposition",
            "seed",
            str(PROVENANCE),
            "--store-id",
            STORE_ID,
            "--declared-by",
            "miles",
            "--ledger-root",
            str(seeded_cli["ledger_root"]),
            "--json",
        )
        assert again.exit_code == 0
        assert json.loads(again.output)["data"]["added"] == []
        assert ledger_path.read_bytes() == before

    def test_checking_a_published_job_exits_zero(self, seeded_cli) -> None:
        result = self._invoke(
            "disposition",
            "check",
            PUBLISHED_CHUNK,
            "--store-id",
            STORE_ID,
            "--lakehouse",
            str(seeded_cli["lake"]),
            "--json",
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["published"] == [PUBLISHED_CHUNK]

    def test_checking_an_UNDISPOSITIONED_job_exits_NON_ZERO_naming_it(
        self, seeded_cli
    ) -> None:
        """THE test. A job nobody declared must not be publishable, and the
        process has to say which job it refused."""
        result = self._invoke(
            "disposition",
            "check",
            UNDECLARED,
            "--store-id",
            STORE_ID,
            "--lakehouse",
            str(seeded_cli["lake"]),
            "--json",
        )
        assert result.exit_code != 0
        combined = result.output + str(result.exception or "")
        assert UNDECLARED in combined
        assert "NO disposition row" in combined

    def test_checking_the_cancelled_look_alike_exits_NON_ZERO_naming_it(
        self, seeded_cli
    ) -> None:
        result = self._invoke(
            "disposition",
            "check",
            CANCELLED_MONOLITH,
            "--store-id",
            STORE_ID,
            "--lakehouse",
            str(seeded_cli["lake"]),
            "--json",
        )
        assert result.exit_code != 0
        combined = result.output + str(result.exception or "")
        assert CANCELLED_MONOLITH in combined
        assert "cancelled" in combined

    def test_listing_shows_the_current_declaration_per_job(self, seeded_cli) -> None:
        result = self._invoke(
            "disposition",
            "list",
            "--store-id",
            STORE_ID,
            "--ledger-root",
            str(seeded_cli["ledger_root"]),
            "--json",
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["jobs"] == 21
        assert len(data["published"]) == 12

    def test_declaring_a_sixth_disposition_through_the_cli_exits_non_zero(
        self, tmp_path: Path
    ) -> None:
        result = self._invoke(
            "disposition",
            "declare",
            "j1",
            "archived",
            "--store-id",
            STORE_ID,
            "--reason",
            "no such thing",
            "--declared-by",
            "miles",
            "--ledger-root",
            str(tmp_path / "ledgers"),
        )
        assert result.exit_code != 0
        assert "archived" in result.output + str(result.exception or "")

    def test_a_second_publish_for_the_same_snapshot_exits_non_zero(
        self, seeded_cli
    ) -> None:
        result = self._invoke(
            "disposition",
            "publish",
            "--store-id",
            STORE_ID,
            "--snapshot-id",
            SNAPSHOT_ID,
            "--snapshot-seq",
            "1",
            "--load-batch-id",
            BATCH,
            "--ledger-root",
            str(seeded_cli["ledger_root"]),
            "--lakehouse",
            str(seeded_cli["lake"]),
        )
        assert result.exit_code != 0
