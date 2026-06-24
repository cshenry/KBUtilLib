# KBUtilLib-owned Prokka image
#
# Thin pin over the well-maintained StaPH-B Prokka image, re-tagged under the
# kbutillib/ namespace so the local annotation pipeline has a single owned tag
# (mirrors docker/transyt).
#
# Prokka (https://github.com/tseemann/prokka) is a Perl program with a stack of
# binary dependencies (BioPerl, BLAST+, HMMER, Prodigal, Aragorn, Barrnap,
# tbl2asn).  Running it via Docker sidesteps the macOS / arm64 BioPerl + Perl
# dependency pain entirely: the base image is linux/amd64 and runs under
# Docker's emulation on Apple Silicon.
#
# Build:
#   docker build -t kbutillib/prokka:1.14.6 -t kbutillib/prokka:latest docker/prokka/
#
# Run via ProkkaUtils (recommended) — set in config.yaml:
#   prokka:
#     docker_image: kbutillib/prokka:latest
#
# Direct run (for testing) — prokka expects a bind-mounted work dir:
#   docker run --rm --platform linux/amd64 -v "$(pwd)":/work \
#     kbutillib/prokka:latest \
#     prokka --outdir /work/out --prefix prokka --force --quiet /work/input.fasta
#
# ProkkaUtils bind-mounts its TemporaryDirectory at /work and translates the
# --outdir / input-FASTA arguments to /work/<name>, then reads prokka.tsv /
# prokka.gff back from the host side of the mount.
FROM --platform=linux/amd64 staphb/prokka:1.14.6

# Sanity layer: fail the build early (not at first annotate) if prokka is not
# runnable in the base image.
RUN prokka --version

# ProkkaUtils always passes an explicit `prokka ...` argv to `docker run`, so
# this CMD only matters for a bare `docker run kbutillib/prokka`.
CMD ["prokka", "--help"]
