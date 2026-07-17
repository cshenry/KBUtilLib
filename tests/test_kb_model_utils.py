"""Offline regression tests for the reconstruction / save-to-KBase fixes.

Every bug pinned here was fixed in commit 9368585 ("make reconstruction + model
saving work outside an SDK container").  None of them had a test, which is why
they survived: ``kb_build_metabolic_models`` had never run outside a KBase SDK
container, so the dead calls were only reachable on a path nobody exercised.

The bugs, and the test class that pins each:

1. ``process_genome_list`` was called but never defined      -> TestProcessGenomeList
2. ``anno_client(native_python_api)`` param ignored          -> TestAnnoClientNativePythonApi
3. ontology dictionaries assumed at ``module_dir/data``      -> TestOntologyDataDir
4. ``self.FBAModel = None`` clobbered the real class binding -> TestFBAModelBinding
5. ``self.provenance()`` instead of ``self.get_provenance()``-> TestProvenanceOnSavePaths

Everything here runs OFFLINE.  The workspace client is mocked; no test needs a
KBase token, a callback URL, or a network call.  Construction of the utility
classes is real (it loads the ModelSEED biochemistry database), so the instance
fixtures are session-scoped to pay that cost once.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

# ── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def recon():
    """A real MSReconstructionUtils instance, constructed offline.

    Session-scoped: __init__ loads the ModelSEED biochemistry database, which is
    far too slow to repeat per test.  Tests MUST NOT mutate this instance; use
    ``patch.object`` so changes are undone.
    """
    from kbutillib.ms_reconstruction_utils import MSReconstructionUtils

    return MSReconstructionUtils(
        config_file=False,
        token_file=None,
        kbase_token_file=None,
        token="fake-kbase-token-for-tests",
    )


@pytest.fixture(scope="session")
def model_utils():
    """A real KBModelUtils instance, constructed offline."""
    from kbutillib.kb_model_utils import KBModelUtils

    return KBModelUtils(
        config_file=False,
        token_file=None,
        kbase_token_file=None,
        token="fake-kbase-token-for-tests",
    )


@pytest.fixture
def ontology_dir_factory(tmp_path):
    """Build a directory containing a fake SSO_dictionary.json.

    ``ontology_data_dir`` probes for that exact filename, so an empty marker file
    is enough to make a candidate directory "resolve".
    """

    def _make(name: str) -> str:
        d = tmp_path / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SSO_dictionary.json").write_text("{}")
        return str(d)

    return _make


@pytest.fixture
def no_ontology_env():
    """Clear KBUTILLIB_ONTOLOGY_DATA_DIR for the duration of a test.

    Without this the suite is not hermetic: a developer with the variable
    exported would silently resolve candidate 1 in tests that mean to exercise
    candidates 2-4.
    """
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("KBUTILLIB_ONTOLOGY_DATA_DIR", None)
        yield


# ── Bug 1: process_genome_list was called but never defined ───────────────


class TestProcessGenomeList:
    """``kb_build_metabolic_models`` called ``self.process_genome_list(...)``.

    It was defined nowhere, so the call raised AttributeError before the method
    did any work -- reconstruction could not start at all.
    """

    def test_method_exists(self, recon):
        """The regression itself: the attribute must exist and be callable."""
        assert hasattr(recon, "process_genome_list"), (
            "process_genome_list is missing -- kb_build_metabolic_models calls it "
            "and will raise AttributeError before doing any work"
        )
        assert callable(recon.process_genome_list)

    def test_bare_id_is_qualified_with_workspace(self, recon):
        """A bare genome ID gets the workspace prefixed onto it."""
        assert recon.process_genome_list(["562.55367"], "265353") == [
            "265353/562.55367"
        ]

    def test_ws_obj_ref_passes_through(self, recon):
        """An existing ws/obj ref is not re-qualified."""
        assert recon.process_genome_list(["265353/562.55367"], "999") == [
            "265353/562.55367"
        ]

    def test_versioned_ref_passes_through(self, recon):
        """A ws/obj/ver ref survives intact -- version pinning must not be lost.

        This matters concretely: the 18 "missing" 2022 genomes are only
        addressable by their v1 ref, because v2 overwrote them in place.
        """
        assert recon.process_genome_list(["265353/562.55367/1"], "999") == [
            "265353/562.55367/1"
        ]

    def test_four_part_ref_is_rejected(self, recon):
        """Anything deeper than ws/obj/ver is malformed and dropped locally.

        Rejecting here beats sending it to the workspace API to fail there.
        """
        assert recon.process_genome_list(["a/b/c/d"], "265353") == []

    def test_empty_string_is_filtered(self, recon):
        assert recon.process_genome_list([""], "265353") == []

    def test_none_entry_is_filtered_not_crashed(self, recon):
        """A None entry must be filtered, not raise on str(None)."""
        assert recon.process_genome_list([None], "265353") == []

    def test_non_string_id_is_coerced(self, recon):
        """Numeric IDs are coerced rather than raising on .split().

        pandas hands back numpy/int types from a genome column, so this is a
        real input shape, not a hypothetical.
        """
        assert recon.process_genome_list([12345], "265353") == ["265353/12345"]

    def test_empty_list_returns_empty(self, recon):
        assert recon.process_genome_list([], "265353") == []

    def test_none_list_returns_empty(self, recon):
        assert recon.process_genome_list(None, "265353") == []

    def test_mixed_batch_preserves_order_and_filters(self, recon):
        """A realistic mixed batch: qualify, pass through, and drop the junk."""
        result = recon.process_genome_list(
            ["562.55367", "265353/562.55380/1", "", "a/b/c/d", "265353/562.1"],
            "265353",
        )
        assert result == [
            "265353/562.55367",
            "265353/562.55380/1",
            "265353/562.1",
        ]

    def test_returns_strings_not_objects(self, recon):
        """Callers hand each result to get_msgenome_from_ontology, which resolves
        the ref itself -- so this must return ref STRINGS, unlike its sibling
        process_media_list, which returns fetched media OBJECTS."""
        result = recon.process_genome_list(["562.55367"], "265353")
        assert all(isinstance(r, str) for r in result)


class TestProcessMediaList:
    """The sibling of process_genome_list; its normalization is the model the
    genome version was written to mirror.  ``get_media`` is mocked -- the
    normalization logic is what is under test, not the workspace fetch."""

    def test_bare_id_qualified_and_fetched(self, recon):
        with patch.object(recon, "get_media", side_effect=lambda ref, _: ref) as gm:
            result = recon.process_media_list(["Carbon-D-Glucose"], "KBaseMedia/Complete", "265353")
        assert result == ["265353/Carbon-D-Glucose"]
        gm.assert_called_once_with("265353/Carbon-D-Glucose", None)

    def test_empty_list_falls_back_to_default_media(self, recon):
        """An empty media list must fall back to the default, not fetch nothing."""
        with patch.object(recon, "get_media", side_effect=lambda ref, _: ref):
            result = recon.process_media_list([], "KBaseMedia/Complete", "265353")
        assert result == ["KBaseMedia/Complete"]

    def test_first_empty_ref_becomes_default(self, recon):
        with patch.object(recon, "get_media", side_effect=lambda ref, _: ref):
            result = recon.process_media_list([""], "KBaseMedia/Complete", "265353")
        assert result == ["KBaseMedia/Complete"]

    def test_four_part_ref_rejected(self, recon):
        """Malformed refs are dropped, so the fallback-to-default kicks in."""
        with patch.object(recon, "get_media", side_effect=lambda ref, _: ref):
            result = recon.process_media_list(["a/b/c/d"], "KBaseMedia/Complete", "265353")
        assert result == ["KBaseMedia/Complete"]

    def test_returns_objects_not_strings(self, recon):
        """Unlike process_genome_list, this returns fetched media OBJECTS."""
        sentinel = object()
        with patch.object(recon, "get_media", return_value=sentinel):
            result = recon.process_media_list(["Carbon-Pyruvic-Acid"], "KBaseMedia/Complete", "265353")
        assert result == [sentinel]


# ── Bug 2: anno_client(native_python_api) was threaded through and ignored ─


class TestAnnoClientNativePythonApi:
    """The parameter was accepted at three call sites and then dropped, so the
    advertised native path did not exist: any non-SDK caller fell through to the
    callback branch and hit "Either set callback URL".
    """

    def test_native_returns_self(self, recon):
        """``self`` is the drop-in: KBAnnotationUtils implements
        get_annotation_ontology_events natively."""
        assert recon.anno_client(native_python_api=True) is recon

    def test_native_works_with_no_callback_url(self, recon):
        """The point of the fix: the native path must not require a callback URL.

        This is the exact failure that made reconstruction impossible outside an
        SDK container.
        """
        with patch.object(recon, "_callback_url", None):
            assert recon.anno_client(native_python_api=True) is recon

    def test_native_client_implements_the_only_method_callers_use(self, recon):
        """`self` is only a valid substitute if it actually answers the call the
        callback proxy was used for."""
        client = recon.anno_client(native_python_api=True)
        assert hasattr(client, "get_annotation_ontology_events")
        assert callable(client.get_annotation_ontology_events)

    def test_default_is_callback_path(self, recon):
        """Default must stay False: inside an SDK container the callback client
        is still the right client, so the fix must not flip the default."""
        with patch.object(recon, "_callback_url", None):
            with pytest.raises(ValueError, match="callback URL"):
                recon.anno_client()

    def test_explicit_false_is_callback_path(self, recon):
        with patch.object(recon, "_callback_url", None):
            with pytest.raises(ValueError, match="callback URL"):
                recon.anno_client(native_python_api=False)


# ── Bug 3: ontology dictionaries assumed to live at module_dir/data ───────


class TestOntologyDataDir:
    """The dictionaries (~67MB) ship with cb_annotation_ontology_api and are
    deliberately NOT vendored, so the location is resolved, not assumed.

    Resolution order: $KBUTILLIB_ONTOLOGY_DATA_DIR -> repo data/ -> sibling
    checkout -> raise with an actionable message.

    Every test patches ``module_dir`` into tmp_path and clears the env var.
    That is not ceremony: this developer machine HAS real sibling checkouts of
    cb_annotation_ontology_api, so an unpatched test would resolve candidate 3
    here and fail on a machine without them.
    """

    def test_env_var_wins(self, recon, ontology_dir_factory, no_ontology_env, tmp_path):
        env_dir = ontology_dir_factory("from_env")
        repo_dir = ontology_dir_factory("repo/data")
        os.environ["KBUTILLIB_ONTOLOGY_DATA_DIR"] = env_dir
        with patch.object(
            type(recon), "module_dir", new_callable=PropertyMock,
            return_value=str(tmp_path / "repo"),
        ):
            assert recon.ontology_data_dir == env_dir, (
                "env var must take precedence over the repo data/ directory"
            )
        assert repo_dir  # the lower-priority candidate existed and still lost

    def test_env_var_without_dictionary_falls_through(
        self, recon, ontology_dir_factory, no_ontology_env, tmp_path
    ):
        """An env var pointing at a directory with no SSO_dictionary.json must
        fall through to the next candidate, not be returned blindly."""
        empty = tmp_path / "empty_env_dir"
        empty.mkdir()
        os.environ["KBUTILLIB_ONTOLOGY_DATA_DIR"] = str(empty)
        ontology_dir_factory("repo/data")
        with patch.object(
            type(recon), "module_dir", new_callable=PropertyMock,
            return_value=str(tmp_path / "repo"),
        ):
            assert recon.ontology_data_dir == str(tmp_path / "repo" / "data")

    def test_repo_data_dir_is_second(
        self, recon, ontology_dir_factory, no_ontology_env, tmp_path
    ):
        ontology_dir_factory("repo/data")
        ontology_dir_factory("cb_annotation_ontology_api/data")
        with patch.object(
            type(recon), "module_dir", new_callable=PropertyMock,
            return_value=str(tmp_path / "repo"),
        ):
            assert recon.ontology_data_dir == str(tmp_path / "repo" / "data"), (
                "repo data/ must be preferred over a sibling checkout"
            )

    def test_sibling_cb_annotation_checkout_is_third(
        self, recon, ontology_dir_factory, no_ontology_env, tmp_path
    ):
        (tmp_path / "repo").mkdir(exist_ok=True)
        sibling = ontology_dir_factory("cb_annotation_ontology_api/data")
        with patch.object(
            type(recon), "module_dir", new_callable=PropertyMock,
            return_value=str(tmp_path / "repo"),
        ):
            assert recon.ontology_data_dir == sibling

    def test_sibling_modelseedrecon_checkout_is_last_resort(
        self, recon, ontology_dir_factory, no_ontology_env, tmp_path
    ):
        (tmp_path / "repo").mkdir(exist_ok=True)
        sibling = ontology_dir_factory("KB-ModelSEEDReconstruction/data")
        with patch.object(
            type(recon), "module_dir", new_callable=PropertyMock,
            return_value=str(tmp_path / "repo"),
        ):
            assert recon.ontology_data_dir == sibling

    def test_raises_actionable_error_when_nothing_found(
        self, recon, no_ontology_env, tmp_path
    ):
        """The fix's stated goal: name the repo and the env var, rather than let
        modelseedpy raise a bare FileNotFoundError from deep inside itself."""
        (tmp_path / "repo").mkdir(exist_ok=True)
        with patch.object(
            type(recon), "module_dir", new_callable=PropertyMock,
            return_value=str(tmp_path / "repo"),
        ):
            with pytest.raises(FileNotFoundError) as exc:
                _ = recon.ontology_data_dir
        message = str(exc.value)
        assert "cb_annotation_ontology_api" in message, (
            "error must name the repo that ships the dictionaries"
        )
        assert "KBUTILLIB_ONTOLOGY_DATA_DIR" in message, (
            "error must name the env var that fixes it"
        )
        assert "SSO_dictionary.json" in message
        assert "Looked in" in message, "error must list the candidates tried"


# ── Bug 4: self.FBAModel = None clobbered the real class binding ──────────


class TestFBAModelBinding:
    """MSReconstructionUtils.__init__ used to reset self.FBAModel to None AFTER
    KBModelUtils.__init__ had bound the real cobrakbase class.

    save_model does ``isinstance(mdlutl.model, self.FBAModel)``, and isinstance
    against None raises "arg 2 must be a type" -- so every save through the
    callback-free core died.  The kb_* SDK wrappers masked it by lazily calling
    _kbase_imports(), which rebinds it.
    """

    def test_fbamodel_is_bound_after_init(self, recon):
        assert recon.FBAModel is not None, (
            "FBAModel was clobbered to None in __init__; save_model's isinstance "
            "check will raise 'arg 2 must be a type'"
        )

    def test_fbamodel_is_a_type(self, recon):
        """The precise precondition isinstance() needs."""
        assert isinstance(recon.FBAModel, type)

    def test_isinstance_check_does_not_raise(self, recon):
        """Reproduces save_model's exact call shape.  Under the bug this raises
        TypeError; the assertion on the result is incidental."""
        try:
            result = isinstance(object(), recon.FBAModel)
        except TypeError as e:
            pytest.fail(
                f"isinstance(x, self.FBAModel) raised -- FBAModel is not a type: {e}"
            )
        assert result is False

    def test_subclass_binding_matches_parent(self, recon, model_utils):
        """The subclass must not diverge from the parent's binding."""
        assert recon.FBAModel is model_utils.FBAModel

    def test_binding_is_the_real_cobrakbase_class(self, recon):
        """Guards against a future 'fix' that binds a placeholder to satisfy the
        isinstance check without being the real type."""
        from cobrakbase.core.kbasefba import FBAModel

        assert recon.FBAModel is FBAModel

    def test_kbase_imports_is_idempotent(self, recon):
        """_kbase_imports() rebinds FBAModel; it must agree with __init__ rather
        than mask a difference.  This is what hid the bug from SDK callers."""
        before = recon.FBAModel
        recon._kbase_imports()
        assert recon.FBAModel is before


