# quadlet-lint rules

Every finding carries a stable id (`QL304`), a name (`podman-socket-mount`) and
a severity:

* **error**: Quadlet will reject the unit, systemd will refuse it, or the unit
  hands out root-equivalent access.
* **warning**: the unit works but is fragile or weakens isolation.
* **info**: a hardening suggestion. Info findings never fail a run unless you
  pass `--fail-on info`.

Silence a rule for one file with a comment anywhere in it, or for the next
assignment only:

```ini
# quadlet-lint: disable=QL101,QL201
[Container]
# quadlet-lint: disable-next-line=QL303
Network=host
```

or for the whole run with `--disable QL201`.

Quoting follows Quadlet's own lookups on the target release. Single-valued
keys (`Image=`, `ReadOnly=`, `SeccompProfile=`, `StopTimeout=`, ...) lose
surrounding double quotes from Podman 5.0: 5.x strips every leading and
trailing `"`, 6.0+ strips one matched pair, and 4.x keeps them, so
`ReadOnly="true"` is false on 4.x (QL007). Multi-valued keys (`Volume=`,
`Network=`, `Mount=`) are used as written; `AddCapability=` and
`PodmanArgs=` are split into words with quotes removed.

Rule groups: `QL0xx` structure and keys, `QL1xx` images, `QL2xx` health
checks, `QL3xx` risky options, `QL4xx` restart and service settings, `QL5xx`
stop timeouts, `QL6xx` read-only root filesystem.

---

## Structure and keys

### QL001 `syntax-error`

Severity: **error**. Applies to all files.

A line is not valid unit-file syntax: a malformed `[Section` header, a line
with no `=`, an invalid key name, or an assignment before the first section
header. systemd and Quadlet skip or reject such lines, so whatever you meant
by them is not happening.

### QL002 `missing-section`

Severity: **error**. Applies to all files.

A `.container` file needs a `[Container]` section, a `.volume` file a
`[Volume]` section, and so on. Without it the Quadlet generator produces no
service.

### QL003 `unknown-key`

Severity: **error**. Applies to all files.

The key appears in no Podman release's list of accepted keys for that section
(`[Container]`, `[Volume]`, `[Network]`, `[Pod]`, `[Quadlet]`). The Quadlet
generator fails the whole unit on an unknown key, so the service silently
never appears. quadlet-lint suggests the nearest real key, e.g.
`Enviroment=` → `Environment=`.

`[Unit]`, `[Service]` and `[Install]` belong to systemd and are not checked
here; `X-` sections are reserved for extensions and ignored.

### QL004 `key-too-new`

Severity: **error**. Applies to all files.

The key exists, but only in a Podman release newer than `--podman-version`.
On the target release Quadlet rejects it as unknown. Upgrade Podman or use
the older spelling, often `PodmanArgs=` with the equivalent `podman run`
flag.

The key table (`quadlet_lint/keydb.json`) is extracted from
`pkg/systemd/quadlet/quadlet.go` at every `vX.Y.0` tag from 4.4 (the first
release with Quadlet) onwards, so it reflects what each release's generator
actually accepts. Only major.minor is compared: a key backported in a patch
release is reported as arriving in the next minor.

### QL005 `key-removed`

Severity: **error**. Applies to all files.

The key was accepted by older releases but removed before the target
release. No key has been removed upstream yet; the rule exists so the table
can record removals when they happen.

### QL006 `deprecated-key`

Severity: **warning**. Applies to all files.

Upstream marks the key deprecated (`RemapUsers=`, `RemapUid=`, `RemapGid=`,
`RemapUidSize=`, `VolatileTmp=`). It still works but may go away. Use
`UserNS=`/`UIDMap=`/`GIDMap=` instead of the `Remap*` keys and `Tmpfs=/tmp`
instead of `VolatileTmp=`.

### QL007 `invalid-boolean`

Severity: **error**. Applies to all files.

Quadlet's boolean parser treats `1`, `yes`, `true` and `on` (any case) as
true and **everything else as false**, without complaint. So `ReadOnly=y`,
`ReadOnly=enabled` or `NoNewPrivileges=ture` all quietly turn the option off.
(systemd itself accepts `y` and `t`; Quadlet does not.) `Notify=` also
accepts `healthy`.

### QL008 `missing-image`

Severity: **error**. Applies to `.container`.

A container needs `Image=` (or `Rootfs=`); Quadlet refuses the unit
otherwise.

## Images

### QL101 `image-not-pinned`

Severity: **warning**. Applies to `.container`, and `.volume` with `Driver=image`.

The image is referenced by tag (or no tag, which means `:latest`) instead of
by digest. A tag can move, so a restart, a `podman auto-update`, or a fresh
host can run different code than the one you tested.

