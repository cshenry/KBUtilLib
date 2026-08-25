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
# NOT A BYTE-FOR-BYTE PORT, AND SAY SO PLAINLY: this file was authored from
# primary-laptop, which has no network route to poplar or to
# github.com/Fxe/bioseed_tools. It could not be diffed against the literal
# upstream Dockerfile. It is reconstructed from the recon's MEASURED,
# load-bearing facts instead:
#   - KOfamScan v1.3.0 with HMMER 3.4 is the pinned, measured pairing,
#     against the `2025-11-03` profile set (7.2 GB) plus its paired
#     `ko_list`.
#   - `exec_annotation` is an HMM search over protein input by construction
#     (see D2/D11, kbdl-local-bakta-kofamscan-v1) -- no nucleotide path.
#   - Output must be read with `-f detail-tsv` (not `-f mapper`), filtered
#     to rows carrying the `*` significance flag -- that is KofamscanUtils's
#     concern, not this image's, but it is why HMMER's exact version is
#     pinned rather than left to float.
# Whoever next has poplar access should diff this file against the literal
# upstream Dockerfile and correct any drift this reconstruction introduced.
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

# Sanity layer: fail the build early (not at first annotate) if
# exec_annotation is not runnable in this image. exec_annotation has no
# dedicated --version flag; -h is the documented smoke check.
RUN exec_annotation -h >/dev/null

WORKDIR /work

# KofamscanUtils always passes an explicit `exec_annotation ...` argv to
# `docker run`, so this CMD only matters for a bare `docker run
# kbutillib/kofamscan`. There is deliberately no ENTRYPOINT -- see
# "STRIPPED FROM UPSTREAM" above.
CMD ["exec_annotation", "-h"]
