# KBUtilLib-owned Bakta image
#
# PROVENANCE: vendored from github.com/Fxe/bioseed_tools @ a9c22ca (checked
# out on poplar at /scratch1/fliu/hub_scratch/fliu/docker/bioseed_tools).
# That recipe is proven on poplar -- the repo's bakta/worker/worker.py is
# byte-identical to the image actually running there (measured
# 2026-08-23, see
# agent-io/research/2026-08-23-filipe-bakta-kofamscan-recon.md in the
# KBDLJobRunningPrototype repo).
#
# NOT A BYTE-FOR-BYTE PORT, AND SAY SO PLAINLY: this file was authored from
# primary-laptop, which has no network route to poplar or to
# github.com/Fxe/bioseed_tools. It could not be diffed against the literal
# upstream Dockerfile. It is reconstructed from the recon's MEASURED,
# load-bearing facts instead:
#   - `bakta_proteins` (Bakta's protein entry point, not the nucleotide
#     `bakta` contig pipeline) is the binary actually invoked, alongside
#     `bakta`, `bakta_db`, `bakta_io`, `bakta_plot` present in the same
#     image.
#   - `/opt/conda/bin` must be on PATH or `bakta_proteins` fails with
#     "AMRFinderPlus not found or not executable".
#   - Bakta 1.11.4 against DB v6.0 "full" is the pinned, measured pairing
#     (4,493/4,493 genes annotated in 646s on the Keio corpus).
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
#   docker build -t kbutillib/bakta:1.11.4 -t kbutillib/bakta:latest docker/bakta/
#
# Run via BaktaUtils (recommended) -- set in config.yaml:
#   bakta:
#     docker_image: kbutillib/bakta:latest
#
# Direct run (for testing). Per D10/D17 of kbdl-local-bakta-kofamscan-v1,
# containers run with --network none, an explicit --user, and the database
# mounted read-only:
#   docker run --rm --network none --user "$(id -u):$(id -g)" \
#     -v /path/to/bakta-db:/db:ro \
#     -v "$(pwd)":/work \
#     kbutillib/bakta:latest \
#     bakta_proteins --db /db --output /work/out /work/input.faa
#
# BaktaUtils bind-mounts its TemporaryDirectory at /work and the resolved
# BAKTA_DB ObjectRef path at /db:ro, then reads Bakta's native JSON result
# back (results/input.json) from the host side of the /work mount.
#
# The Bakta reference database (registered separately, never baked into this
# image -- see D7/D8/D10 of kbdl-local-bakta-kofamscan-v1) carries its own
# `db/version.json`; this image pins the *tool*, not the database.

FROM condaforge/miniforge3:24.9.2-0

LABEL org.opencontainers.image.title="kbutillib/bakta"
LABEL org.opencontainers.image.description="Bakta protein annotation (bakta_proteins); vendored from Fxe/bioseed_tools@a9c22ca with the Celery worker layer stripped"
LABEL org.opencontainers.image.source="https://github.com/Fxe/bioseed_tools"
LABEL org.opencontainers.image.revision="a9c22ca"

# Install Bakta and its full dependency stack (AMRFinderPlus, Diamond,
# HMMER, tRNAscan-SE, Aragorn, Infernal, PILER-CR, pyrodigal, ...) from
# bioconda/conda-forge into the base conda environment (NOT a named
# sub-env), so the resulting binaries land directly on /opt/conda/bin --
# matching the PATH requirement measured on poplar. Version pinned to the
# tool version measured against the registered DB (1.11.4 / DB major 6);
# an unpinned resolve would let a rebuild elsewhere silently drift to a
# different Bakta with different recall than the one the recon measured.
RUN conda install -n base -y -c conda-forge -c bioconda \
      bakta=1.11.4 \
    && conda clean -afy

# bakta_proteins shells out to AMRFinderPlus by bare name from within its
# own subprocess environment; it must resolve on PATH directly, not only
# through the conda env's activation hooks. Measured failure mode without
# this: "AMRFinderPlus not found or not executable".
ENV PATH=/opt/conda/bin:$PATH

# Sanity layer: fail the build early (not at first annotate) if
# bakta_proteins is not runnable in this image.
RUN bakta_proteins --version

WORKDIR /work

# BaktaUtils always passes an explicit `bakta_proteins ...` argv to
# `docker run`, so this CMD only matters for a bare `docker run
# kbutillib/bakta`. There is deliberately no ENTRYPOINT -- see "STRIPPED
# FROM UPSTREAM" above.
CMD ["bakta_proteins", "--help"]
