"""A council store's DECLARED ARCHIVE IDENTITY, and the refusals around it.

Every row archived out of a store into ``kbaseincubator.council`` is keyed on
``store_id``, and ``job_id`` is unique only WITHIN one store -- which is also
why ``endpoint_state.user`` is the fixed literal ``STORE_IDENTITY``. So the
id has to be declared by a human, persisted inside the database file, and
never change; and ``snapshot``/``export`` have to refuse rather than invent
one, because the clearinghouse payload that carries the id is frozen on first
write (its slot key excludes ``result_type_version``, so a corrected payload
in the same slot is silently dropped as already-present).

NOTHING IS STUBBED HERE. Every store in this module is a real SQLite file
created by the real ``SqliteStore`` constructor and the real
``init_schema()``/``migrate()`` path production uses; every refusal comes from
the real ``snapshot()`` / ``export()`` entry points; and the copied-store
tests copy the file on disk with ``shutil``. There is no fake store object and
no monkeypatched identity reader -- the identity reader IS the thing under
test, so patching it would test nothing.

One test additionally drives the refusal through a real SUBPROCESS, to show
that "refuse" means a non-zero exit and not just a caught exception.

No test here makes a network call, spends a token, or needs a pod.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip(
    "sqlalchemy",
    reason="the council store needs the `council` extra: pip install 'kbutillib[council]'",
)

from kbutillib.domains.ai.council.config import CouncilConfig  # noqa: E402
from kbutillib.domains.ai.council.store import open_store  # noqa: E402
from kbutillib.domains.ai.council.store.schema import (  # noqa: E402
    SCHEMA_VERSION,
    TABLE_NAMES,
)
from kbutillib.domains.ai.council.store.sqlite import (  # noqa: E402
    POPLAR_COUNCIL_STORE_ID,
    STORE_IDENTITY_RECORD_VERSION,
    STORE_IDENTITY_TABLE,
    SqliteStore,
    StoreIdentityAlreadyDeclared,
    StoreIdentityMismatch,
    StoreIdentityMissing,
    declare_identity_once,
    validate_store_id,
)

_DECLARED = "poplar/council-1"


def _make_store(path: Path, **kwargs) -> SqliteStore:
    """A REAL store at *path*, created through the real constructor path."""
    store = open_store(str(path), **kwargs)
    store.init_schema()
    return store


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "council" / "council.sqlite"


@pytest.fixture
def store(store_path: Path):
    opened = _make_store(store_path)
    try:
        yield opened
    finally:
        opened.close()


# --------------------------------------------------------------------------
# A fresh store has no identity
# --------------------------------------------------------------------------


class TestFreshStoreHasNoIdentity:
    def test_a_fresh_store_reports_no_identity(self, store):
        assert store.store_identity() is None

    def test_reading_the_identity_of_a_fresh_store_writes_nothing(self, store):
        # The reader must work on a read-only file, so it must not create its
        # own table on the way past.
        store.store_identity()
        with store.engine.connect() as conn:
            found = conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (STORE_IDENTITY_TABLE,),
            ).first()
        assert found is None

    def test_snapshot_refuses_and_names_the_absence(self, store, tmp_path):
        target = tmp_path / "snap.sqlite"
        with pytest.raises(StoreIdentityMissing) as excinfo:
            store.snapshot(target)
        message = str(excinfo.value)
        assert "NO declared archive identity" in message
        assert "snapshot" in message
        # And it refused BEFORE writing.
        assert not target.exists()

    def test_export_refuses_and_names_the_absence(self, store, tmp_path):
        target = tmp_path / "export"
        with pytest.raises(StoreIdentityMissing) as excinfo:
            store.export(target)
        assert "NO declared archive identity" in str(excinfo.value)
        assert not target.exists()

    def test_the_refusal_exits_non_zero_in_a_real_subprocess(self, store_path):
        """"Refuse" means a non-zero exit, not merely a caught exception."""
        script = (
            "from kbutillib.domains.ai.council.store import open_store\n"
            f"store = open_store({str(store_path)!r})\n"
            "store.init_schema()\n"
            f"store.snapshot({str(store_path.parent / 'snap.sqlite')!r})\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": os.environ.get("PYTHONPATH", "")},
        )
        assert completed.returncode != 0
        assert "StoreIdentityMissing" in completed.stderr


# --------------------------------------------------------------------------
# Assignment
# --------------------------------------------------------------------------


class TestAssignment:
    def test_after_assignment_snapshot_and_export_succeed(self, store, tmp_path):
        store.assign_store_identity(_DECLARED, declared_by="chenry")

        manifest = store.snapshot(tmp_path / "snap.sqlite")
        assert manifest["store_id"] == _DECLARED
        assert (tmp_path / "snap.sqlite").exists()
        assert manifest["bytes"] > 0
        # snapshot_id is the VERIFIED digest of the file as written -- which
        # is what council_schema's snapshot_id column holds.
        digest = __import__("hashlib").sha256(
            (tmp_path / "snap.sqlite").read_bytes()
        ).hexdigest()
        assert manifest["snapshot_id"] == digest

        exported = store.export(tmp_path / "export")
        assert exported["store_id"] == _DECLARED
        assert (tmp_path / "export" / "MANIFEST.json").exists()

    def test_the_record_carries_when_and_by_whom_and_its_own_version(self, store):
        record = store.assign_store_identity(_DECLARED, declared_by="chenry")
        assert record["store_id"] == _DECLARED
        assert record["declared_by"] == "chenry"
        assert record["declared_at"]
        assert record["record_version"] == STORE_IDENTITY_RECORD_VERSION
        assert record["supersedes"] is None

    def test_declared_by_is_never_left_blank(self, store):
        record = store.assign_store_identity(_DECLARED)
        assert record["declared_by"]

    def test_assignment_is_idempotent(self, store):
        first = store.assign_store_identity(_DECLARED, declared_by="chenry")
        second = store.assign_store_identity(_DECLARED, declared_by="someone-else")
        third = store.assign_store_identity(_DECLARED)
        assert second == first
        assert third == first
        # One row, not three: re-declaring the same id writes nothing.
        with store.engine.connect() as conn:
            rows = conn.exec_driver_sql(
                f"SELECT COUNT(*) FROM {STORE_IDENTITY_TABLE}"
            ).scalar_one()
        assert rows == 1

    def test_the_record_persists_across_a_reopen(self, store_path):
        opened = _make_store(store_path)
        opened.assign_store_identity(_DECLARED, declared_by="chenry")
        opened.close()

        reopened = _make_store(store_path)
        try:
            assert reopened.store_identity()["store_id"] == _DECLARED
        finally:
            reopened.close()

    def test_the_identity_table_is_outside_the_versioned_schema(self, store):
        # Deliberate: every existing store was created at this SCHEMA_VERSION
        # without the table, and migrate() adds columns and indexes, not
        # tables. An identity you can only get by migrating is an identity
        # the existing store cannot have.
        assert STORE_IDENTITY_TABLE not in TABLE_NAMES
        store.assign_store_identity(_DECLARED)
        assert store.schema_version() == SCHEMA_VERSION


class TestThePoplarOneOff:
    def test_the_declared_poplar_id_is_the_documented_constant(self):
        assert POPLAR_COUNCIL_STORE_ID == "poplar/council-1"

    def test_declare_identity_once_assigns_and_is_idempotent(self, store_path):
        created = _make_store(store_path)
        created.close()

        first = declare_identity_once(
            str(store_path), POPLAR_COUNCIL_STORE_ID, declared_by="chenry"
        )
        second = declare_identity_once(
            str(store_path), POPLAR_COUNCIL_STORE_ID, declared_by="chenry"
        )
        assert first["store_id"] == POPLAR_COUNCIL_STORE_ID
        assert second == first

        reopened = _make_store(store_path)
        try:
            assert (
                reopened.store_identity()["store_id"] == POPLAR_COUNCIL_STORE_ID
            )
        finally:
            reopened.close()

    def test_declare_identity_once_refuses_a_different_id(self, store_path):
        created = _make_store(store_path)
        created.close()
        declare_identity_once(str(store_path), POPLAR_COUNCIL_STORE_ID)
        with pytest.raises(StoreIdentityAlreadyDeclared):
            declare_identity_once(str(store_path), "elsewhere/council-1")

    def test_declare_identity_once_works_on_a_version_mismatched_store(
        self, store_path
    ):
        # "Which store did these bytes come from" is exactly the question
        # asked about an old file, so the one-off must not need a migration
        # first. It opens with check_version=False.
        created = _make_store(store_path)
        with created.transaction() as conn:
            conn.exec_driver_sql("UPDATE schema_version SET version = 99")
        created.close()

        with pytest.raises(Exception):
            open_store(str(store_path))  # the real version check still bites

        record = declare_identity_once(
            str(store_path), POPLAR_COUNCIL_STORE_ID, declared_by="chenry"
        )
        assert record["store_id"] == POPLAR_COUNCIL_STORE_ID


# --------------------------------------------------------------------------
# A copy carries the identity; a writable fork must redeclare
# --------------------------------------------------------------------------


class TestACopiedStoreFileCarriesTheIdentity:
    def test_a_real_shutil_copy_of_the_file_carries_the_identity(
        self, store_path, tmp_path
    ):
        original = _make_store(store_path)
        original.assign_store_identity(_DECLARED, declared_by="chenry")
        original.close()

        moved = tmp_path / "moved" / "council.sqlite"
        moved.parent.mkdir(parents=True)
        shutil.copy2(store_path, moved)

        opened = _make_store(moved)
        try:
            # A MOVE retains the identity -- that is the whole reason the
            # record lives inside the database rather than in config or a
            # sidecar.
            assert opened.store_identity()["store_id"] == _DECLARED
            manifest = opened.snapshot(tmp_path / "snap.sqlite")
            assert manifest["store_id"] == _DECLARED
        finally:
            opened.close()

    def test_a_read_only_snapshot_carries_the_identity(self, store, tmp_path):
        store.assign_store_identity(_DECLARED, declared_by="chenry")
        store.snapshot(tmp_path / "snap.sqlite")

        from_snapshot = _make_store(tmp_path / "snap.sqlite")
        try:
            assert from_snapshot.store_identity()["store_id"] == _DECLARED
        finally:
            from_snapshot.close()

    def test_an_independently_writable_fork_needs_the_explicit_act(
        self, store_path, tmp_path
    ):
        original = _make_store(store_path)
        original.assign_store_identity(_DECLARED, declared_by="chenry")
        original.close()

        fork = tmp_path / "fork" / "council.sqlite"
        fork.parent.mkdir(parents=True)
        shutil.copy2(store_path, fork)

        forked = _make_store(fork)
        try:
            # The ordinary assignment path REFUSES rather than inferring a
            # fork: two independently writable stores under one id silently
            # merge two unrelated histories.
            with pytest.raises(StoreIdentityAlreadyDeclared) as excinfo:
                forked.assign_store_identity("poplar/council-2")
            message = str(excinfo.value)
            assert _DECLARED in message
            assert "poplar/council-2" in message
            assert forked.store_identity()["store_id"] == _DECLARED

            # The explicit act, and only it, replaces the identity.
            record = forked.redeclare_store_identity(
                "poplar/council-2", declared_by="chenry"
            )
            assert record["store_id"] == "poplar/council-2"
            assert record["supersedes"] == _DECLARED
        finally:
            forked.close()

    def test_redeclaring_keeps_the_superseded_row_visible(self, store):
        store.assign_store_identity(_DECLARED, declared_by="chenry")
        store.redeclare_store_identity("poplar/council-2", declared_by="chenry")
        with store.engine.connect() as conn:
            ids = [
                row[0]
                for row in conn.exec_driver_sql(
                    f"SELECT store_id FROM {STORE_IDENTITY_TABLE} "
                    "ORDER BY declared_seq"
                )
            ]
        # Append-only: a later reader can tell a fork from a store that
        # always had its current id.
        assert ids == [_DECLARED, "poplar/council-2"]

    def test_redeclaring_the_current_id_is_a_no_op(self, store):
        first = store.assign_store_identity(_DECLARED, declared_by="chenry")
        assert store.redeclare_store_identity(_DECLARED) == first


# --------------------------------------------------------------------------
# Mismatch: refused, both values named, never auto-healed
# --------------------------------------------------------------------------


class TestConfiguredVersusStoredMismatch:
    def _mismatched(self, store_path: Path) -> SqliteStore:
        created = _make_store(store_path)
        created.assign_store_identity(_DECLARED, declared_by="chenry")
        created.close()
        # The REAL constructor path, carrying a configured id that disagrees.
        opened = open_store(str(store_path), store_id="elsewhere/council-9")
        return opened

    def test_snapshot_refuses_naming_both_values(self, store_path, tmp_path):
        opened = self._mismatched(store_path)
        try:
            target = tmp_path / "snap.sqlite"
            with pytest.raises(StoreIdentityMismatch) as excinfo:
                opened.snapshot(target)
            message = str(excinfo.value)
            assert "elsewhere/council-9" in message
            assert _DECLARED in message
            assert not target.exists()
        finally:
            opened.close()

    def test_export_refuses_naming_both_values(self, store_path, tmp_path):
        opened = self._mismatched(store_path)
        try:
            target = tmp_path / "export"
            with pytest.raises(StoreIdentityMismatch) as excinfo:
                opened.export(target)
            message = str(excinfo.value)
            assert "elsewhere/council-9" in message
            assert _DECLARED in message
            assert not target.exists()
        finally:
            opened.close()

    def test_a_mismatch_is_never_auto_healed(self, store_path, tmp_path):
        opened = self._mismatched(store_path)
        try:
            with pytest.raises(StoreIdentityMismatch):
                opened.snapshot(tmp_path / "snap.sqlite")
            with pytest.raises(StoreIdentityMismatch):
                opened.export(tmp_path / "export")
            # The stored record is untouched by the refusal, in both
            # directions: neither the config nor the store wins.
            assert opened.store_identity()["store_id"] == _DECLARED
        finally:
            opened.close()

    def test_a_matching_configured_identity_passes(self, store_path, tmp_path):
        created = _make_store(store_path)
        created.assign_store_identity(_DECLARED, declared_by="chenry")
        created.close()
        opened = open_store(str(store_path), store_id=_DECLARED)
        try:
            assert opened.snapshot(tmp_path / "snap.sqlite")["store_id"] == _DECLARED
        finally:
            opened.close()

    def test_no_configured_identity_means_do_not_compare(self, store, tmp_path):
        # Still requires the store's OWN record to exist; it just does not
        # compare against anything.
        assert store.configured_store_id is None
        with pytest.raises(StoreIdentityMissing):
            store.snapshot(tmp_path / "snap.sqlite")
        store.assign_store_identity(_DECLARED)
        assert store.snapshot(tmp_path / "snap.sqlite")["store_id"] == _DECLARED

    def test_an_invalid_configured_identity_fails_at_open_time(self, store_path):
        created = _make_store(store_path)
        created.close()
        with pytest.raises(ValueError):
            open_store(str(store_path), store_id="/scratch/chenry/council")


# --------------------------------------------------------------------------
# No automatic default, anywhere
# --------------------------------------------------------------------------


class TestNoAutomaticDefault:
    def test_the_config_field_defaults_to_none(self):
        assert CouncilConfig().store_id is None

    def test_the_config_default_is_not_a_hostname_path_or_uuid(self):
        # The assertion is the ABSENCE of a value: an automatic default would
        # be indistinguishable from a correct one until the first store
        # moved, by which time no already-written payload could be fixed.
        config = CouncilConfig()
        assert config.store_id is None
        assert config.store_id != os.uname().nodename
        assert config.store_id != config.store

    def test_a_store_never_assigns_itself_an_identity(self, store_path):
        opened = _make_store(store_path)
        try:
            # init_schema(), migrate() and add_missing_columns() are the whole
            # store-creation path; none of them may produce an identity.
            opened.migrate()
            opened.add_missing_columns()
            opened.add_missing_indexes()
            assert opened.store_identity() is None
        finally:
            opened.close()

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "   ",
            "/scratch/chenry/council/council.sqlite",
            "~/.kbutillib/council/council.sqlite",
            "./council",
            "council.sqlite",
            "council.db",
            "0123456789abcdef0123456789abcdef",
            "f81d4fae-7dec-11d0-a765-00a0c91e6bf6",
            "poplar council 1",
            "poplar\tcouncil",
            "-leading-dash",
            "x" * 129,
        ],
    )
    def test_validate_store_id_refuses_automatic_looking_values(self, bad):
        with pytest.raises(ValueError):
            validate_store_id(bad)

    def test_validate_store_id_refuses_a_non_string(self):
        with pytest.raises(ValueError):
            validate_store_id(None)

    @pytest.mark.parametrize(
        "good",
        ["poplar/council-1", "poplar-council-1", "h100.council_2", "a"],
    )
    def test_validate_store_id_accepts_a_declared_name(self, good):
        assert validate_store_id(good) == good

    def test_validate_store_id_strips_surrounding_whitespace(self):
        assert validate_store_id("  poplar/council-1  ") == "poplar/council-1"


# --------------------------------------------------------------------------
# What snapshot and export actually produce
# --------------------------------------------------------------------------


class TestSnapshotAndExportOutput:
    def test_the_snapshot_is_a_real_openable_store_not_a_mid_write_copy(
        self, store, tmp_path
    ):
        store.assign_store_identity(_DECLARED, declared_by="chenry")
        store.put_query_type(
            name="go_mf", version="4", spec={"roles": {}}, created_by="chenry"
        )
        store.snapshot(tmp_path / "snap.sqlite")

        from_snapshot = _make_store(tmp_path / "snap.sqlite")
        try:
            assert from_snapshot.get_query_type("go_mf", "4") is not None
        finally:
            from_snapshot.close()

    def test_snapshot_refuses_to_overwrite_by_default(self, store, tmp_path):
        store.assign_store_identity(_DECLARED)
        target = tmp_path / "snap.sqlite"
        store.snapshot(target)
        with pytest.raises(FileExistsError):
            store.snapshot(target)
        assert store.snapshot(target, overwrite=True)["store_id"] == _DECLARED

    def test_the_export_manifest_carries_the_identity_and_a_digest_per_file(
        self, store, tmp_path
    ):
        store.assign_store_identity(_DECLARED, declared_by="chenry")
        store.put_query_type(
            name="go_mf", version="4", spec={"roles": {}}, created_by="chenry"
        )
        target = tmp_path / "export"
        manifest = store.export(target)

        on_disk = json.loads((target / "MANIFEST.json").read_text())
        assert on_disk == manifest
        assert on_disk["store_id"] == _DECLARED
        assert on_disk["identity"]["declared_by"] == "chenry"
        assert on_disk["schema_version"] == SCHEMA_VERSION
        # One file per store table, each with a row count and a digest.
        assert set(on_disk["files"]) == {f"{name}.jsonl" for name in TABLE_NAMES}
        for entry in on_disk["files"].values():
            assert len(entry["sha256"]) == 64
        assert on_disk["files"]["query_types.jsonl"]["rows"] == 1

    def test_the_exported_rows_are_readable_json(self, store, tmp_path):
        store.assign_store_identity(_DECLARED)
        store.put_query_type(
            name="go_mf", version="4", spec={"roles": {}}, created_by="chenry"
        )
        manifest = store.export(tmp_path / "export")
        lines = (tmp_path / "export" / "query_types.jsonl").read_text().splitlines()
        assert len(lines) == manifest["files"]["query_types.jsonl"]["rows"]
        row = json.loads(lines[0])
        assert row["name"] == "go_mf"
        assert row["version"] == "4"

    def test_export_refuses_to_overwrite_by_default(self, store, tmp_path):
        store.assign_store_identity(_DECLARED)
        target = tmp_path / "export"
        store.export(target)
        with pytest.raises(FileExistsError):
            store.export(target)
        assert store.export(target, overwrite=True)["store_id"] == _DECLARED
