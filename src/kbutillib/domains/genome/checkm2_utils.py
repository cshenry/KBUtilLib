"""CheckM2 utilities for KBUtilLib.

Implements ``CheckM2Utils``, a thin wrapper around CheckM2 v1.1.0
(https://github.com/chklovski/CheckM2) that runs genome-quality prediction
(``checkm2 predict``) inside Docker and parses the resulting
``quality_report.tsv`` into a ``Name``-keyed dict.

This module deliberately follows the existing Dockerised-tool-wrapper
convention used elsewhere in ``domains/genome`` (see ``bakta_utils.py`` /
``kofamscan_utils.py``): a config key holding the pinned image reference, a
``docker image inspect`` availability probe, ``ToolUnavailableError`` (import
ed from ``annotation/annotator_utils.py`` -- CheckM2 does not get its own
exception class) when the image is absent, and a ``docker run`` invocation
built with ``--user <uid>:<gid>``, ``--network none`` and a read-only
database bind-mount.

Unlike ``BaktaUtils``/``KofamscanUtils``, CheckM2 does not produce
``Term``/``AnnotationRecord`` output (it scores whole genomes for
completeness/contamination, not per-gene function), so this class subclasses
``SharedEnvUtils`` directly rather than ``AnnotatorUtils`` -- the same choice
already made for ``SKANIUtils``, CheckM2's nearest sibling in this package.

Container invocation
---------------------
The published image's entrypoint is ``micromamba run -n checkm2 checkm2``,
so (as with ``bakta_utils.py``'s ``--entrypoint bash`` override) the
entrypoint is overridden here to ``micromamba`` and the inner command is
built explicitly::

    docker run --rm
      --user <uid>:<gid>
      --network none
      -v <work>:/work
      -v <db_dir>:/db:ro
      --entrypoint micromamba
      <image>
      run -n checkm2 checkm2 predict
          --input /work/<name1> /work/<name2> ...
          --output-directory /work/out
          --database_path /db/<db file name>
          --threads <threads>
          --extension <extension>
          [--remove_intermediates]

``--user`` is load-bearing: the image's default user is ``mambauser``, and
without the override files written into the bind-mounted work dir get a
container UID the caller cannot read back or clean up. ``--network none`` is
required -- CheckM2 does no network I/O.

Database argument
------------------
``predict()``'s ``db_path`` argument is the path to CheckM2's diamond
reference database *file* (the ``.dmnd`` produced by ``checkm2 database
--download``), not its containing directory. Docker bind-mounts require a
directory, so this module mounts ``db_path``'s parent directory read-only to
``/db`` and passes ``--database_path /db/<db_path.name>`` -- the container
never sees, and cannot write to, anything outside that one file's directory.

Batch input
-----------
CheckM2 natively accepts a list of genomes in one ``--input`` invocation and
writes a single ``quality_report.tsv`` for the whole batch -- ``predict()``
copies every genome in ``genome_paths`` into the per-run scratch work dir
(symlinks are not usable here: a symlink's target would resolve outside the
container's bind-mounted view and be unreadable) and passes the full list to
``--input`` in one call, rather than invoking the tool once per genome.

Quality report parsing
------------------------
``quality_report.tsv`` is TAB-separated with exactly 14 columns. Parsing is
by **header name**, not positional index -- a silently mis-positioned column
would otherwise be a wrong-answer vector -- and a missing or unexpected
column raises ``ValueError`` rather than being silently reinterpreted. Rows
are returned keyed by the ``Name`` column (the input file's basename with
its extension stripped) exactly as CheckM2 reports them; a non-``None``
``Additional_Notes`` (e.g. a low-confidence-prediction disagreement note)
accompanies a fully scored row and is never dropped or treated as a failure.
"""

from __future__ import annotations

import csv
import io
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from kbutillib.core.shared_env_utils import SharedEnvUtils

from .annotation.annotator_utils import ToolUnavailableError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TOOL = "checkm2"
_INSTALL_HINT = (
    "Obtain the CheckM2 v1.1.0 Docker image from the deployment host and set "
    "checkm2.docker_image in config.yaml."
)

