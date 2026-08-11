FROM node:24.17.0-bookworm-slim

ARG CODEX_VERSION=0.147.0
ARG TEA_VERSION=0.14.0
ARG TEA_SHA256=3680e6844b413bd2eb171294e8b1aeb440cdf7bebcfead2ec36b91c1e334310d
ARG BENCH_UID=10000
ARG BENCH_GID=10000

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        jq \
        python3 \
        tini \
    && rm -rf /var/lib/apt/lists/*

RUN npm install --global "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force

RUN curl --fail --location --silent --show-error \
        --retry 5 --retry-all-errors --connect-timeout 20 \
        "https://dl.gitea.com/tea/${TEA_VERSION}/tea-${TEA_VERSION}-linux-amd64" \
        --output /usr/local/bin/tea \
    && echo "${TEA_SHA256}  /usr/local/bin/tea" | sha256sum --check --strict \
    && chmod 0755 /usr/local/bin/tea

RUN groupadd --gid "${BENCH_GID}" benchmark \
    && useradd --uid "${BENCH_UID}" --gid "${BENCH_GID}" \
        --create-home --shell /bin/bash benchmark \
    && install -d --owner "${BENCH_UID}" --group "${BENCH_GID}" \
        --mode 0700 /run/codex-benchmark \
    && install -d --owner "${BENCH_UID}" --group "${BENCH_GID}" \
        --mode 0755 /workspace

COPY --chmod=0755 codex-entrypoint.sh /usr/local/bin/codex-benchmark-entrypoint

ENV CODEX_HOME=/run/codex-benchmark/codex
ENV HOME=/run/codex-benchmark/home
ENV XDG_CONFIG_HOME=/run/codex-benchmark/home/.config
ENV XDG_CACHE_HOME=/run/codex-benchmark/home/.cache
ENV GITEA_REVIEW_CACHE=/run/codex-benchmark/home/.cache/gitea-review
ENV TMPDIR=/run/codex-benchmark/tmp
ENV GIT_CONFIG_GLOBAL=/dev/null
ENV GIT_CONFIG_NOSYSTEM=1

USER benchmark:benchmark
WORKDIR /workspace

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/codex-benchmark-entrypoint"]