```ini
# instead of
Image=docker.io/library/nginx:1.27
# pin the digest (keeping the tag for readability is allowed)
Image=docker.io/library/nginx:1.27@sha256:<64 hex digits>
```

References to other Quadlet units (`Image=app.image`, `Image=app.build`) are
skipped; pin the image in that unit instead.

If you deliberately track a tag with `AutoUpdate=registry`, disable this rule
for that file.

### QL102 `image-short-name`

Severity: **warning**. Applies to `.container`, and `.volume` with `Driver=image`.

The reference has no registry host (`nginx`, `library/nginx`). Podman
resolves short names through `registries.conf`, which can prompt, differ
between hosts, or pick an unexpected registry, and `AutoUpdate=registry`
requires a fully qualified reference. Write `docker.io/library/nginx`.

## Health checks

### QL201 `missing-healthcheck`

Severity: **info**. Applies to `.container`.

Without `HealthCmd=` (or `PodmanArgs=--health-cmd`), systemd only knows that
the container's process exists, not that the service works. Add a health
check, and `HealthOnFailure=kill` plus `Restart=on-failure` to have systemd
replace a wedged container. An explicit `--no-healthcheck` in `PodmanArgs=`
silences the rule. A `HEALTHCHECK` baked into the image also works but is not
visible to the linter; disable the rule for that file if you rely on one.

### QL202 `notify-healthy-without-healthcmd`

Severity: **warning**. Applies to `.container`.

`Notify=healthy` makes systemd wait for the first passing health check before
the unit counts as started, and Podman refuses to create the container
("sdnotify policy healthy requires a healthcheck") when there is none. The
unit defines no `HealthCmd=`, so it works only if the image carries its own
Docker-format `HEALTHCHECK`, which the linter cannot see. Declaring the
check in the unit makes it explicit.

## Risky options

### QL301 `privileged`

Severity: **error**. Applies to `.container`.

`PodmanArgs=--privileged` gives the container every capability, every host
device, and no SELinux or seccomp confinement. A `Privileged=true` key is
also reported: it is not a Quadlet key (QL003) so it does nothing, but it
signals the same intent. Grant the specific `AddCapability=` / `AddDevice=`
the workload needs instead.

### QL302 `security-label-disabled`

Severity: **warning**. Applies to `.container`.

`SecurityLabelDisable=true`, or `--security-opt label=disable` in
`PodmanArgs=`, turns off SELinux separation between the container and the
host. Usually the real problem is a bind mount without a label; use the
`:z`/`:Z` volume option or a specific `SecurityLabelType=`.

### QL303 `host-network`

Severity: **warning**. Applies to `.container` and `.pod`.

`Network=host` (or `--network=host` / `--net host`) puts the container in the
host's network namespace: every port it opens is a host port, and it can
reach services bound to `localhost` on the host. Prefer a Quadlet `.network`
and `PublishPort=`.

### QL304 `podman-socket-mount`

Severity: **error**. Applies to `.container` and `.pod`.

A `Volume=`, `Mount=` or `PodmanArgs=-v/--volume/--mount` mounts the Podman
or Docker API socket (`podman.sock`, `docker.sock`, `/run/podman`,
`%t/podman`). Whoever controls the container can then start a privileged
container with `/` mounted, so for rootful Podman this is root on the host,
and for rootless it is full control of that user's containers. If a tool
really needs the API, run it rootless under a dedicated user, or put an
API-filtering proxy in between.

### QL305 `host-namespace`

Severity: **warning**. Applies to `.container` and `.pod`.

`PodmanArgs=` joins the host's PID, IPC, UTS or cgroup namespace
(`--pid=host`, `--ipc=host`, `--uts=host`, `--cgroupns=host`). With the host
PID namespace the container sees, and with enough privileges can signal or
ptrace, every host process.

### QL306 `dangerous-capability`

Severity: **warning**. Applies to `.container`.

`AddCapability=` or `--cap-add` grants a capability that is close to full
root: `ALL`, `SYS_ADMIN`, `SYS_MODULE`, `SYS_PTRACE`, `SYS_RAWIO`,
`NET_ADMIN`, `BPF` or `DAC_READ_SEARCH`.

### QL307 `confinement-disabled`

Severity: **warning**. Applies to `.container`.

`SeccompProfile=unconfined`, `AppArmor=unconfined`, or
`--security-opt seccomp=unconfined` / `apparmor=unconfined` removes a layer of
kernel attack-surface reduction. Write a custom profile that allows the
syscalls you need instead.

## Restart and service settings

### QL401 `restart-missing`

Severity: **info**. Applies to `.container`.

Quadlet does not add a `Restart=` to container services, so systemd leaves a
crashed container down. Most long-running services want
`Restart=on-failure` in `[Service]`. (`.pod` units get `Restart=on-failure`
from Quadlet by default; `Type=oneshot` containers are skipped.)