# ── Bug 5: self.provenance() instead of self.get_provenance() ─────────────


class TestProvenanceOnSavePaths:
    """``self.provenance`` does not exist; the method is ``get_provenance()``.

    The typo was in save_model, save_phenotypeset, save_solution_as_fba and
    add_annotations_to_object -- i.e. every save-to-KBase path -- so each raised
    AttributeError at the moment it tried to build its save params.

    NOTE: the commit message calls the fourth site "save_genome"; the method is
    actually ``add_annotations_to_object`` in kb_genome_utils.
    """

    def test_provenance_attribute_does_not_exist(self, recon):
        """The root fact that made every self.provenance() call a bug.

        If someone ever adds a real provenance() method this test fails loudly,
        which is the correct prompt to revisit the call sites.
        """
        assert not hasattr(recon, "provenance"), (
            "A 'provenance' attribute now exists -- the save paths deliberately "
            "call get_provenance(); reconcile the two before changing this test"
        )

    def test_get_provenance_returns_a_list_of_dicts(self, recon):
        prov = recon.get_provenance()
        assert isinstance(prov, list) and prov
        assert isinstance(prov[0], dict)

    def test_save_model_sends_provenance(self, recon):
        """save_model must reach ws_client().save_objects with real provenance."""
        mdlutl = MagicMock()
        mdlutl.model.reactions = []
        mdlutl.wsid = "562.55367_2026p"
        mdlutl.model.get_data.return_value = {"id": "562.55367_2026p"}

        ws_client = MagicMock()
        with patch.object(recon, "ws_client", return_value=ws_client), \
             patch.object(recon, "set_ws"), \
             patch.object(recon, "create_ref", return_value="265353/1/1"), \
             patch.object(recon, "CobraModelConverter") as converter:
            converter.return_value.build.return_value.get_data.return_value = {
                "id": "562.55367_2026p"
            }
            recon.save_model(mdlutl, workspace="265353", objid="562.55367_2026p")

        ws_client.save_objects.assert_called_once()
        params = ws_client.save_objects.call_args[0][0]
        provenance = params["objects"][0]["provenance"]
        assert provenance == recon.get_provenance()
        assert isinstance(provenance, list) and provenance

    def test_save_phenotypeset_sends_provenance(self, recon):
        ws_client = MagicMock()
        with patch.object(recon, "ws_client", return_value=ws_client), \
             patch.object(recon, "set_ws"), \
             patch.object(recon, "create_ref", return_value="265353/1/1"):
            recon.save_phenotypeset({"id": "pheno"}, "265353", "biolog_set")

        ws_client.save_objects.assert_called_once()
        params = ws_client.save_objects.call_args[0][0]
        assert params["objects"][0]["provenance"] == recon.get_provenance()
        assert params["objects"][0]["type"] == "KBasePhenotypes.PhenotypeSet"

    def test_save_solution_as_fba_sends_provenance(self, recon):
        fba = MagicMock()
        fba.generate_kbase_data.return_value = {"id": "fba1"}
        media = MagicMock()
        media.info.reference = "265353/media/1"

        ws_client = MagicMock()
        with patch.object(recon, "ws_client", return_value=ws_client), \
             patch.object(recon, "set_ws"), \
             patch.object(recon, "create_ref", return_value="265353/1/1"), \
             patch.object(recon, "MSFBA", MagicMock):
            recon.save_solution_as_fba(
                fba, MagicMock(), media, "fba1", workspace="265353"
            )

        ws_client.save_objects.assert_called_once()
        params = ws_client.save_objects.call_args[0][0]
        assert params["objects"][0]["provenance"] == recon.get_provenance()
        assert params["objects"][0]["type"] == "KBaseFBA.FBA"

    def test_add_annotations_to_object_sends_provenance(self):
        """The kb_genome_utils save path -- the fourth site the fix touched.

        ``anno_client`` is patched with create=True because KBGenomeUtils does not
        define it (see TestKBGenomeUtilsAnnoClientGap).  That gap is a separate,
        still-open bug; this test isolates the provenance payload, which is what
        the fix actually changed.
        """
        from kbutillib.kb_genome_utils import KBGenomeUtils

        g = KBGenomeUtils(
            config_file=False,
            token_file=None,
            kbase_token_file=None,
            token="fake-kbase-token-for-tests",
        )
        anno_client = MagicMock()
        anno_client.add_annotation_ontology_events.return_value = {
            "output_ref": "265353/1/1"
        }
        g.object_info_hash = {"265353/g/1": [0, "my_genome"]}

        with patch.object(g, "anno_client", create=True, return_value=anno_client):
            g.add_annotations_to_object(
                "265353/g/1",
                ".RAST",
                {"gene1": {"SSO": {"SSO:000001": {"name": "thing"}}}},
            )

        anno_client.add_annotation_ontology_events.assert_called_once()
        payload = anno_client.add_annotation_ontology_events.call_args[0][0]
        assert payload["provenance"] == g.get_provenance()
        assert isinstance(payload["provenance"], list) and payload["provenance"]


