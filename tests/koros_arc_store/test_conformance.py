"""Tests for the koros_arc_store CAC conformance helpers (Responsibility 3).

Covers the two helpers and their pinned semantics:

  * ``module_id`` derives the underscore form MECHANICALLY from the canonical
    hyphen form (``id.replace("-", "_")``), never via a lookup table, and is an
    APP-ID-only helper — it is not applied to kind, subject or record_id.
  * ``check_contract_version`` treats ``contract_version`` as an INTEGER and
    compares by equality only: equal proceeds, ANY difference hard-fails with a
    ``ContractVersionMismatch`` naming BOTH versions. There is deliberately no
    minor/warn branch, and no test asserts one.
  * The gate applies on WRITE, at ``record_analysis``.

No network is used.
"""

from __future__ import annotations

import pytest

from kbutillib.koros_arc_store import (
    CONTRACT_VERSION,
    ContractVersionMismatch,
    KorosArcStore,
    RunDatabase,
    check_contract_version,
    module_id,
)
from kbutillib.koros_arc_store.conformance import CONTRACT_VERSION as MODULE_CV

from .test_run_database import make_record

# ── id normalisation ────────────────────────────────────────────────────────


class TestModuleId:
    def test_underscore_form_is_hyphen_replaced(self):
        assert module_id("kind-annotation-results-explorer-v1") == (
            "kind_annotation_results_explorer_v1"
        )
        assert module_id("kind-model-analysis-explorer-v1") == (
            "kind_model_analysis_explorer_v1"
        )

    def test_no_hyphen_is_returned_unchanged(self):
        # A single-token id has nothing to replace.
        assert module_id("koros") == "koros"
        assert module_id("already_underscored") == "already_underscored"

    def test_derivation_is_mechanical_not_a_lookup(self):
        # An id the helper has never "seen" still derives correctly, which is
        # only possible if the derivation is computed, not table-driven.
        made_up = "some-brand-new-app-nobody-registered-x99"
        assert module_id(made_up) == "some_brand_new_app_nobody_registered_x99"
        # Property: the result is exactly the hyphen->underscore substitution,
        # for an arbitrary id, with no other transformation.
        for raw in ("a-b-c", "x", "p-q", "one-2-three-4"):
            assert module_id(raw) == raw.replace("-", "_")

    def test_only_hyphens_change_case_and_other_chars_preserved(self):
        # Not lower-cased, not otherwise rewritten — only '-' -> '_'.
        assert module_id("Kind-App-V2") == "Kind_App_V2"


# ── contract-version gate (integer only) ────────────────────────────────────


class TestContractVersionGate:
    def test_current_contract_version_is_the_integer_one(self):
        assert CONTRACT_VERSION == 1
        assert isinstance(CONTRACT_VERSION, int)
        assert not isinstance(CONTRACT_VERSION, bool)
        assert MODULE_CV == 1

    def test_equal_versions_proceed(self):
        # Equal -> no raise, returns None.
        assert check_contract_version(1, 1) is None
        assert check_contract_version(CONTRACT_VERSION) is None

    def test_any_difference_hard_fails_naming_both_versions(self):
        with pytest.raises(ContractVersionMismatch) as exc:
            check_contract_version(found=2, expected=1)
        assert exc.value.expected == 1
        assert exc.value.found == 2
        # The message names BOTH versions so a consumer can report the gap.
        message = str(exc.value)
        assert "1" in message and "2" in message

    def test_a_lower_found_version_also_hard_fails(self):
        # ANY difference fails — there is no "found is older, warn and proceed".
        with pytest.raises(ContractVersionMismatch) as exc:
            check_contract_version(found=0, expected=1)
        assert exc.value.expected == 1
        assert exc.value.found == 0

    def test_mismatch_is_the_shared_base_error(self):
        # Consumers may catch by the shared base hierarchy.
        from kbutillib.koros_arc_store import KorosArcStoreError

        with pytest.raises(KorosArcStoreError):
            check_contract_version(found=5, expected=1)


# ── the gate applies on WRITE, at record_analysis ───────────────────────────


class TestGateAtRecordAnalysis:
    def _db(self, tmp_path):
        return RunDatabase(db_path=tmp_path / "runs.sqlite", enabled=True)

    def test_matching_version_writes_normally(self, tmp_path):
        db = self._db(tmp_path)
        db.record_analysis(None, None, make_record(contract_version=1))
        rows = db.list_analyses(None, None)
        assert len(rows) == 1
        assert rows[0].contract_version == 1

    def test_mismatched_version_hard_fails_on_write(self, tmp_path):
        db = self._db(tmp_path)
        with pytest.raises(ContractVersionMismatch) as exc:
            db.record_analysis(None, None, make_record(contract_version=2))
        assert exc.value.expected == 1
        assert exc.value.found == 2
        # It is a rejection, not a soft failure: nothing was stored and the
        # soft-fail counter did not move.
        assert db.failed_write_count == 0
        assert db.list_analyses(None, None) == []

    def test_gate_reaches_through_the_store_facade(self, tmp_path):
        store = KorosArcStore(runs_root=tmp_path, db_path=tmp_path / "runs.sqlite")
        store._db.enabled = True
        with pytest.raises(ContractVersionMismatch):
            store.record_analysis(None, None, make_record(contract_version=99))
