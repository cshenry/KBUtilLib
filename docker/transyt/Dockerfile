# KBUtilLib-owned Transyt image
#
# Derived from merlin-sysbio/kb_transyt (U Minho / Davide Lagoa).
# This image strips the KBase SDK launcher layers and exposes a clean
# entrypoint: start Neo4j, wait for readiness, run the Transyt JAR.
#
# Prerequisites (vendored sources — mirror these URLs; they may rot):
#   transyt.jar      — https://github.com/merlin-sysbio/transyt/releases
#   data.tar.gz      — Transyt Neo4j 4.0.2 database snapshot
#   workdir.tar.gz   — Transyt working-directory scaffold
#
# Build:
#   docker build -t kbutillib/transyt:latest docker/transyt/
#
# Run via TransytUtils (recommended):
#   from kbutillib import TransytUtils
#   tu = TransytUtils()
#   result = tu.annotate(proteins={...}, tax_id="562")
#
# Direct run (for testing):
#   docker run --rm \
#     -v $(pwd)/my_input_dir:/workdir/processingDir \
#     --entrypoint bash \
#     kbutillib/transyt:latest \
#     -lc 'neo4j start && \
#          i=0; while [ $i -lt 120 ]; do \
#            curl -fsS http://localhost:7474/ >/dev/null 2>&1 && break; \
#            sleep 1; i=$((i+1)); done && \
#          java --add-exports java.base/jdk.internal.misc=ALL-UNNAMED \
#               -Dio.netty.tryReflectionSetAccessible=true \
#               -Dworkdir=/workdir \
#               -Xmx4096m \
#               -jar /opt/transyt/transyt.jar 3 /workdir/processingDir/'
#
# Input directory (bind-mounted at /workdir/processingDir):
#   protein.faa         — FASTA amino-acid sequences
#   params.txt          — Key=value: taxID, reference_database, scoring params
#   metabolites.txt     — (Optional) one ModelSEED compound id per line
#
# Output (written to /workdir/processingDir/results/):
#   transyt.xml                 — SBML with GPR and transport reactions
#   reactions_references.txt    — TSV: transyt rxn → MSRXN → MSCPD

# ---------------------------------------------------------------------------
# Stage 1: Java 11 + Neo4j 4.0.2 base
# ---------------------------------------------------------------------------
FROM ubuntu:20.04 AS base

