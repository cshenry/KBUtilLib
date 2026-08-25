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
# RECONSTRUCTED-THEN-CORRECTED ON POPLAR (2026-08-25): this file was first
# authored from primary-laptop, which had no network route to poplar or to
# github.com/Fxe/bioseed_tools, so it could not be diffed against the literal
# upstream Dockerfile. The first reconstruction used
# `FROM condaforge/miniforge3:24.9.2-0` + `conda install bakta=1.11.4`. On
# poplar that DOES NOT BUILD: miniforge3:24.9.2-0 pins python 3.12, but
# bakta 1.11.4 requires python >=3.9,<3.12 (measured
# `LibMambaUnsatisfiableError` on 2026-08-25).
#
# CORRECTED to match the literal upstream recipe now that poplar access is
# available: `FROM oschwengers/bakta:v1.11.4` -- the exact base upstream's
# bakta/Dockerfile and bakta/cts/Dockerfile use. That base ships the whole
# pinned stack (bakta_proteins 1.11.4, AMRFinderPlus, Diamond, HMMER,
# tRNAscan-SE, Aragorn, Infernal, PILER-CR, pyrodigal) under /opt/conda/bin.
# Load-bearing facts that survive the correction:
#   - `bakta_proteins` (Bakta's protein entry point, not the nucleotide
#     `bakta` contig pipeline) is the binary actually invoked, alongside
#     `bakta`, `bakta_db`, `bakta_io`, `bakta_plot` present in the same
#     image.
#   - `/opt/conda/bin` must be on PATH or `bakta_proteins` fails with
#     "AMRFinderPlus not found or not executable". Verified in the v1.11.4
#     base: `/opt/conda/bin/bakta_proteins`, `/opt/conda/bin/amrfinder`.
#   - Bakta 1.11.4 against DB v6.0 "full" is the pinned, measured pairing
#     (4,493/4,493 genes annotated in 646s on the Keio corpus).
# The only intentional divergence from upstream is dropping the Celery worker
# layer (see "STRIPPED FROM UPSTREAM" below).
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

# The exact base upstream (Fxe/bioseed_tools@a9c22ca) uses for both its
# worker image (bakta/Dockerfile) and its standalone image
# (bakta/cts/Dockerfile). It bakes bakta_proteins 1.11.4 and its full
# dependency stack into /opt/conda/bin -- no conda install layer needed, and
# no version drift, because the base tag itself pins the tool.
FROM oschwengers/bakta:v1.11.4

LABEL org.opencontainers.image.title="kbutillib/bakta"
LABEL org.opencontainers.image.description="Bakta protein annotation (bakta_proteins); vendored from Fxe/bioseed_tools@a9c22ca with the Celery worker layer stripped"
LABEL org.opencontainers.image.source="https://github.com/Fxe/bioseed_tools"
LABEL org.opencontainers.image.revision="a9c22ca"

# bakta_proteins shells out to AMRFinderPlus by bare name from within its
# own subprocess environment; it must resolve on PATH directly, not only
# through the conda env's activation hooks. Measured failure mode without
# this: "AMRFinderPlus not found or not executable".
#
# NOTE (measured on poplar 2026-08-25): the oschwengers/bakta base sets
# `SHELL ["bash","-l","-c"]` and activates conda through its /entrypoint.sh,
# NOT through a static PATH. A bare `ENV PATH=/opt/conda/bin:$PATH` is
# clobbered by the login shell's own profile, so `RUN bakta_proteins`
# failed with "command not found". BaktaUtils sidesteps this by invoking
# `--entrypoint bash -lc 'PATH=/opt/conda/bin:$PATH bakta_proteins ...'`
# (see BaktaUtils._run_bakta). The sanity layer below uses the identical
# incantation so it validates the exact call path the adapter uses.
ENV PATH=/opt/conda/bin:$PATH

# Sanity layer: fail the build early (not at first annotate) if
# bakta_proteins is not runnable via the adapter's own invocation.
RUN bash -lc 'PATH=/opt/conda/bin:$PATH bakta_proteins --version'

WORKDIR /work

# The oschwengers/bakta base sets `ENTRYPOINT ["/entrypoint.sh"]`, whose
# body is just `bakta "$@"` -- benign (no Celery/worker; that hazard only
# lived in the upstream worker layer we did NOT copy), but it would prepend
# `bakta` to a bare `docker run` argv. Reset it to nothing so this image
# has NO ENTRYPOINT, as the design intends: BaktaUtils always overrides the
# entrypoint anyway (`--entrypoint bash`), and a bare `docker run
# kbutillib/bakta` should just run the CMD below.
ENTRYPOINT []

# BaktaUtils always passes an explicit `bakta_proteins ...` argv to
# `docker run`, so this CMD only matters for a bare `docker run
# kbutillib/bakta`. There is deliberately no worker ENTRYPOINT -- see
# "STRIPPED FROM UPSTREAM" above.
CMD ["bakta_proteins", "--help"]
