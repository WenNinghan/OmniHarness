# OpenSandbox local M4 adaptations

The execd adaptation is based on OpenSandbox BOM build commit
`25873227f60424bdc59bfa83b963bbc00e871297` (release 1.2.0). Its original source
is Apache-2.0, copyright The OpenSandbox Authors. Original notices are retained;
OmniHarness additions in this patch are Apache-2.0 too. The complete license is
in `OpenSandbox-APACHE-2.0.txt` and the resulting image. This is a local adapted
runtime, not an unmodified upstream release or an upstream security endorsement.

`opensandbox-m4-execd.patch` changes five Go source files and the existing native
launcher, and adds three focused Go test files. The scope is a command-only API,
static native authentication, UID/GID 10001, environment filtering, and mandatory
capability/no-new-privileges/seccomp enforcement. The launcher checks actual
child state before executing code. It does not introduce another Agent loop,
workflow scheduler or container platform.

The daemon is a trusted root supervisor; its container ceiling retains only
SETUID, SETGID, SETPCAP and KILL. Workloads and entrypoint run as UID/GID 10001
with empty supplementary groups, all five capability sets zero and NoNewPrivs=1.
The runtime refuses missing/degraded hardening rather than continuing. Root
filesystem/PTY/Jupyter/session/proxy/internal-init APIs are not registered.
Only health, command/status/logs and authenticated `/omni/profile` are exposed.
For Docker SDK server-proxy compatibility, `/proxy/44772/` is rewritten into
the same local route table and authentication middleware. It never opens a
network connection; other ports, nested proxies and disabled APIs remain
rejected. Only the existing ping/ready health paths retain anonymous access.

Required deployment settings:

- `runtime.execd_run_as_init=true`; `EXECD_RUNTIME_INIT` absent/false.
- `EXECD_ACCESS_TOKEN` is an independent per-operation secret, only delivered
  to the daemon and trusted SDK via `X-EXECD-ACCESS-TOKEN`. Never put it in argv.
- `EXECD_ISOLATION_CONFIG` refers to root-owned immutable config containing
  `[hardening]`, `enabled=true`, `keep_capabilities=[]`.
- `/opt/opensandbox` is root owned and not writable by the workload. Any env
  file is root-owned mode 0600. Only a filtered workspace copy is mounted RW.
- The controller must pin the adapted image digest/ID and verify the
  authenticated profile before dispatch. Merely using `secure_access=False`
  with the original Docker execd image does not establish authentication.

Authenticated profile JSON fields are:
`profile="omniharness-m4-execd-v1"`, `workload_uid=10001`, `workload_gid=10001`,
`init_mode=true`, `signal_shield=true`, `cap_drop="active"`, `seccomp="active"`.
`init_mode_name="pid1"` is additionally returned for diagnostics.

To reproduce the image, obtain the exact BOM commit, verify its revision,
and make a temporary build context containing its `components/execd`,
`components/internal`, `LICENSE`, plus this project's
`deploy/patches/opensandbox-m4-execd.patch`. Build with
`docker build -f deploy/m4-execd.Dockerfile -t omniharness/m4a-execd:1.2.0-m4a1 <context>`.
The Dockerfile pins Go 1.25.9-bookworm and the original execd image by digest,
checks and applies the exact patch with `git apply`, compiles the original native launcher with
the small local changes, executes the nine `TestM4*` tests and builds execd.
Full upstream source remains in private build/cache directories and is not
vendored into the application repository.

The focused tests cover identity defaults/rejection, final environment scrub,
disabled routes, authenticated profile, self-proxy streaming HTTP status, missing configuration and degraded
startup, plus actual launcher child credentials/capabilities and refusal of
invalid seccomp. They do not claim all upstream feature tests pass: several
intentionally unsupported upstream identity/route behaviors have changed.
Actual container/network/restart/cleanup probes remain separately required.

`opensandbox-m4-egress.patch` retains nft enforcement and DNS/transparent
redirect rules during graceful sidecar shutdown. Docker workloads can still
share that network namespace, so rule deletion must wait for namespace
destruction. The patch changes only this shutdown choice and adds an ordinary
Docker shutdown regression test; the upstream experimental shutdown test also
passes. The derived image retains the original egress supervisor and tools.

To build it, use a separate context containing the same BOM commit's
`components/egress`, `components/internal`, `LICENSE`, and this project's
`deploy/patches/opensandbox-m4-egress.patch`. Run
`docker build -f deploy/opensandbox-egress.Dockerfile -t omniharness/m4a-egress:1.2.0-m4a1 <context>`.
The original egress and Go builder are pinned by digest. The derived image
includes its license and patch under `/usr/share/licenses/omniharness-opensandbox`.

The complete local M4-A Docker suite passed 22 real checks. It includes token
separation, UID/capability state, rejected mounts, positive-control network
fault injection, process-tree cleanup and the application's strict SDK adapter.
Original failures remain separately recorded. Reproduce with the independent
controller from `scripts/m4_sandbox_probe.py` and
`OMNI_M4_SANDBOX_LIVE=1 pytest tests/test_m4_sandbox.py -q`; see
`docs/M4-implementation.md` for tested image IDs and the local validation scope.
