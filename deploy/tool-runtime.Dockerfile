# Pinned Python + Node layers already used by this project's WSL deployment.
# These mirrors serve the upstream content-addressed manifests; no TLS bypass.
FROM docker.m.daocloud.io/library/node@sha256:01d566e8d94d8c619591d78652babfccd3c6992a3dffa9ed5ab39e11cf6c70e9 AS node
FROM docker.m.daocloud.io/library/python@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
# Current tested runtime is linux/amd64. Its native compiler is a required,
# integrity-locked dependency, so npm cannot silently omit a failed optional fetch.
COPY tool-types/package.json tool-types/package-lock.json /opt/omniharness/types/
COPY tool-python-requirements.txt /opt/omniharness/tool-python-requirements.txt
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm ci --prefix /opt/omniharness/types --ignore-scripts --no-audit --no-fund \
    && npm cache clean --force \
    && ln -s /opt/omniharness/types/node_modules/typescript/bin/tsc /usr/local/bin/tsc \
    && groupadd --gid 10001 omni \
    && useradd --uid 10001 --gid 10001 --create-home --shell /bin/bash omni \
    && mkdir -p /workspace /opt/opensandbox /opt/omniharness /tmp/omniharness \
    && chown -R 10001:10001 /workspace /tmp/omniharness \
    && printf '[hardening]\nenabled = true\nkeep_capabilities = []\n' > /opt/omniharness/execd-hardening.toml \
    && touch /opt/omniharness/execd.env \
    && chmod 0600 /opt/omniharness/execd.env \
    && chmod 0644 /opt/omniharness/execd-hardening.toml
# Install the pinned, hash-verified Python test runner at build time; tool
# containers do not need network access to run a project's pytest suite.
RUN python -m pip install --no-cache-dir --require-hashes --only-binary=:all: \
    --ignore-installed -r /opt/omniharness/tool-python-requirements.txt
ENV HOME=/home/omni PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENV EXECD_ISOLATION_CONFIG=/opt/omniharness/execd-hardening.toml EXECD_ENVS=/opt/omniharness/execd.env
WORKDIR /workspace
# Only the authenticated, patched execd runs as root with four management caps.
# The server forces every workload to UID/GID 10001 with all capability sets zero.
USER 0:0
CMD ["tail", "-f", "/dev/null"]