# quality_report.tsv's exact column set, in the order CheckM2 v1.1.0 emits
# them -- verified identical across three separate production runs. Parsing
# is by header name (see _parse_quality_report), but both a missing AND an
# extra column are treated as a format break worth raising on rather than
# silently reinterpreting.
_QUALITY_REPORT_COLUMNS: tuple[str, ...] = (
    "Name",
    "Completeness",
    "Contamination",
    "Completeness_Model_Used",
    "Translation_Table_Used",
    "Coding_Density",
    "Contig_N50",
    "Average_Gene_Length",
    "Genome_Size",
    "GC_Content",
    "Total_Coding_Sequences",
    "Total_Contigs",
    "Max_Contig_Length",
    "Additional_Notes",
)


# ---------------------------------------------------------------------------
# Pure parse helper (offline-unit-testable)
# ---------------------------------------------------------------------------


def _parse_quality_report(text: str) -> dict[str, dict[str, str]]:
    """Parse CheckM2's ``quality_report.tsv`` content into a ``Name``-keyed dict.

    Args:
        text: Raw TSV contents of ``quality_report.tsv``.

    Returns:
        Mapping of the report's ``Name`` column to that row as a dict of
        ``{column_name: value}`` (all values are the raw string cells --
        no numeric coercion is applied here). Rows are never filtered,
        dropped or reinterpreted, including rows carrying a non-``None``
        ``Additional_Notes`` soft low-confidence flag.

    Raises:
        ValueError: If the report is empty, or its header does not contain
            exactly the expected 14 columns (a missing OR an unexpected
            extra column both raise -- a silently mis-positioned column is
            a wrong-answer vector, so this never falls back to positional
            parsing).
    """
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    try:
        header = next(reader)
    except StopIteration:
        raise ValueError("checkm2 quality report is empty") from None

    missing = [c for c in _QUALITY_REPORT_COLUMNS if c not in header]
    if missing:
        raise ValueError(
            "checkm2 quality report is missing expected column(s): "
            f"{missing} (header was: {header})"
        )
    extra = [c for c in header if c not in _QUALITY_REPORT_COLUMNS]
    if extra:
        raise ValueError(
            "checkm2 quality report has unexpected column(s): "
            f"{extra} (header was: {header})"
        )

    rows: dict[str, dict[str, str]] = {}
    for row in reader:
        if not row:
            continue
        record = dict(zip(header, row))
        rows[record["Name"]] = record
    return rows


# ---------------------------------------------------------------------------
# CheckM2Utils class
# ---------------------------------------------------------------------------


