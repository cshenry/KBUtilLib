"""The CONFIGURED store identity has to reach the store on the REAL path.

KBUtilLib#93.  ``SqliteStore`` has carried a configured-vs-stored identity
refusal since the archive-identity task: ``require_store_identity`` compares
``self.configured_store_id`` against the identity row inside the database and
raises ``StoreIdentityMismatch`` naming both values.  That comparison was
correct and completely unreachable in production, because
``CouncilUtils.store()`` -- the only place anything opens a council store --
called ``open_store(url, **kwargs)`` and passed no ``store_id``.  So
``configured_store_id`` was ``None`` on every real path, the mismatch branch
was dead code, and a store that had been copied and then written to under
somebody else's declared id archived without a word.

Every test here therefore goes in through ``CouncilUtils`` and NEVER
constructs ``SqliteStore(store_id=...)`` itself.  Constructing the store
directly is precisely what made this gap invisible: it exercises the branch
while proving nothing about whether production reaches it.

The other three tests exist because the OBVIOUS fix is worse than the bug.
``store()`` uses the truthiness of ``**kwargs`` as its one-off-open flag, so
folding the id into ``kwargs`` makes every open a one-off: the store stops
being cached, ``_load_query_dirs`` stops being reached, and NEITHER raises.
Caching, the one-off bypass, and query-dir loading are pinned here for that
reason.

Nothing here makes a network call, spends a token or needs a pod.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

pytest.importorskip(
    "sqlalchemy",
    reason="the council store needs the `council` extra: pip install 'kbutillib[council]'",
)

from kbutillib.domains.ai.council.store.sqlite import (  # noqa: E402
    StoreIdentityMismatch,
    StoreIdentityMissing,
)
from kbutillib.domains.ai.council_utils import CouncilUtils  # noqa: E402

from . import engine_support as support  # noqa: E402

#: What the STORE will declare about itself.
STORED_ID = "poplar/council-1"

#: What the CONFIG will claim the store is.  Different on purpose.
CONFIGURED_ID = "laptop/council-copy"


def _config(tmp_path: Path, **extra: Any) -> Dict[str, Any]:
    """The smallest config that names a throwaway store.

    No endpoints and no profiles: nothing here submits or runs, and a seat
    this test does not use is a seat whose reachability check could fail for
    reasons that have nothing to do with identity.
    """
    config: Dict[str, Any] = {"store": f"sqlite:///{tmp_path}/council.sqlite"}
    config.update(extra)
    return config


def _util(tmp_path: Path, **extra: Any) -> CouncilUtils:
    """A facade over a real temp store.  Nothing is patched."""
    return CouncilUtils(
        council_config=_config(tmp_path, **extra),
        config_file=False,
        token_file=None,
        kbase_token_file=None,
    )


# ---------------------------------------------------------------------------
# 1. The refusal production never reached
# ---------------------------------------------------------------------------


class TestTheConfiguredIdReachesTheStore:
    """The mismatch branch, driven from the facade with no injected id."""

    def test_snapshot_refuses_naming_both_values(self, tmp_path: Path) -> None:
        """A store declaring one id, a config claiming another: refused.

        The test passes NO ``store_id`` anywhere.  The configured value
        arrives only because ``store()`` feeds it in, which is the whole
        point: before the fix this snapshot succeeded.
        """
        util = _util(tmp_path, store_id=CONFIGURED_ID)
        try:
            util.db_init()
            # The store's OWN record, written through the real assignment
            # path, disagrees with the config above.
            util.store().assign_store_identity(STORED_ID, declared_by="chenry")

            with pytest.raises(StoreIdentityMismatch) as excinfo:
                util.store().snapshot(tmp_path / "snap.sqlite")
        finally:
            util.close()

        message = str(excinfo.value)
        assert CONFIGURED_ID in message
        assert STORED_ID in message
        assert not (tmp_path / "snap.sqlite").exists()

    def test_export_refuses_naming_both_values(self, tmp_path: Path) -> None:
        """The same gate on the other identity-checking operation."""
        util = _util(tmp_path, store_id=CONFIGURED_ID)
        try:
            util.db_init()
            util.store().assign_store_identity(STORED_ID, declared_by="chenry")

            with pytest.raises(StoreIdentityMismatch) as excinfo:
                util.store().export(tmp_path / "export")
        finally:
            util.close()

        assert CONFIGURED_ID in str(excinfo.value)
        assert STORED_ID in str(excinfo.value)

    def test_the_store_the_facade_hands_back_carries_the_configured_id(
        self, tmp_path: Path
    ) -> None:
        """Directly: the facade's store knows what the config declared."""
        util = _util(tmp_path, store_id=CONFIGURED_ID)
        try:
            util.db_init()
            assert util.store().configured_store_id == CONFIGURED_ID
        finally:
            util.close()

    def test_a_matching_configured_id_passes(self, tmp_path: Path) -> None:
        """Agreement is not a refusal -- the gate is not just "always no"."""
        util = _util(tmp_path, store_id=STORED_ID)
        try:
            util.db_init()
            util.store().assign_store_identity(STORED_ID, declared_by="chenry")
            manifest = util.store().snapshot(tmp_path / "snap.sqlite")
        finally:
            util.close()

        assert manifest["store_id"] == STORED_ID

    def test_a_one_off_open_carries_the_id_too(self, tmp_path: Path) -> None:
        """``db migrate``'s open is not a hole in the gate.

        A one-off open (``check_version=False``) is the path ``db_migrate``
        uses.  It is uncached, but it is still a production open, so it gets
        the configured identity like any other.
        """
        util = _util(tmp_path, store_id=CONFIGURED_ID)
        try:
            util.db_init()
            one_off = util.store(check_version=False)
            try:
                assert one_off.configured_store_id == CONFIGURED_ID
            finally:
                one_off.close()
        finally:
            util.close()

    def test_an_explicit_store_id_argument_wins_over_the_config(
        self, tmp_path: Path
    ) -> None:
        """An explicit argument is a deliberate override, and it is honoured."""
        util = _util(tmp_path, store_id=CONFIGURED_ID)
        try:
            util.db_init()
            opened = util.store(store_id=STORED_ID)
            try:
                assert opened.configured_store_id == STORED_ID
            finally:
                opened.close()
        finally:
            util.close()