ENV DEBIAN_FRONTEND=noninteractive
# Latest installable 4.0.x patch (the exact 4.0.2 .deb was pruned from the
# neo4j apt pool).  The Transyt data snapshot is a 4.0.x store and opens on any
# 4.0.x patch — the store format is stable across patch releases within 4.0.
ENV NEO4J_VERSION=1:4.0.12
ENV JAVA_VERSION=11

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    wget \
    gnupg \
    lsb-release \
    ca-certificates \
    openjdk-${JAVA_VERSION}-jre-headless \
    && rm -rf /var/lib/apt/lists/*

# Install Neo4j 4.0.2
RUN wget -O - https://debian.neo4j.com/neotechnology.gpg.key \
      | gpg --dearmor -o /usr/share/keyrings/neo4j.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/neo4j.gpg] https://debian.neo4j.com stable 4.0" \
      > /etc/apt/sources.list.d/neo4j.list && \
    apt-get update && \
    apt-get install -y neo4j=${NEO4J_VERSION} && \
    rm -rf /var/lib/apt/lists/*

# Configure Neo4j for Transyt: allow unauthenticated local access
RUN echo "dbms.security.auth_enabled=false" >> /etc/neo4j/neo4j.conf && \
    echo "dbms.connector.bolt.listen_address=:7687" >> /etc/neo4j/neo4j.conf && \
    echo "dbms.connector.http.listen_address=:7474" >> /etc/neo4j/neo4j.conf

# ---------------------------------------------------------------------------
# Stage 2: Add Transyt JAR and pre-loaded Neo4j database
#
# VENDOR NOTICE: Download these from the Transyt release page and place them
# in docker/transyt/vendor/ before building:
#
#   vendor/transyt.jar        — Transyt Java application JAR
#   vendor/data.tar.gz        — Neo4j 4.0.2 graph DB with TCDB+ModelSEED data
#   vendor/workdir.tar.gz     — Transyt workdir scaffold (optional scripts)
#
# Alternatively, set ARG TRANSYT_URL / DATA_URL / WORKDIR_URL to download
# directly (requires internet access at build time).
# ---------------------------------------------------------------------------

ARG TRANSYT_JAR_URL=""
ARG DATA_URL=""
ARG WORKDIR_URL=""

WORKDIR /opt/transyt

# Copy vendored files if present; fall back to URL download
COPY vendor/transyt.jar* /opt/transyt/transyt.jar.local
RUN if [ -f /opt/transyt/transyt.jar.local ] && \
       [ "$(wc -c < /opt/transyt/transyt.jar.local)" -gt 1000 ]; then \
      mv /opt/transyt/transyt.jar.local /opt/transyt/transyt.jar; \
    elif [ -n "${TRANSYT_JAR_URL}" ]; then \
      curl -fsSL "${TRANSYT_JAR_URL}" -o /opt/transyt/transyt.jar; \
    else \
      echo "ERROR: transyt.jar not found. Place vendor/transyt.jar in docker/transyt/ or set TRANSYT_JAR_URL." >&2; \
      exit 1; \
    fi

WORKDIR /workdir

# Unpack Neo4j database snapshot.
# The tarball is rooted at `data/` (data/databases/{neo4j,system}, data/dbms,
# data/transactions), so it must be extracted into the Neo4j HOME
# (/var/lib/neo4j) — NOT /var/lib/neo4j/data, which would double the path to
# /var/lib/neo4j/data/data/databases/... and leave Neo4j serving an empty DB.
# The transyt graph lives in the default `neo4j` database.
COPY vendor/data.tar.gz* /tmp/data.tar.gz.local
RUN if [ -f /tmp/data.tar.gz.local ] && \
       [ "$(wc -c < /tmp/data.tar.gz.local)" -gt 1000 ]; then \
      tar -xzf /tmp/data.tar.gz.local -C /var/lib/neo4j && \
      rm /tmp/data.tar.gz.local; \
    elif [ -n "${DATA_URL}" ]; then \
      curl -fsSL "${DATA_URL}" | tar -xz -C /var/lib/neo4j; \
    else \
      echo "WARNING: data.tar.gz not found. Neo4j will start with an empty database." >&2; \
    fi

# Unpack workdir scaffold (provides /workdir/system/configurations.txt, which
# the transyt JAR requires).  The tarball is rooted at `workdir/`, so it must be
# extracted into `/` (giving /workdir/system/...) — NOT /workdir, which would
# double the path to /workdir/workdir/system/... and the JAR would abort with
# "default configurations can not be loaded".
COPY vendor/workdir.tar.gz* /tmp/workdir.tar.gz.local
RUN if [ -f /tmp/workdir.tar.gz.local ] && \
       [ "$(wc -c < /tmp/workdir.tar.gz.local)" -gt 1000 ]; then \
      tar -xzf /tmp/workdir.tar.gz.local -C / && \
      rm /tmp/workdir.tar.gz.local; \
    elif [ -n "${WORKDIR_URL}" ]; then \
      curl -fsSL "${WORKDIR_URL}" | tar -xz -C /; \
    fi

# Set correct ownership for Neo4j
RUN chown -R neo4j:neo4j /var/lib/neo4j /var/log/neo4j

# ---------------------------------------------------------------------------
# Stage 3: Runtime entrypoint
# ---------------------------------------------------------------------------

# Expose Neo4j ports (informational)
EXPOSE 7474 7687

# The entrypoint script:
#   1. Start Neo4j in the background.
#   2. Poll until the HTTP interface is up (no fixed sleep).
#   3. Run the Transyt JAR.
#
# TransytUtils overrides this with --entrypoint bash -lc '...' to control
# the exact invocation from Python.  This CMD is the default for direct
# docker run usage.
CMD ["bash", "-lc", "\
neo4j start && \
i=0; while [ $i -lt 120 ]; do \
  curl -fsS http://localhost:7474/ >/dev/null 2>&1 && break; \
  sleep 1; i=$((i+1)); \
done && \
java --add-exports java.base/jdk.internal.misc=ALL-UNNAMED \
     -Dio.netty.tryReflectionSetAccessible=true \
     -Dworkdir=/workdir \
     -Xmx4096m \
     -jar /opt/transyt/transyt.jar 3 /workdir/processingDir/"]
