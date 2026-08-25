# KBUtilLib-owned KOfamScan image
#
# PROVENANCE: vendored from github.com/Fxe/bioseed_tools @ a9c22ca (checked
# out on poplar at /scratch1/fliu/hub_scratch/fliu/docker/bioseed_tools).
# That recipe is proven on poplar -- both tools in that repo were run
# standalone from their own images with the databases mounted read-only,
# --network none, entrypoint overridden and --user set, producing
# byte-identical output to the Celery-service runs (measured 2026-08-23,
# see agent-io/research/2026-08-23-filipe-bakta-kofamscan-recon.md in the
# KBDLJobRunningPrototype repo).
#
# RECONSTRUCTED-THEN-CORRECTED ON POPLAR (2026-08-25): this file was first
# authored from primary-laptop with no network route to poplar or to
# github.com/Fxe/bioseed_tools, so it could not be diffed against the
# literal upstream Dockerfile. The literal upstream recipe is
# `FROM python:3.13-slim` + apt ruby/parallel + HMMER 3.4 compiled from a
# vendored `src/hmmer.tar.gz` + a vendored `src/kofam_scan` Ruby tool copied
# to `/opt/kofam_scan`, then a Celery worker layer.
#
# This reconstruction keeps the conda base instead of vendoring fliu's
# 19 MB HMMER tarball + kofam_scan source into KBUtilLib. Verified on poplar
# 2026-08-25 that `conda install kofamscan=1.3.0 hmmer=3.4` DOES build
# (unlike the bakta conda recipe) and yields the measured pairing:
# `exec_annotation` (kofamscan 1.3.0-2) + `hmmsearch` reporting
# "HMMER 3.4 (Aug 2023)", plus ruby 3.4 and GNU parallel, all on
# /opt/conda/bin.
#
# ONE DRIFT CORRECTED: conda installs `exec_annotation` at
# `/opt/conda/bin/exec_annotation`, but KofamscanUtils's default
# `kofamscan.executable` is `/opt/kofam_scan/exec_annotation` (the upstream
# vendored path) and it invokes that path as argv[0] with NO PATH lookup.
# We add a symlink so the upstream path resolves out-of-box; because the
# Ruby entry point uses `require_relative 'lib/kofam_scan'`, the symlink
# resolves to the real file and its sibling `lib/` is found correctly.
# Load-bearing facts that survive the correction:
#   - KOfamScan v1.3.0 with HMMER 3.4 is the pinned, measured pairing,
#     against the `2025-11-03` profile set (7.2 GB) plus its paired
#     `ko_list`.
#   - `exec_annotation` is an HMM search over protein input by construction
#     (see D2/D11, kbdl-local-bakta-kofamscan-v1) -- no nucleotide path.
#   - Output must be read with `-f detail-tsv` (not `-f mapper`), filtered
#     to rows carrying the `*` significance flag -- that is KofamscanUtils's
#     concern, not this image's, but it is why HMMER's exact version is
#     pinned rather than left to float.
#
# STRIPPED FROM UPSTREAM: the Celery worker layer. Upstream's default
# entrypoint is /worker/run_worker.sh, which subscribes to fliu's Celery
# queue -- running it here unoverridden would silently steal his tasks.
# This image installs no worker.py, no run_worker.sh, and defines no
# ENTRYPOINT at all, so the hazard is structurally impossible rather than
# reliant on every caller remembering `--entrypoint`.
#
# Build:
#   docker build -t kbutillib/kofamscan:1.3.0 -t kbutillib/kofamscan:latest docker/kofamscan/
#
# Run via KofamscanUtils (recommended) -- set in config.yaml:
#   kofamscan:
#     docker_image: kbutillib/kofamscan:latest
#     ko_function_map: /path/to/ko_function_map.tsv   # see build_ko_function_map.py
#
# Direct run (for testing). Per D10/D17 of kbdl-local-bakta-kofamscan-v1,
# containers run with --network none, an explicit --user, and the profile
# set mounted read-only. `<root>` below is the registered KOfam reference
# object, holding `profiles/<name>/` (HMMs) and its paired
# `profiles/<name>.txt` (ko_list) -- see D7/D8:
#   docker run --rm --network none --user "$(id -u):$(id -g)" \
#     -v /path/to/kofam-root:/db:ro \
#     -v "$(pwd)":/work \
#     kbutillib/kofamscan:latest \
#     exec_annotation -p /db/profiles/2025-11-03 -k /db/profiles/2025-11-03.txt \
#       -f detail-tsv --cpu "$(nproc)" -o /work/out.tsv /work/input.faa
#
# KofamscanUtils bind-mounts its TemporaryDirectory at /work and the
# resolved KOFAM_PROFILES ObjectRef path at /db:ro, selects the profile
# set by `kofam_profile_set` name (never a path -- see D7), and parses
# `/work/out.tsv` back on the host side of the mount, filtering to rows
# carrying the `*` significance flag (D11).

