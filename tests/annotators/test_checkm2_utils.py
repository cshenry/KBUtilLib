"""Offline unit tests for checkm2_utils.py, plus one real-dependency guard.

Test strategy
-------------
All Docker/subprocess-dependent code paths are exercised with
``subprocess.run`` mocked out, except for ``TestRealDockerGuard`` at the
bottom, which is deliberately the ONE test in this file that invokes the
real image -- it SKIPS (never errors, never silently passes) whenever
docker or ``checkm2.docker_image`` is unavailable in the current
environment. That skip is the point: it is what keeps the wrapper's Docker
boundary from going 100% unverified.

Directory placement
--------------------
``CheckM2Utils`` itself lives at ``domains/genome/checkm2_utils.py`` (a
sibling of ``skani_utils.py``, not inside ``domains/genome/annotation/``),
but its *test* is placed in ``tests/annotators/`` alongside
``test_bakta_utils.py`` / ``test_kofamscan_utils.py`` / ``test_prokka_utils.py``
rather than in ``tests/domains/`` -- CheckM2Utils follows the Dockerised
tool-wrapper pattern (``ToolUnavailableError``, ``docker image inspect``
probe, ``--user``/``--network none`` argv) documented and tested there,
whereas ``tests/domains/test_genome_skani_hardening.py`` covers
``SKANIUtils``, a native-binary wrapper that shares neither the exception
type nor the Docker argv shape. Grouping by pattern (not by the source
module's directory) keeps this test next to the fixtures/conventions it
actually reuses.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kbutillib.domains.genome.annotation.annotator_utils import ToolUnavailableError
from kbutillib.domains.genome.checkm2_utils import CheckM2Utils, _parse_quality_report

# ---------------------------------------------------------------------------
# Fixtures: two real quality_report.tsv rows, verbatim from production runs.
# ---------------------------------------------------------------------------

_HEADER = (
    "Name\tCompleteness\tContamination\tCompleteness_Model_Used\t"
    "Translation_Table_Used\tCoding_Density\tContig_N50\tAverage_Gene_Length\t"
    "Genome_Size\tGC_Content\tTotal_Coding_Sequences\tTotal_Contigs\t"
    "Max_Contig_Length\tAdditional_Notes"
)

_ROW_HIGH_CONFIDENCE = (
    "GB_GCA_002694305.1\t99.96\t1.56\tNeural Network (Specific Model)\t11\t"
    "0.886\t39667\t311.46309095935857\t3810898\t0.41\t3617\t168\t109114\tNone"
)

_ROW_LOW_CONFIDENCE = (
    "1663.201\t59.97\t0.0\tNeural Network (Specific Model)\t11\t0.909\t665875\t"
    "336.15819209039546\t2549154\t0.63\t2301\t9\t833406\t"
    "Low confidence prediction - substantial (25%) disagreement between "
    "completeness prediction models"
)


def _make_utils(**kwargs: Any) -> CheckM2Utils:
    """Create CheckM2Utils with no filesystem config discovery."""
    return CheckM2Utils(
        config_file=False,
        token_file=None,
        kbase_token_file=None,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# check_availability()
# ---------------------------------------------------------------------------


class TestCheckAvailability:
    def test_true_when_image_present(self):
        cu = _make_utils()
        cu._docker_image = "kbutillib/checkm2:1.1.0"
        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            assert cu.check_availability() is True

    def test_false_when_image_absent_but_configured(self):
        cu = _make_utils()
        cu._docker_image = "kbutillib/checkm2:1.1.0"
        with patch("subprocess.run", return_value=MagicMock(returncode=1)):
            assert cu.check_availability() is False

    def test_false_when_docker_image_unconfigured(self):
        cu = _make_utils()
        assert cu._docker_image == ""
        assert cu.check_availability() is False

    def test_false_when_docker_binary_missing(self):
        cu = _make_utils()
        cu._docker_image = "kbutillib/checkm2:1.1.0"
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert cu.check_availability() is False

    def test_false_on_timeout(self):
        cu = _make_utils()
        cu._docker_image = "kbutillib/checkm2:1.1.0"
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=15),
        ):
            assert cu.check_availability() is False


class TestRequireAvailable:
    def test_get_version_raises_tool_unavailable_when_unconfigured(self):
        cu = _make_utils()
        with pytest.raises(ToolUnavailableError):
            cu.get_version()

    def test_predict_raises_tool_unavailable_when_unconfigured(self, tmp_path):
        cu = _make_utils()
        with pytest.raises(ToolUnavailableError):
            cu.predict(
                genome_paths=[tmp_path / "g1.fna"],
                db_path=tmp_path / "db" / "uniref100.KO.1.dmnd",
                outdir=tmp_path / "out",
                threads=1,
                extension=".fna",
            )


# ---------------------------------------------------------------------------
# No hardcoded image reference anywhere in the module.
# ---------------------------------------------------------------------------


class TestImageIsConfigurationNotConstant:
    def test_default_docker_image_is_empty(self):
        """With no config, _docker_image resolves to "" -- never a baked-in tag."""
        cu = _make_utils()
        assert cu._docker_image == ""

    def test_docker_image_comes_from_config_key(self):
        cu = CheckM2Utils(
            config={"checkm2": {"docker_image": "example.org/checkm2:pinned"}},
            token_file=None,
            kbase_token_file=None,
        )
        assert cu._docker_image == "example.org/checkm2:pinned"


# ---------------------------------------------------------------------------
# get_version() — argv shape + version string parsing
# ---------------------------------------------------------------------------


class TestGetVersion:
    def test_parses_bare_version_string(self):
        cu = _make_utils()
        cu._docker_image = "kbutillib/checkm2:1.1.0"
        completed = MagicMock(returncode=0, stdout="1.1.0\n", stderr="")
        with patch("subprocess.run", return_value=completed) as mock_run:
            version = cu.get_version()

        assert version == "1.1.0"
        argv = mock_run.call_args[0][0]
        assert argv[:2] == ["docker", "run"]
        assert "--entrypoint" in argv
        assert argv[argv.index("--entrypoint") + 1] == "micromamba"
        assert cu._docker_image in argv
        # Inner command: `micromamba run -n checkm2 checkm2 --version`.
        image_idx = argv.index(cu._docker_image)
        assert argv[image_idx + 1 :] == ["run", "-n", "checkm2", "checkm2", "--version"]

    def test_raises_called_process_error_on_nonzero_exit(self):
        cu = _make_utils()
        cu._docker_image = "kbutillib/checkm2:1.1.0"
        completed = MagicMock(returncode=1, stdout="", stderr="boom")
        with patch.object(cu, "check_availability", return_value=True), patch(
            "subprocess.run", return_value=completed
        ):
            with pytest.raises(subprocess.CalledProcessError):
                cu.get_version()


# ---------------------------------------------------------------------------
# _run_checkm2_predict — argv construction (the "docker run" shape)
# ---------------------------------------------------------------------------


class TestPredictArgvConstruction:
    def _fake_completed(self, returncode=0, stdout="", stderr=""):
        return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)

    def test_argv_sets_writable_home_for_micromamba(self, tmp_path):
        """``--user`` without a writable HOME makes micromamba abort.

        REGRESSION GUARD for a blocker measured on poplar 2026-09-02. The
        image's default user is ``mambauser``; overriding it with ``--user``
        leaves ``HOME=/``, which the invoking uid cannot write, so micromamba
        dies before CheckM2 starts::

            critical libmamba filesystem error: directory iterator cannot open
            directory: No such file or directory [/.cache/mamba/proc]

        KBDLCheckM2 failed 100% of the time on poplar until ``-e HOME=/work``
        was added. ``/work`` is the bind-mounted scratch dir owned by that uid.
        The mocked argv tests could not catch this because none of them runs a
        real container -- hence this explicit assertion on the argv itself.
        """
        cu = _make_utils()
        cu._docker_image = "kbutillib/checkm2:1.1.0"
        work = tmp_path / "work"
        work.mkdir()
        db_dir = tmp_path / "db"
        db_dir.mkdir()
        db_file = db_dir / "uniref100.KO.1.dmnd"
        db_file.write_text("fake-db")

        with patch("subprocess.run", return_value=self._fake_completed()) as mock_run:
            cu._run_checkm2_predict(
                work=work,
                genome_names=["genome1.fna"],
                resolved_db=db_file,
                threads=4,
                extension=".fna",
                remove_intermediates=True,
            )

        argv = mock_run.call_args[0][0]
        env_vals = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]

        assert "HOME=/work" in env_vals, (
            "HOME must be set to the writable bind-mounted /work; without it "
            "micromamba aborts and every CheckM2 job fails"
        )
        assert "XDG_CACHE_HOME=/work/.cache" in env_vals

        # The HOME value must be a path that is actually writable in the
        # container -- i.e. the /work bind mount, not / or /db (read-only).
        home_val = next(v for v in env_vals if v.startswith("HOME="))
        home_path = home_val.split("=", 1)[1]
        assert home_path.startswith("/work"), home_val

    def test_argv_has_user_network_none_and_ro_db_mount(self, tmp_path):
        cu = _make_utils()
        cu._docker_image = "kbutillib/checkm2:1.1.0"
        work = tmp_path / "work"
        work.mkdir()
        db_dir = tmp_path / "db"
        db_dir.mkdir()
        db_file = db_dir / "uniref100.KO.1.dmnd"
        db_file.write_text("fake-db")

        with patch("subprocess.run", return_value=self._fake_completed()) as mock_run:
            cu._run_checkm2_predict(
                work=work,
                genome_names=["genome1.fna", "genome2.fna"],
                resolved_db=db_file,
                threads=4,
                extension=".fna",
                remove_intermediates=True,
            )

        argv = mock_run.call_args[0][0]
        assert argv[:2] == ["docker", "run"]

        assert "--user" in argv
        user_val = argv[argv.index("--user") + 1]
        assert ":" in user_val

        assert "--network" in argv
        assert argv[argv.index("--network") + 1] == "none"

        # Database directory mounted read-only.
        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
        assert any(m == f"{db_dir}:/db:ro" for m in mounts)
        assert any(m == f"{work}:/work" for m in mounts)

        # Entrypoint overridden to micromamba, image reference used.
        assert "--entrypoint" in argv
        assert argv[argv.index("--entrypoint") + 1] == "micromamba"
        assert cu._docker_image in argv

        # --input receives the full list of genome paths (not one at a time).
        assert "--input" in argv
        input_idx = argv.index("--input")
        assert argv[input_idx + 1] == "/work/genome1.fna"
        assert argv[input_idx + 2] == "/work/genome2.fna"

        assert "--threads" in argv
        assert argv[argv.index("--threads") + 1] == "4"

        assert "--extension" in argv
        assert argv[argv.index("--extension") + 1] == ".fna"

        assert "--database_path" in argv
        assert argv[argv.index("--database_path") + 1] == "/db/uniref100.KO.1.dmnd"

        assert "--remove_intermediates" in argv

    def test_remove_intermediates_false_omits_flag(self, tmp_path):
        cu = _make_utils()
        cu._docker_image = "kbutillib/checkm2:1.1.0"
        work = tmp_path / "work"
        work.mkdir()
        db_file = tmp_path / "db" / "uniref100.KO.1.dmnd"
        db_file.parent.mkdir()
        db_file.write_text("fake-db")

        with patch("subprocess.run", return_value=self._fake_completed()) as mock_run:
            cu._run_checkm2_predict(
                work=work,
                genome_names=["genome1.fna"],
                resolved_db=db_file,
                threads=1,
                extension=".fna",
                remove_intermediates=False,
            )

        argv = mock_run.call_args[0][0]
        assert "--remove_intermediates" not in argv

    def test_raises_called_process_error_on_nonzero_exit(self, tmp_path):
        cu = _make_utils()
        cu._docker_image = "kbutillib/checkm2:1.1.0"
        work = tmp_path / "work"
        work.mkdir()
        db_file = tmp_path / "db" / "uniref100.KO.1.dmnd"
        db_file.parent.mkdir()
        db_file.write_text("fake-db")

        with patch(
            "subprocess.run",
            return_value=self._fake_completed(returncode=1, stderr="boom"),
        ):
            with pytest.raises(subprocess.CalledProcessError):
                cu._run_checkm2_predict(
                    work=work,
                    genome_names=["genome1.fna"],
                    resolved_db=db_file,
                    threads=1,
                    extension=".fna",
                    remove_intermediates=True,
                )


# ---------------------------------------------------------------------------
# predict() end-to-end (subprocess mocked, but genome copy / report parse /
# outdir population are exercised for real against tmp_path).
# ---------------------------------------------------------------------------


class TestPredictEndToEnd:
    def test_predict_copies_genomes_parses_report_and_populates_outdir(
        self, tmp_path
    ):
        cu = _make_utils()
        cu._docker_image = "kbutillib/checkm2:1.1.0"

        genome1 = tmp_path / "GB_GCA_002694305.1.fna"
        genome1.write_text(">contig1\nACGT\n")
        genome2 = tmp_path / "1663.201.fna"
        genome2.write_text(">contig2\nACGT\n")

        db_file = tmp_path / "db" / "uniref100.KO.1.dmnd"
        db_file.parent.mkdir()
        db_file.write_text("fake-db")

        outdir = tmp_path / "results"
        report_text = "\n".join(
            [_HEADER, _ROW_HIGH_CONFIDENCE, _ROW_LOW_CONFIDENCE]
        )

        def _fake_run_checkm2_predict(self_, work, **kwargs):
            # Simulate the container writing its (flat) output tree into
            # the bind-mounted work dir, as the real docker run would.
            out = work / "out"
            out.mkdir(parents=True, exist_ok=True)
            (out / "quality_report.tsv").write_text(report_text)
            (out / "checkm2.log").write_text("log contents")
            return "docker run ..."

        with patch.object(cu, "check_availability", return_value=True), patch.object(
            CheckM2Utils, "_run_checkm2_predict", autospec=True,
            side_effect=_fake_run_checkm2_predict,
        ):
            report = cu.predict(
                genome_paths=[genome1, genome2],
                db_path=db_file,
                outdir=outdir,
                threads=2,
                extension=".fna",
            )

        assert set(report) == {"GB_GCA_002694305.1", "1663.201"}
        assert report["GB_GCA_002694305.1"]["Completeness"] == "99.96"
        assert (outdir / "quality_report.tsv").exists()
        assert (outdir / "checkm2.log").exists()

    def test_predict_requires_at_least_one_genome(self, tmp_path):
        cu = _make_utils()
        cu._docker_image = "kbutillib/checkm2:1.1.0"
        with pytest.raises(ValueError):
            cu.predict(
                genome_paths=[],
                db_path=tmp_path / "db" / "uniref100.KO.1.dmnd",
                outdir=tmp_path / "out",
                threads=1,
                extension=".fna",
            )


# ---------------------------------------------------------------------------
# _parse_quality_report — header-name parsing of quality_report.tsv
# ---------------------------------------------------------------------------


class TestParseQualityReport:
    def test_parses_rows_keyed_by_name(self):
        text = "\n".join([_HEADER, _ROW_HIGH_CONFIDENCE, _ROW_LOW_CONFIDENCE])
        rows = _parse_quality_report(text)

        assert set(rows) == {"GB_GCA_002694305.1", "1663.201"}

        high = rows["GB_GCA_002694305.1"]
        assert high["Completeness"] == "99.96"
        assert high["Contamination"] == "1.56"
        assert high["Completeness_Model_Used"] == "Neural Network (Specific Model)"
        assert high["Additional_Notes"] == "None"

    def test_low_confidence_row_parses_fully_and_keeps_its_note(self):
        text = "\n".join([_HEADER, _ROW_HIGH_CONFIDENCE, _ROW_LOW_CONFIDENCE])
        rows = _parse_quality_report(text)

        low = rows["1663.201"]
        assert low["Completeness"] == "59.97"
        assert low["Contamination"] == "0.0"
        assert low["Additional_Notes"] == (
            "Low confidence prediction - substantial (25%) disagreement "
            "between completeness prediction models"
        )
        # A soft low-confidence note is not an error/drop signal: the row
        # is fully present, keyed, and carries a real Completeness score.
        assert "1663.201" in rows

    def test_missing_column_raises(self):
        header_missing_gc = _HEADER.replace("GC_Content\t", "")
        row_missing_gc = _ROW_HIGH_CONFIDENCE.replace("0.41\t", "")
        text = "\n".join([header_missing_gc, row_missing_gc])
        with pytest.raises(ValueError, match="missing expected column"):
            _parse_quality_report(text)

    def test_extra_column_raises(self):
        header_with_extra = _HEADER + "\tUnexpected_Column"
        row_with_extra = _ROW_HIGH_CONFIDENCE + "\tsurprise"
        text = "\n".join([header_with_extra, row_with_extra])
        with pytest.raises(ValueError, match="unexpected column"):
            _parse_quality_report(text)

    def test_empty_report_raises(self):
        with pytest.raises(ValueError, match="empty"):
            _parse_quality_report("")


# ---------------------------------------------------------------------------
# Real-dependency guard — MANDATORY, skips (never errors) when docker or the
# image is unavailable. This is deliberately the one test in this file that
# touches the real boundary: a suite that mocks subprocess.run in 100% of
# its tests proves nothing about whether the real image actually behaves as
# documented.
# ---------------------------------------------------------------------------


class TestRealDockerGuard:
    def test_get_version_against_real_image(self):
        """Invoke the real image's ``checkm2 --version`` and assert ``1.1.0``.

        Uses default config discovery (NOT config_file=False) so that on a
        deployment host with ``checkm2.docker_image`` set in
        ``~/.kbutillib/config.yaml`` (or the project ``config.yaml``), this
        exercises the real container. Skips -- does not fail, does not
        silently pass -- when docker or the configured image is absent, as
        it is on this development machine.
        """
        cu = CheckM2Utils()
        if not cu.check_availability():
            pytest.skip(
                "checkm2 docker image not available locally -- set "
                "checkm2.docker_image in config.yaml and ensure the image "
                "is pulled to exercise this test for real"
            )
        assert cu.get_version() == "1.1.0"
