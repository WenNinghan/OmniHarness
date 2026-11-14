# Derived from OpenSandbox 1.2.0, Apache-2.0.
# Source: opensandbox-group/OpenSandbox@25873227f60424bdc59bfa83b963bbc00e871297
# GO_BUILDER must be the inspected content-addressed Go 1.25.9 Bookworm image.
ARG GO_BUILDER=docker.m.daocloud.io/library/golang@sha256:298734aec230b5f3e8cee450ce6d7eccc39f1797ba548ee90d57e9803030c6c3
FROM ${GO_BUILDER} AS builder
WORKDIR /source
COPY components/internal ./components/internal
COPY components/egress ./components/egress
COPY deploy/patches/opensandbox-m4-egress.patch /tmp/omniharness.patch
RUN git apply --check /tmp/omniharness.patch && git apply /tmp/omniharness.patch
WORKDIR /source/components/egress
ENV CGO_ENABLED=0 GOTOOLCHAIN=local GOMAXPROCS=2 GOFLAGS=-p=2
RUN --mount=type=cache,id=omni-m4a-go-mod,target=/go/pkg/mod \
    --mount=type=cache,id=omni-m4a-go-build,target=/root/.cache/go-build \
    go test -run 'TestOmniHarnessShutdownRetainsSharedNamespaceRules|TestRuntimeQuarantineShutdownNeverDeletesEnforcement' .
RUN --mount=type=cache,id=omni-m4a-go-mod,target=/go/pkg/mod \
    --mount=type=cache,id=omni-m4a-go-build,target=/root/.cache/go-build \
    go build -trimpath -buildvcs=false -ldflags '-s -w -buildid= -B none -X github.com/alibaba/opensandbox/internal/version.Version=1.2.0-omniharness.m4a1 -X github.com/alibaba/opensandbox/internal/version.GitCommit=25873227f60424bdc59bfa83b963bbc00e871297' -o /out/egress .

FROM docker.m.daocloud.io/opensandbox/egress@sha256:a3d30246e557525f3fa44d16226258268cea175532e652822bd1eb4e83e0f54d
COPY --from=builder /out/egress /opt/opensandbox-egress/egress
COPY LICENSE /usr/share/licenses/omniharness-opensandbox/LICENSE
COPY deploy/patches/opensandbox-m4-egress.patch /usr/share/licenses/omniharness-opensandbox/omniharness-m4-egress.patch
LABEL org.opencontainers.image.source="https://github.com/opensandbox-group/OpenSandbox" \
      org.opencontainers.image.revision="25873227f60424bdc59bfa83b963bbc00e871297" \
      org.opencontainers.image.version="1.2.0-omniharness.m4a1" \
      org.opencontainers.image.licenses="Apache-2.0"
