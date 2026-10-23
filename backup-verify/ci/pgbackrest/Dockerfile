# PostgreSQL 16 + pgBackRest, for the CI integration test only. The same
# image runs the source cluster (archive_command -> pgbackrest) and the
# throwaway restore. Ubuntu's own packages, so no third-party apt repo.
ARG BASE=mirror.gcr.io/library/ubuntu:24.04
FROM ${BASE}
ENV DEBIAN_FRONTEND=noninteractive PATH=/usr/lib/postgresql/16/bin:$PATH
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends postgresql-16 pgbackrest \
 && rm -rf /var/lib/apt/lists/* \
 && install -d -o postgres -g postgres -m 2775 /var/run/postgresql \
 && install -d -o postgres -g postgres -m 750 /var/log/pgbackrest /var/lib/pgbackrest /var/spool/pgbackrest
