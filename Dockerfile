# The sandbox-run primitive, ghcr.io/uniconhq/primitive-sandbox-run. One
# container per batch of tests: it reads /work/inputs.json and the binary and
# inputs under /work/in/, and writes /work/outputs.json and each run's output
# under /work/out/.

# Pinned by digest so a release rebuilds from the same base. python:3.14-slim
# (Debian 13), pulled 2026-09-13, the same pin as the runner's images and the
# compile primitive, whose Python and Java versions this image must match to
# run what compile builds. Change it in both primitives together, from
# `docker image inspect python:3.14-slim --format '{{index .RepoDigests 0}}'`.
FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS helper

# sandbox-exec, the small static program every binary is started through, so
# that its peak memory is its own (see src/sandbox-exec.c).
RUN apt-get update \
    && apt-get install --yes --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
COPY src/sandbox-exec.c /src/
RUN gcc -O2 -static -Wall -Werror -o /sandbox-exec /src/sandbox-exec.c


FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

LABEL org.opencontainers.image.title="primitive-sandbox-run" \
      org.opencontainers.image.description="The Unicon sandbox-run primitive: run a binary per test and measure it." \
      org.opencontainers.image.source="https://github.com/uniconhq/primitive-sandbox-run" \
      org.opencontainers.image.licenses="MIT"

# The Java runtime runs the jars compile builds, from the same Debian package
# set as compile's JDK. Native binaries are statically linked and Python
# binaries run on the image's own interpreter. The JRE's post-install scripts
# expect the man directory the slim image leaves out.
RUN mkdir -p /usr/share/man/man1 \
    && apt-get update \
    && apt-get install --yes --no-install-recommends openjdk-21-jre-headless \
    && rm -rf /var/lib/apt/lists/*

COPY --from=helper --chmod=0755 /sandbox-exec /usr/local/bin/sandbox-exec
COPY --chmod=0755 src/sandbox_run.py /usr/local/bin/sandbox-run

# The harness runs every step as a non-root user with a read-only root, and
# /work and /tmp as the only writable places. The program and every binary it
# runs write nowhere else.
USER 65532:65532
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/sandbox-run"]