class CheckM2Utils(SharedEnvUtils):
    """Run CheckM2 v1.1.0 (Docker-only) and parse its quality report.

    Config keys (read via ``get_config_value``):
        ``checkm2.docker_image`` -- the pinned image reference. Empty (the
            default) means CheckM2 is unavailable; ``check_availability()``
            returns ``False`` and ``predict()``/``get_version()`` raise
            ``ToolUnavailableError``. Never hardcode an image digest/tag --
            this is the only source of the image reference.
        ``checkm2.docker_workdir`` -- base dir for the per-run bind-mounted
            work dir (default ``~/.kbutillib/checkm2_work``).

    Example::

        cu = CheckM2Utils()
        if cu.check_availability():
            report = cu.predict(
                genome_paths=[Path("genome1.fna"), Path("genome2.fna")],
                db_path=Path("/data/CheckM2_database/uniref100.KO.1.dmnd"),
                outdir=Path("/scratch/checkm2_out"),
                threads=4,
                extension=".fna",
            )
            print(report["genome1"]["Completeness"])

    Raises:
        ToolUnavailableError: If ``checkm2.docker_image`` is unset or the
            image is not present locally.
    """

    def __init__(self, **kwargs: Any) -> None:
        """Initialize CheckM2Utils.

        Args:
            **kwargs: Forwarded to ``SharedEnvUtils.__init__``.
        """
        super().__init__(**kwargs)
        #: Pinned Docker image reference, or "" when unset (=> unavailable).
        self._docker_image: str = (
            self.get_config_value("checkm2.docker_image", default="") or ""
        )

    # ------------------------------------------------------------------
    # Availability probe
    # ------------------------------------------------------------------

    def check_availability(self) -> bool:
        """Return True if ``docker image inspect <checkm2.docker_image>`` exits 0.

        Side-effect-free: never pulls, builds or runs the image. Returns
        False (without raising) when ``checkm2.docker_image`` is unset,
        docker itself is not on PATH, or the inspect call fails/times out.
        """
        if not self._docker_image:
            return False
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", self._docker_image],
                capture_output=True,
                text=True,
                timeout=15,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False

    def _require_available(self) -> None:
        """Raise ``ToolUnavailableError`` unless the image is present locally."""
        if self.check_availability():
            return
        detail = (
            f"docker image '{self._docker_image}' is not present locally"
            if self._docker_image
            else "checkm2.docker_image is not configured"
        )
        raise ToolUnavailableError(tool=_TOOL, detail=detail, hint=_INSTALL_HINT)

    def _docker_workdir_base(self) -> str:
        """Base dir for the per-run work dir (``tempfile.TemporaryDirectory``).

        Must live under a path Docker Desktop shares by default (see
        ``BaktaUtils._docker_workdir_base`` for the same macOS/Docker
        caveat). Override with ``checkm2.docker_workdir``.
        """
        base = self.get_config_value(
            "checkm2.docker_workdir", default=""
        ) or str(Path.home() / ".kbutillib" / "checkm2_work")
        Path(base).mkdir(parents=True, exist_ok=True)
        return base

    # ------------------------------------------------------------------
    # Version probe
    # ------------------------------------------------------------------

    def get_version(self) -> str:
        """Return the version CheckM2 reports via ``checkm2 --version``.

        Parses ``--version``'s bare ``1.1.0`` output (no ``v`` prefix, no
        trailing text) -- never the ``--help`` banner, which separately
        prints ``CheckM2 v1.1.0`` WITH a ``v`` prefix.

        Returns:
            The stripped stdout of ``checkm2 --version`` (expected:
            ``"1.1.0"``).

        Raises:
            ToolUnavailableError: If the image is not present locally.
            subprocess.CalledProcessError: If the container exits non-zero.
        """
        self._require_available()
        cmd = [
            "docker", "run", "--rm",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--network", "none",
            "--entrypoint", "micromamba",
            self._docker_image,
            "run", "-n", "checkm2", "checkm2", "--version",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, cmd, result.stdout, result.stderr
            )
        return result.stdout.strip()

    # ------------------------------------------------------------------
    # Predict
    # ------------------------------------------------------------------

    def predict(
        self,
        genome_paths: list[Path],
        db_path: Path,
        outdir: Path,
        *,
        threads: int,
        extension: str,
        remove_intermediates: bool = True,
    ) -> dict[str, dict[str, str]]:
        """Run ``checkm2 predict`` over a batch of genomes and parse its report.

        Args:
            genome_paths: Genome FASTA files to score, all in one
                ``checkm2 predict`` invocation (CheckM2 natively batches).
            db_path: Path to CheckM2's diamond reference database *file*
                (e.g. ``uniref100.KO.1.dmnd``) -- its parent directory is
                what gets bind-mounted read-only (see module docstring).
            outdir: Host directory CheckM2's full output tree
                (``quality_report.tsv``, ``checkm2.log``, ``protein_files/``,
                ``diamond_output/``) is copied into. Created if absent.
            threads: Passed verbatim to ``--threads`` (CheckM2 defaults to
                1 and never infers this itself -- always pass explicitly).
            extension: Passed verbatim to ``--extension`` (CheckM2 defaults
                to ``.fna`` and silently ignores non-matching files -- the
                caller must pass the extension its own genome files use,
                never inferred here).
            remove_intermediates: When True (the default), passes
                ``--remove_intermediates`` so protein files and the diamond
                output are removed after scoring.

        Returns:
            The parsed ``quality_report.tsv``, keyed by the report's
            ``Name`` column (see ``_parse_quality_report``).

        Raises:
            ValueError: If ``genome_paths`` is empty, or the report's
                header does not match the expected column set.
            ToolUnavailableError: If the image is not present locally.
            subprocess.CalledProcessError: If the container exits non-zero.
        """
        if not genome_paths:
            raise ValueError("predict() requires at least one genome path")
        self._require_available()

        genome_paths = [Path(p).expanduser().resolve() for p in genome_paths]

        resolved_db = Path(db_path).expanduser().resolve()
        outdir = Path(outdir).expanduser()
        outdir.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(dir=self._docker_workdir_base()) as tmpdir:
            work = Path(tmpdir)
            # Copy (never symlink) genomes into the work dir: a symlink's
            # target lives outside /work and would be unreadable from
            # inside the container's bind-mounted view.
            for genome_path in genome_paths:
                shutil.copy2(genome_path, work / genome_path.name)

            self._run_checkm2_predict(
                work=work,
                genome_names=[p.name for p in genome_paths],
                resolved_db=resolved_db,
                threads=threads,
                extension=extension,
                remove_intermediates=remove_intermediates,
            )

            container_out = work / "out"
            report_path = container_out / "quality_report.tsv"
            text = report_path.read_text(encoding="utf-8")

            # Copy the run's full (flat) output tree into the caller's
            # outdir before the scratch tempdir is cleaned up.
            for item in container_out.iterdir():
                dest = outdir / item.name
                if item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dest)

        return _parse_quality_report(text)

    # ------------------------------------------------------------------
    # Internal subprocess runner
    # ------------------------------------------------------------------

    def _run_checkm2_predict(
        self,
        work: Path,
        genome_names: list[str],
        resolved_db: Path,
        threads: int,
        extension: str,
        remove_intermediates: bool,
    ) -> str:
        """Build and run the ``docker run`` invocation for ``checkm2 predict``.

        Args:
            work: Host scratch dir (already populated with the genome
                copies) bind-mounted to ``/work``.
            genome_names: Basenames of the genomes already copied into
                *work*, passed to ``--input`` as ``/work/<name>``.
            resolved_db: Absolute, resolved path to the database *file*
                (its parent dir is bind-mounted read-only to ``/db``).
            threads: Passed to ``--threads``.
            extension: Passed to ``--extension``.
            remove_intermediates: When True, appends
                ``--remove_intermediates``.

        Returns:
            The shlex-quoted command string (for logging/diagnostics).

        Raises:
            subprocess.CalledProcessError: If the container exits non-zero.
        """
        db_dir = resolved_db.parent
        inner_args = [
            "run", "-n", "checkm2", "checkm2", "predict",
            "--input", *[f"/work/{name}" for name in genome_names],
            "--output-directory", "/work/out",
            "--database_path", f"/db/{resolved_db.name}",
            "--threads", str(threads),
            "--extension", extension,
        ]
        if remove_intermediates:
            inner_args.append("--remove_intermediates")

        cmd = [
            "docker", "run", "--rm",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--network", "none",
            # HOME is load-bearing and must point somewhere WRITABLE. The image's
            # default user is ``mambauser``; the ``--user`` override above leaves
            # ``HOME=/``, which the invoking uid cannot write. micromamba then
            # aborts before CheckM2 ever starts, trying to create its proc cache:
            #
            #   critical libmamba filesystem error: directory iterator cannot
            #   open directory: No such file or directory [/.cache/mamba/proc]
            #
            # ``/work`` is the bind-mounted host scratch dir owned by that same
            # uid, so it is the one reliably writable path in the container.
            # MEASURED on poplar 2026-09-02: without these two flags KBDLCheckM2
            # fails 100% of the time and the adapter's batch-then-isolate retries
            # every genome individually before failing; with them the identical
            # invocation succeeds. See
            # KBDLJobRunningPrototype/agent-io/research/checkm2-live-measurement.md
            "-e", "HOME=/work",
            "-e", "XDG_CACHE_HOME=/work/.cache",
            "-v", f"{work}:/work",
            "-v", f"{db_dir}:/db:ro",
            "--entrypoint", "micromamba",
            self._docker_image,
            *inner_args,
        ]
        command_str = shlex.join(cmd)

        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, cmd, result.stdout, result.stderr
            )
        return command_str


__all__ = [
    "CheckM2Utils",
    "_parse_quality_report",
]