# ---------------------------------------------------------------------------
# 2. The two silent regressions the obvious fix would cause
# ---------------------------------------------------------------------------


class TestTheCacheAndTheOneOffBypassSurvive:
    """``store()``'s kwargs semantics are unchanged by the fix."""

    def test_the_store_is_still_cached(self, tmp_path: Path) -> None:
        util = _util(tmp_path, store_id=CONFIGURED_ID)
        try:
            util.db_init()
            assert util.store() is util.store()
        finally:
            util.close()

    def test_the_store_is_cached_with_no_configured_id_either(
        self, tmp_path: Path
    ) -> None:
        """``store_id=None`` must not read as "a kwarg was supplied"."""
        util = _util(tmp_path)
        try:
            util.db_init()
            assert util.store() is util.store()
            assert util.store().configured_store_id is None
        finally:
            util.close()

    def test_a_one_off_open_still_bypasses_the_cache(self, tmp_path: Path) -> None:
        util = _util(tmp_path, store_id=CONFIGURED_ID)
        try:
            util.db_init()
            one_off = util.store(check_version=False)
            try:
                assert one_off is not util.store()
            finally:
                one_off.close()
        finally:
            util.close()


class TestQueryDirsStillLoad:
    """The other silent casualty: a configured ``query_dirs`` is still read."""

    def test_a_spec_in_a_query_dir_is_registered_on_the_cached_store(
        self, tmp_path: Path
    ) -> None:
        directory = tmp_path / "queries"
        directory.mkdir(parents=True, exist_ok=True)
        spec = support.make_query("dir_query", "1").to_spec()
        (directory / "dir_query.yaml").write_text(
            yaml.safe_dump(spec, sort_keys=True), encoding="utf-8"
        )

        util = _util(
            tmp_path, store_id=CONFIGURED_ID, query_dirs=[str(directory)]
        )
        try:
            util.db_init()
            # Registered on the CACHED store, which is only reached when
            # `kwargs` is falsy -- i.e. only when the id was not folded in.
            assert util.query("dir_query@1").name == "dir_query"
            assert "dir_query@1" in util.query_dir_sources
            names = {row["name"] for row in util.queries()}
            assert "dir_query" in names
        finally:
            util.close()