# ── Still-open: add_annotations_to_object cannot reach its own client ─────


class TestKBGenomeUtilsAnnoClientGap:
    """KNOWN BUG, not fixed by 9368585 -- pinned here so it is not lost.

    ``add_annotations_to_object`` builds its payload (with the now-correct
    get_provenance()) and then calls ``self.anno_client()``.  But anno_client is
    defined on KBCallbackUtils, and KBGenomeUtils extends KBWSUtils -- a sibling
    branch.  The attribute exists on no KBGenomeUtils instance, and the facade
    does not compose it in either (kbu.genome._delegate is a plain KBGenomeUtils).

    So the fix moved the failure from line 236 to line 253: the method still
    cannot complete on any path.  Fixing it is a design call -- either
    KBGenomeUtils inherits KBCallbackUtils, or the method moves to a class that
    has the client -- so it is deliberately left to Chris rather than guessed at.
    """

    def test_anno_client_is_missing_on_kb_genome_utils(self):
        """Documents the gap as it stands today.  If this starts failing, the
        composition changed and the xfail below should flip -- delete both."""
        from kbutillib.kb_genome_utils import KBGenomeUtils

        assert not hasattr(KBGenomeUtils, "anno_client")

    @pytest.mark.xfail(
        strict=True,
        reason="KBGenomeUtils lacks anno_client (defined on sibling KBCallbackUtils); "
        "add_annotations_to_object raises AttributeError at the call. Remove this "
        "marker once the composition is fixed.",
    )
    def test_add_annotations_to_object_completes_unmocked_client(self):
        """The behavior we actually want: the method reaches its client on its own.

        Only the workspace side is mocked -- anno_client is NOT patched in, which
        is the whole point.
        """
        from kbutillib.kb_genome_utils import KBGenomeUtils

        g = KBGenomeUtils(
            config_file=False,
            token_file=None,
            kbase_token_file=None,
            token="fake-kbase-token-for-tests",
        )
        g.object_info_hash = {"265353/g/1": [0, "my_genome"]}
        g.add_annotations_to_object(
            "265353/g/1", ".RAST", {"gene1": {"SSO": {"SSO:000001": {"name": "thing"}}}}
        )


# ── Still-open: ontology_data_dir is unreachable from its own class ───────


class TestOntologyDataDirClassGap:
    """KNOWN BUG, not fixed by 9368585 -- same shape as the anno_client gap.

    ``ontology_data_dir`` is defined on KBModelUtils and reads ``self.module_dir``,
    but module_dir is a property on the MSReconstructionUtils SUBCLASS only.  So on
    a bare KBModelUtils the property raises AttributeError('module_dir') instead of
    the actionable FileNotFoundError the fix was written to produce.

    Reconstruction runs through MSReconstructionUtils, so this is latent rather
    than live -- but get_msgenome_from_ontology is itself a KBModelUtils method,
    so any KBModelUtils-only caller hits it.
    """

    @pytest.mark.xfail(
        strict=True,
        reason="module_dir is defined only on MSReconstructionUtils, so KBModelUtils "
        "raises AttributeError before the actionable FileNotFoundError can fire. "
        "Remove this marker once module_dir moves to a shared base.",
    )
    def test_kb_model_utils_raises_actionable_error_not_attribute_error(
        self, model_utils, no_ontology_env
    ):
        with pytest.raises(FileNotFoundError):
            _ = model_utils.ontology_data_dir