### QL402 `restart-invalid`

Severity: **error**. Applies to all files.

`Restart=` must be one of `no`, `on-success`, `on-failure`, `on-abnormal`,
`on-watchdog`, `on-abort`, `always`. systemd ignores any other value with a
log message, leaving the unit with no restart policy.

### QL403 `restart-oneshot`

Severity: **error**. Applies to all files.

systemd refuses to load a `Type=oneshot` service with `Restart=always` or
`Restart=on-success`. Quadlet makes `.volume` and `.network` units
`Type=oneshot`, and a `.container` can opt into it, so these combinations
make the generated unit fail to load. Use `Restart=on-failure` or drop it.

### QL404 `restart-without-delay`

Severity: **info**. Applies to `.container` and `.pod`.

With a `Restart=` policy but no `RestartSec=`, systemd restarts after 100 ms.
A container that fails immediately (bad config, missing image, port in use)
burns through the default start limit (5 starts within 10 s) in about a
second, after which the unit stays failed until someone runs
`systemctl reset-failed`. Set `RestartSec=` (a few seconds) or tune
`StartLimitIntervalSec=`/`StartLimitBurst=` in `[Unit]`.

### QL405 `service-type-invalid`

Severity: **error**. Applies to `.container`.

Quadlet only accepts `Type=notify` (the default it sets) or `Type=oneshot`
for containers, and fails the unit for anything else (`simple`, `forking`,
`exec`).

### QL406 `health-action-without-restart`

Severity: **warning**. Applies to `.container`.

`HealthOnFailure=kill` or `stop` takes an unhealthy container down, relying on
systemd to start it again. Without `Restart=on-failure` (or `always`,
`on-abnormal`; `on-abort` covers `kill` only) it just stays down, turning a
degraded service into an outage.

## Stop timeouts

When systemd stops a container unit it runs Quadlet's `ExecStop`
(`podman rm -f`), which sends the stop signal and waits the container's stop
timeout (`StopTimeout=`, else `--stop-timeout` in `PodmanArgs=`, else Podman's
default of 10 s or `stop_timeout` in containers.conf) before SIGKILL. systemd
in turn allows the whole stop `TimeoutStopSec=` (or `TimeoutSec=`, default
90 s) before it kills everything itself.

### QL501 `stop-timeout-exceeds-systemd`

Severity: **warning**. Applies to `.container`.

The effective container stop timeout is not shorter than the effective
`TimeoutStopSec`. systemd then SIGKILLs the stop command and the container
before Podman's grace period ends, so the application never gets the
shutdown time you configured, and Podman's cleanup may not run. Keep
`StopTimeout=` a few seconds below `TimeoutStopSec=`:

```ini
[Container]
StopTimeout=50

[Service]
TimeoutStopSec=60
```

Only checked when at least one side is set explicitly. `TimeoutStopSec=0`
and `infinity` disable systemd's timeout and never conflict. Defaults changed
in `system.conf` (`DefaultTimeoutStopSec`) or `containers.conf` are not
visible to the linter.

### QL502 `stop-timeout-implicit`

Severity: **info**. Applies to `.container`.

`TimeoutStopSec=` is raised above 10 s but the container stop timeout is
left at Podman's default. The longer systemd window is wasted (Podman still
kills after 10 s), and the ordering depends on `containers.conf`. Set
`StopTimeout=` (Podman 5.0+) or `PodmanArgs=--stop-timeout` explicitly.

### QL503 `invalid-timeout`

Severity: **error**. Applies to all files.

`StopTimeout=` must be a whole number of seconds (Podman's
`--stop-timeout`), and `TimeoutStopSec=`, `TimeoutSec=`, `TimeoutStartSec=`,
`RestartSec=` must be systemd time spans (`30`, `30s`, `1min 30s`,
`infinity`).

## Read-only root filesystem

### QL601 `read-only-rootfs`

Severity: **info**. Applies to `.container`.

The container's root filesystem is writable. `ReadOnly=true` stops an
attacker or a bug from modifying binaries and config inside the container
and makes accidental state-in-the-image obvious. Podman keeps `/tmp`,
`/var/tmp`, `/run` and `/dev` writable as tmpfs by default (`ReadOnlyTmpfs`),
so most apps need only a `Volume=` or `Tmpfs=` for their data directory.

### QL602 `read-only-no-tmpfs`

Severity: **info**. Applies to `.container`.

`ReadOnly=true` together with `ReadOnlyTmpfs=false` leaves `/tmp`,
`/var/tmp` and `/run` read-only too. That is fine for a fully static binary,
but most software expects a writable `/tmp`; add `Tmpfs=/tmp` if it does.