# ---------------------------------------------------------------------------
# 3. The refusal that already worked, and still has to
# ---------------------------------------------------------------------------


class TestAbsenceIsUnchanged:
    """``store_id=None`` + a store with no identity is still refused."""

    def test_snapshot_still_refuses_a_store_with_no_identity(
        self, tmp_path: Path
    ) -> None:
        util = _util(tmp_path)
        try:
            util.db_init()
            assert util.store().configured_store_id is None
            assert util.store().store_identity() is None
            with pytest.raises(StoreIdentityMissing):
                util.store().snapshot(tmp_path / "snap.sqlite")
        finally:
            util.close()

    def test_a_configured_id_does_not_substitute_for_the_stored_record(
        self, tmp_path: Path
    ) -> None:
        """Configuring an id does NOT assign one: absence still refuses.

        This is the failure mode a "helpful" fix would introduce -- feeding
        the configured id in is not the same as declaring it, and nothing
        about #93 may turn config into an assignment.
        """
        util = _util(tmp_path, store_id=CONFIGURED_ID)
        try:
            util.db_init()
            assert util.store().store_identity() is None
            with pytest.raises(StoreIdentityMissing):
                util.store().snapshot(tmp_path / "snap.sqlite")
            # Still nothing written.
            assert util.store().store_identity() is None
        finally:
            util.close()


# ---------------------------------------------------------------------------
# 4. The OTHER half of #93: the loader used to drop the key
# ---------------------------------------------------------------------------


class TestTheConfigLoaderKeepsStoreId:
    """``store_id`` has to survive ``load_council_config``.

    Found while fixing #93 and part of the same gap: ``CouncilConfig`` carried
    the ``store_id`` field, but ``store_id`` was not in the loader's
    ``_TOP_KEYS``, and an unknown top-level key is dropped with nothing but a
    ``logger.warning``.  So the only way to get a configured id onto a config
    object was to construct ``CouncilConfig(store_id=...)`` in Python -- which
    the existing identity tests do, and production does not.  Feeding the id
    through ``store()`` would have changed nothing on its own.
    """

    def test_a_mapping_config_keeps_it(self, tmp_path: Path) -> None:
        util = _util(tmp_path, store_id=CONFIGURED_ID)
        assert util.config().store_id == CONFIGURED_ID

    def test_a_yaml_config_file_keeps_it(self, tmp_path: Path) -> None:
        path = tmp_path / "council.yaml"
        path.write_text(
            yaml.safe_dump(_config(tmp_path, store_id=CONFIGURED_ID)),
            encoding="utf-8",
        )
        util = CouncilUtils(
            council_config=path,
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )
        assert util.config().store_id == CONFIGURED_ID

    def test_the_loader_no_longer_warns_about_it(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The warning was the whole visible symptom.  It has to be gone."""
        from kbutillib.domains.ai.council.config import load_council_config

        with caplog.at_level("WARNING"):
            config = load_council_config(_config(tmp_path, store_id=CONFIGURED_ID))

        assert config.store_id == CONFIGURED_ID
        assert "store_id" not in caplog.text

    def test_a_blank_value_means_do_not_compare(self, tmp_path: Path) -> None:
        """An empty config value is "no configured identity", not an id of ""."""
        from kbutillib.domains.ai.council.config import load_council_config

        assert load_council_config(_config(tmp_path, store_id="")).store_id is None
        assert load_council_config(_config(tmp_path, store_id="  ")).store_id is None

    def test_it_is_still_absent_by_default(self, tmp_path: Path) -> None:
        """No automatic default.  There may never be one."""
        from kbutillib.domains.ai.council.config import load_council_config

        assert load_council_config(_config(tmp_path)).store_id is None

    def test_the_config_snapshot_carries_it(self, tmp_path: Path) -> None:
        """It is provenance, not a credential, so a job's snapshot keeps it."""
        util = _util(tmp_path, store_id=CONFIGURED_ID)
        assert util.config_snapshot()["store_id"] == CONFIGURED_ID
