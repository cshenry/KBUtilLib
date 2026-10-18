"""Where a council store may and may not live.

Two refusals, both at open time, both from the real
``SqliteStore.__init__`` -> ``_resolve_sqlite_url`` path:

* **Dropbox**, which is NOT overridable.  A live SQLite database is a file
  plus a write-ahead log, and a file syncer uploads them mid-transaction and
  later "resolves" the pair into a corrupt database.  No environment variable
  makes that safe.
* **A network filesystem**, which IS overridable with
  ``KBU_COUNCIL_ALLOW_NETWORK_FS=1``.  NFS, SMB, sshfs and friends either do
  not implement the POSIX locks SQLite's WAL mode needs or do not honour them
  across clients; an operator who knows their mount has one client is
  entitled to say so.

The only thing these tests substitute is the injectable filesystem-type
function -- ``sqlite.filesystem_type_of``, one callable, by monkeypatching the
module attribute the shipped code looks up.  The path normalisation above it
(the URL parsing, the ``~`` expansion, the symlink resolution, the Dropbox
scan) is the shipped code in every test here, because that is the part a stub
could most easily make pass while being broken.

No test here makes a network call, mounts anything, spends a token, needs a
CLI login or needs a GPU.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

pytest.importorskip(
    "sqlalchemy",
    reason="the council store needs the `council` extra: pip install 'kbutillib[council]'",
)

from kbutillib.domains.ai.council.store import open_store  # noqa: E402
from kbutillib.domains.ai.council.store import sqlite as sqlite_module  # noqa: E402
from kbutillib.domains.ai.council.store.errors import StorePathRefused  # noqa: E402
from kbutillib.domains.ai.council.store.sqlite import (  # noqa: E402
    ALLOW_NETWORK_FS_ENV,
    NETWORK_FILESYSTEMS,
    nearest_existing_ancestor,
)


@pytest.fixture
def fake_fstype(monkeypatch: pytest.MonkeyPatch):
    """Substitute ONLY the filesystem-type probe; record what it was asked.

    Returns a callable ``(type_or_None) -> list_of_paths_asked``, so a test
    can also assert that the question was put to the right place.
    """

    def _install(value: Optional[str]) -> list:
        asked: list = []

        def _probe(path: Path) -> Optional[str]:
            asked.append(Path(path))
            return value

        monkeypatch.setattr(sqlite_module, "filesystem_type_of", _probe)
        return asked

    return _install


# ---------------------------------------------------------------------------
# The network-filesystem refusal
# ---------------------------------------------------------------------------


class TestNetworkFilesystemRefusal:
    def test_an_nfs_path_is_refused_and_says_what_to_do(
        self, tmp_path: Path, fake_fstype
    ) -> None:
        fake_fstype("nfs")
        target = tmp_path / "shared" / "council.sqlite"
        with pytest.raises(StorePathRefused) as excinfo:
            open_store(f"sqlite:///{target}")
        message = str(excinfo.value)
        assert "nfs" in message
        # It must name the fix, not merely the problem.
        assert "local disk" in message
        assert ALLOW_NETWORK_FS_ENV in message

    def test_a_refused_path_creates_no_file(self, tmp_path: Path, fake_fstype) -> None:
        fake_fstype("nfs4")
        target = tmp_path / "shared" / "council.sqlite"
        with pytest.raises(StorePathRefused):
            open_store(f"sqlite:///{target}")
        assert not target.exists()

    @pytest.mark.parametrize("fstype", sorted(NETWORK_FILESYSTEMS))
    def test_every_listed_type_is_refused(
        self, tmp_path: Path, fake_fstype, fstype: str
    ) -> None:
        fake_fstype(fstype)
        with pytest.raises(StorePathRefused, match=fstype.replace(".", r"\.")):
            open_store(f"sqlite:///{tmp_path}/council.sqlite")

    def test_the_type_is_matched_case_insensitively(
        self, tmp_path: Path, fake_fstype
    ) -> None:
        """``stat -f %T`` and ``/proc/self/mounts`` do not agree on case."""
        fake_fstype("NFS4")
        with pytest.raises(StorePathRefused):
            open_store(f"sqlite:///{tmp_path}/council.sqlite")

    def test_a_local_filesystem_is_admitted(self, tmp_path: Path, fake_fstype) -> None:
        fake_fstype("xfs")
        store = open_store(f"sqlite:///{tmp_path}/local/council.sqlite")
        try:
            store.init_schema()
            assert store.schema_version() > 0
        finally:
            store.close()

    def test_the_probe_is_asked_about_the_nearest_existing_ancestor(
        self, tmp_path: Path, fake_fstype
    ) -> None:
        """The store file does not exist yet; its directory may not either."""
        asked = fake_fstype("xfs")
        target = tmp_path / "not" / "yet" / "council.sqlite"
        store = open_store(f"sqlite:///{target}")
        try:
            assert asked == [tmp_path]
        finally:
            store.close()


# ---------------------------------------------------------------------------
# The override
# ---------------------------------------------------------------------------


class TestTheOverride:
    def test_the_override_admits_a_network_filesystem(
        self, tmp_path: Path, fake_fstype, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_fstype("nfs")
        monkeypatch.setenv(ALLOW_NETWORK_FS_ENV, "1")
        store = open_store(f"sqlite:///{tmp_path}/council.sqlite")
        try:
            store.init_schema()
            assert store.schema_version() > 0
        finally:
            store.close()

    @pytest.mark.parametrize("value", ["", "0", "yes", "true", "TRUE", "2", " 1 x"])
    def test_only_the_literal_1_overrides(
        self, tmp_path: Path, fake_fstype, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """A truthy-looking value is not an override: this one has to be exact."""
        fake_fstype("cifs")
        monkeypatch.setenv(ALLOW_NETWORK_FS_ENV, value)
        with pytest.raises(StorePathRefused):
            open_store(f"sqlite:///{tmp_path}/council.sqlite")

    def test_the_override_does_not_admit_a_dropbox_path(
        self, tmp_path: Path, fake_fstype, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Dropbox refusal is NOT overridable, and must stay that way.

        No spelling of "I know what I am doing" makes a file syncer safe for a
        WAL pair: the syncer uploads the two halves at different instants and
        then resolves a conflict between them.
        """
        fake_fstype("xfs")
        monkeypatch.setenv(ALLOW_NETWORK_FS_ENV, "1")
        target = tmp_path / "Dropbox" / "council.sqlite"
        with pytest.raises(StorePathRefused, match="Dropbox"):
            open_store(f"sqlite:///{target}")
        assert not target.parent.exists()

    def test_dropbox_is_refused_before_the_filesystem_is_even_probed(
        self, tmp_path: Path, fake_fstype, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked = fake_fstype("xfs")
        monkeypatch.setenv(ALLOW_NETWORK_FS_ENV, "1")
        with pytest.raises(StorePathRefused, match="Dropbox"):
            open_store(f"sqlite:///{tmp_path}/Dropbox/council.sqlite")
        assert asked == []


# ---------------------------------------------------------------------------
# An undeterminable type
# ---------------------------------------------------------------------------


class TestUndeterminableType:
    def test_an_unknown_type_is_admitted_with_a_warning(
        self, tmp_path: Path, fake_fstype, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Failing closed here would refuse good local disks on untested platforms."""
        fake_fstype(None)
        with caplog.at_level("WARNING"):
            store = open_store(f"sqlite:///{tmp_path}/council.sqlite")
        try:
            store.init_schema()
            assert store.schema_version() > 0
        finally:
            store.close()
        assert any(
            "could not determine the filesystem type" in record.message
            for record in caplog.records
        )

    def test_an_unrecognised_type_name_is_admitted_silently(
        self, tmp_path: Path, fake_fstype, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A type that is simply not on the list is a local disk we have not met."""
        fake_fstype("btrfs")
        with caplog.at_level("WARNING"):
            store = open_store(f"sqlite:///{tmp_path}/council.sqlite")
        try:
            store.init_schema()
        finally:
            store.close()
        assert not [
            r for r in caplog.records if "could not determine" in r.message
        ]

    def test_an_empty_answer_counts_as_unknown(
        self, tmp_path: Path, fake_fstype
    ) -> None:
        fake_fstype("   ")
        store = open_store(f"sqlite:///{tmp_path}/council.sqlite")
        try:
            store.init_schema()
        finally:
            store.close()


# ---------------------------------------------------------------------------
# The real probe, on the machine the test is running on
# ---------------------------------------------------------------------------


class TestTheRealProbe:
    """Nothing substituted: what the shipped probe says about a real path."""

    def test_it_answers_something_or_honestly_nothing(self, tmp_path: Path) -> None:
        answer = sqlite_module.filesystem_type_of(tmp_path)
        assert answer is None or (answer == answer.strip().lower() and answer)

    def test_a_tmp_path_store_opens_under_the_real_probe(self, tmp_path: Path) -> None:
        """If this fails, CI is running on a network filesystem -- which is the
        exact mistake the guard exists to catch, so it failing is correct."""
        store = open_store(f"sqlite:///{tmp_path}/real/council.sqlite")
        try:
            store.init_schema()
            assert store.schema_version() > 0
        finally:
            store.close()

    def test_the_nearest_existing_ancestor_of_a_deep_new_path(
        self, tmp_path: Path
    ) -> None:
        assert nearest_existing_ancestor(tmp_path / "a" / "b" / "c.sqlite") == tmp_path

    def test_the_nearest_existing_ancestor_of_an_existing_dir_is_itself(
        self, tmp_path: Path
    ) -> None:
        assert nearest_existing_ancestor(tmp_path) == tmp_path

    def test_an_in_memory_store_is_never_probed(self, fake_fstype) -> None:
        """There is no path, so there is no filesystem to ask about."""
        asked = fake_fstype("nfs")
        store = open_store("sqlite://")
        try:
            store.init_schema()
        finally:
            store.close()
        assert asked == []