FROM condaforge/miniforge3:24.9.2-0

LABEL org.opencontainers.image.title="kbutillib/kofamscan"
LABEL org.opencontainers.image.description="KOfamScan KO annotation (exec_annotation); vendored from Fxe/bioseed_tools@a9c22ca with the Celery worker layer stripped"
LABEL org.opencontainers.image.source="https://github.com/Fxe/bioseed_tools"
LABEL org.opencontainers.image.revision="a9c22ca"

# kofamscan (exec_annotation) plus its runtime dependencies (HMMER 3.4,
# GNU parallel, ruby) from bioconda/conda-forge into the base conda
# environment. Both pinned to the versions measured against the registered
# `2025-11-03` profile set; an unpinned resolve would let a rebuild
# elsewhere silently drift to a different HMMER/kofamscan pairing than the
# one the recon measured.
RUN conda install -n base -y -c conda-forge -c bioconda \
      kofamscan=1.3.0 \
      hmmer=3.4 \
    && conda clean -afy

ENV PATH=/opt/conda/bin:$PATH

# Expose exec_annotation at the upstream vendored path
# (/opt/kofam_scan/exec_annotation) that KofamscanUtils uses by default as
# argv[0] with no PATH lookup. The Ruby entry point resolves its own lib/
# via require_relative against the *real* file, so a symlink is sufficient.
RUN mkdir -p /opt/kofam_scan \
    && ln -s /opt/conda/bin/exec_annotation /opt/kofam_scan/exec_annotation

# Sanity layer: fail the build early (not at first annotate) if
# exec_annotation is not runnable in this image, via BOTH the PATH name and
# the adapter's default absolute path. exec_annotation has no dedicated
# --version flag; -h is the documented smoke check.
RUN exec_annotation -h >/dev/null \
    && /opt/kofam_scan/exec_annotation -h >/dev/null

WORKDIR /work

# The condaforge/miniforge3 base sets `ENTRYPOINT ["tini","--"]`. tini is a
# benign init/reaper and does NOT break the adapter's argv invocation
# (verified: `docker run <img> /opt/kofam_scan/exec_annotation -h` works),
# but KofamscanUtils passes its command as plain argv with no
# `--entrypoint` override, so we reset ENTRYPOINT to nothing to match this
# image's stated "no ENTRYPOINT" design and keep a bare `docker run`
# predictable. Containers are short-lived `--rm` runs, so PID-1 reaping is
# not needed.
ENTRYPOINT []

# KofamscanUtils always passes an explicit `exec_annotation ...` argv to
# `docker run` (default `/opt/kofam_scan/exec_annotation`), so this CMD only
# matters for a bare `docker run kbutillib/kofamscan`. There is deliberately
# no worker ENTRYPOINT -- see "STRIPPED FROM UPSTREAM" above.
CMD ["/opt/kofam_scan/exec_annotation", "-h"]
