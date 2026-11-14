# Build context: clean OpenSandbox BOM source + deploy/patches (see README).
# Source: 25873227f60424bdc59bfa83b963bbc00e871297; local Apache-2.0 patch.
FROM docker.m.daocloud.io/library/golang@sha256:298734aec230b5f3e8cee450ce6d7eccc39f1797ba548ee90d57e9803030c6c3 AS builder
ENV GOTOOLCHAIN=local GOMAXPROCS=2 GOFLAGS=-p=2 CGO_ENABLED=0
WORKDIR /build
COPY components/internal ./components/internal
COPY components/execd ./components/execd
COPY deploy/patches/opensandbox-m4-execd.patch /tmp/omni-m4.patch
RUN git apply --check /tmp/omni-m4.patch && git apply /tmp/omni-m4.patch
WORKDIR /build/components/execd
RUN gcc -static -Os -Wall -Wextra -Werror -o /build/opensandbox-launcher native/launcher.c
RUN --mount=type=cache,id=omni-m4a-go-mod,target=/go/pkg/mod \
    --mount=type=cache,id=omni-m4a-go-build,target=/root/.cache/go-build \
    M4_LAUNCHER_BINARY=/build/opensandbox-launcher go test -run TestM4 -count=1 ./pkg/runtime ./pkg/web . \
    && go build -trimpath -buildvcs=false \
       -ldflags="-buildid= -X github.com/alibaba/opensandbox/internal/version.Version=1.2.0-omniharness-m4.3 -X github.com/alibaba/opensandbox/internal/version.GitCommit=25873227f60424bdc59bfa83b963bbc00e871297+omni-m4.3" \
       -o /build/execd .

# Retain the pinned upstream distribution layout expected by Controller.
FROM docker.m.daocloud.io/opensandbox/execd@sha256:c75836c46ef4302afccec182d5d7339eb2f905f51e3d73b7aa17bb76c832526c
LABEL org.opencontainers.image.title="OmniHarness M4 command runtime" \
      org.opencontainers.image.version="1.2.0-omniharness-m4.3" \
      org.opencontainers.image.source="https://github.com/opensandbox-group/OpenSandbox" \
      org.opencontainers.image.revision="25873227f60424bdc59bfa83b963bbc00e871297+omni-m4.3" \
      org.opencontainers.image.licenses="Apache-2.0" \
      io.omniharness.execd.profile="omniharness-m4-execd-v1"
COPY --from=builder --chown=0:0 --chmod=0755 /build/execd /execd
COPY --from=builder --chown=0:0 --chmod=0755 /build/opensandbox-launcher /usr/local/libexec/opensandbox-launcher
COPY --from=builder --chown=0:0 --chmod=0755 /build/opensandbox-launcher /opt/opensandbox/opensandbox-launcher
COPY LICENSE /licenses/OpenSandbox-APACHE-2.0.txt
COPY deploy/patches/opensandbox-m4-execd.patch /licenses/omniharness-m4-execd.patch
