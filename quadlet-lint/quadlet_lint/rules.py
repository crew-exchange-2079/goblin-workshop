"""The lint rules. Each rule is a generator registered with @rule.

Rules see a Context (the parsed unit, its kind, the target Podman version
and the key table) and yield Findings. Rule ids are stable; docs/rules.md
documents every id and tests/test_docs.py keeps the two in step.
"""

import difflib
import re
from dataclasses import dataclass

from . import podmanargs
from .keydb import fmt
from .timespan import INFINITY, format_seconds, parse_timespan

ERROR, WARNING, INFO = "error", "warning", "info"
SEVERITY_ORDER = {INFO: 0, WARNING: 1, ERROR: 2}

# File extension -> the Quadlet section that defines it.
KINDS = {"container": "Container", "volume": "Volume", "network": "Network", "pod": "Pod"}

# Podman's built-in default for `podman stop`/`rm -f` (containers.conf stop_timeout).
PODMAN_DEFAULT_STOP_TIMEOUT = 10
# systemd's DefaultTimeoutStopSec unless system.conf overrides it.
SYSTEMD_DEFAULT_TIMEOUT_STOP = 90

TRUE_WORDS = {"1", "yes", "true", "on"}
FALSE_WORDS = {"0", "no", "false", "off"}
BOOLEAN_KEYS = {
    "Container": {"NoNewPrivileges", "ReadOnly", "SecurityLabelDisable", "SecurityLabelNested",
                  "VolatileTmp", "RunInit", "EnvironmentHost", "HttpProxy", "ReadOnlyTmpfs"},
    "Network": {"DisableDNS", "Internal", "IPv6", "NetworkDeleteOnStop"},
    "Volume": {"Copy"},
    "Quadlet": {"DefaultDependencies"},
}
# Sections Quadlet validates keys in, per file kind. [Unit]/[Service]/[Install]
# belong to systemd and are passed through; X-* sections are ignored by design.
CHECKED_SECTIONS = {kind: {section, "Quadlet"} for kind, section in KINDS.items()}

RESTART_VALUES = {"no", "on-success", "on-failure", "on-abnormal", "on-watchdog", "on-abort", "always"}

SOCKET_RE = re.compile(r"(^|/)(podman|docker)\.sock(:|$)|(^|[:\s])(/var)?/run/podman(/|:|$)|%t/podman(/|:|$)")
DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
DANGEROUS_CAPS = {"ALL", "SYS_ADMIN", "SYS_MODULE", "SYS_PTRACE", "SYS_RAWIO", "NET_ADMIN", "BPF", "DAC_READ_SEARCH"}


@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    severity: str
    summary: str
    kinds: frozenset
    check: object


@dataclass
class Finding:
    rule: str
    name: str
    severity: str
    path: str
    line: int
    message: str

    def as_dict(self):
        return {"rule": self.rule, "name": self.name, "severity": self.severity,
                "path": self.path, "line": self.line, "message": self.message}


RULES = {}


def rule(rule_id, name, severity, summary, kinds=tuple(KINDS)):
    def register(fn):
        assert rule_id not in RULES, rule_id
        RULES[rule_id] = Rule(rule_id, name, severity, summary, frozenset(kinds), fn)
        return fn
    return register


class Context:
    def __init__(self, unit, kind, target, db):
        self.unit = unit
        self.kind = kind
        self.section = KINDS[kind]
        self.target = target
        self.db = db
        self._opts = None

    def opts(self, *names):
        """PodmanArgs options of the main section, filtered by canonical name."""
        if self._opts is None:
            self._opts = podmanargs.options(self.unit.all(self.section, "PodmanArgs"))
        return [o for o in self._opts if not names or o[0] in names]

    def main(self, key):
        return self.unit.last(self.section, key)

    def main_all(self, key):
        return self.unit.all(self.section, key)

    def service(self, key):
        return self.unit.last("Service", key)

    def is_true(self, key):
        e = self.main(key)
        return e is not None and e.value.lower() in TRUE_WORDS

    def first_line(self):
        return self.unit.sections.get(self.section, 1)


def _line(entry, ctx):
    return entry.line if entry is not None else ctx.first_line()


# ---------------------------------------------------------------- QL0xx structure

@rule("QL001", "syntax-error", ERROR, "Line is not valid systemd unit-file syntax.")
def syntax_error(ctx):
    for line, message in ctx.unit.errors:
        yield line, message


@rule("QL002", "missing-section", ERROR, "File has no section matching its extension.")
def missing_section(ctx):
    if not ctx.unit.has_section(ctx.section):
        yield 1, f".{ctx.kind} file has no [{ctx.section}] section; quadlet will refuse to generate a service"


@rule("QL003", "unknown-key", ERROR, "Key is not accepted by Quadlet in any known Podman release.")
def unknown_key(ctx):
    for section in CHECKED_SECTIONS[ctx.kind]:
        known = ctx.db.keys(section)
        for e in ctx.unit.in_section(section):
            if e.key in known:
                continue
            hint = difflib.get_close_matches(e.key, list(known), n=1, cutoff=0.75)
            if not hint:
                lowered = {k.lower(): k for k in known}
                hint = [lowered[e.key.lower()]] if e.key.lower() in lowered else []
            extra = f"; did you mean {hint[0]}=?" if hint else ""
            yield e.line, f"[{section}] {e.key}= is not a Quadlet key{extra}"


@rule("QL004", "key-too-new", ERROR, "Key exists, but only in a newer Podman than the target.")
def key_too_new(ctx):
    first = min(info["added"] for info in ctx.db.keys(ctx.section).values())
    if ctx.target[:2] < first[:2]:
        yield 1, f".{ctx.kind} units need Podman {fmt(first)} or newer (target is {fmt(ctx.target)})"
    for section in CHECKED_SECTIONS[ctx.kind]:
        for e in ctx.unit.in_section(section):
            status, info = ctx.db.status(section, e.key, ctx.target)
            if status == "too-new":
                yield e.line, (f"[{section}] {e.key}= needs Podman {fmt(info['added'])} or newer "
                               f"(target is {fmt(ctx.target)})")


@rule("QL005", "key-removed", ERROR, "Key was removed before the target Podman release.")
def key_removed(ctx):
    for section in CHECKED_SECTIONS[ctx.kind]:
        for e in ctx.unit.in_section(section):
            status, info = ctx.db.status(section, e.key, ctx.target)
            if status == "removed":
                yield e.line, f"[{section}] {e.key}= was removed in Podman {fmt(info['removed'])}"


@rule("QL006", "deprecated-key", WARNING, "Key is deprecated upstream.")
def deprecated_key(ctx):
    for section in CHECKED_SECTIONS[ctx.kind]:
        for e in ctx.unit.in_section(section):
            status, info = ctx.db.status(section, e.key, ctx.target)
            if status == "ok" and info["deprecated"]:
                replacement = {"VolatileTmp": "Tmpfs=/tmp"}.get(e.key, "UserNS=/UIDMap=/GIDMap=")
                yield e.line, f"[{section}] {e.key}= is deprecated; use {replacement} instead"


@rule("QL007", "invalid-boolean", ERROR, "Boolean value that Quadlet silently reads as false.")
def invalid_boolean(ctx):
    for section in CHECKED_SECTIONS[ctx.kind]:
        for e in ctx.unit.in_section(section):
            if e.key not in BOOLEAN_KEYS.get(section, ()):
                continue
            if e.value.lower() not in TRUE_WORDS | FALSE_WORDS:
                yield e.line, (f"{e.key}={e.value} is not 1/yes/true/on or 0/no/false/off; "
                               "quadlet reads anything else as false")
    e = ctx.main("Notify") if ctx.kind == "container" else None
    if e is not None and e.value.lower() not in TRUE_WORDS | FALSE_WORDS | {"healthy"}:
        yield e.line, f"Notify={e.value} is not a boolean or 'healthy'; quadlet reads it as false"


@rule("QL008", "missing-image", ERROR, "Container has neither Image= nor Rootfs=.", kinds=("container",))
def missing_image(ctx):
    if ctx.unit.has_section("Container") and not ctx.main("Image") and not ctx.main("Rootfs"):
        yield ctx.first_line(), "[Container] needs Image= (or Rootfs=); quadlet will refuse this unit"


# ---------------------------------------------------------------- QL1xx images

def _image_entries(ctx):
    if ctx.kind == "container":
        e = ctx.main("Image")
        return [e] if e is not None and e.value else []
    if ctx.kind == "volume":
        e = ctx.main("Image")
        driver = ctx.main("Driver")
        if e is not None and e.value and (driver is None or driver.value == "image"):
            return [e]
    return []


def _is_quadlet_ref(image):
    # Image=foo.image / foo.build point at another quadlet unit, pinned there.
    return image.endswith((".image", ".build"))


def _has_registry(image):
    first = image.split("/", 1)[0]
    return "/" in image and ("." in first or ":" in first or first == "localhost")


@rule("QL101", "image-not-pinned", WARNING, "Image reference is not pinned by digest.",
      kinds=("container", "volume"))
def image_not_pinned(ctx):
    for e in _image_entries(ctx):
        image = e.value
        if _is_quadlet_ref(image) or DIGEST_RE.search(image):
            continue
        name = image.rsplit("/", 1)[-1]
        tag = name.split(":", 1)[1] if ":" in name else None
        what = "no tag (implicitly :latest)" if tag is None else f"tag :{tag}"
        yield e.line, (f"Image={image} uses {what} and no @sha256: digest; "
                       "the running image can change under you")


@rule("QL102", "image-short-name", WARNING, "Image reference has no registry host.",
      kinds=("container", "volume"))
def image_short_name(ctx):
    for e in _image_entries(ctx):
        if _is_quadlet_ref(e.value) or _has_registry(e.value):
            continue
        yield e.line, (f"Image={e.value} is a short name; resolution depends on registries.conf "
                       "and AutoUpdate=registry rejects it. Use a fully qualified name such as "
                       "docker.io/library/<name>")


# ---------------------------------------------------------------- QL2xx health

def _health_cmd(ctx):
    e = ctx.main("HealthCmd")
    if e is not None and e.value and e.value.lower() != "none":
        return e
    for name, value, entry in ctx.opts("--health-cmd"):
        if value.lower() != "none":
            return entry
    return None


@rule("QL201", "missing-healthcheck", INFO, "Container defines no health check.", kinds=("container",))
def missing_healthcheck(ctx):
    if ctx.unit.has_section("Container") and _health_cmd(ctx) is None:
        if any(v == "true" for _, v, _ in ctx.opts("--no-healthcheck")):
            return
        yield ctx.first_line(), ("no HealthCmd=; systemd only knows whether the process is alive, "
                                 "not whether it works (add HealthCmd= and HealthOnFailure=)")


@rule("QL202", "notify-healthy-without-healthcmd", WARNING, "Notify=healthy without a health check in the unit.",
      kinds=("container",))
def notify_healthy(ctx):
    e = ctx.main("Notify")
    if e is not None and e.value.lower() == "healthy" and _health_cmd(ctx) is None:
        yield e.line, ("Notify=healthy needs a health check, and none is defined here; unless the image "
                       "itself has a HEALTHCHECK, podman refuses to start the container")


# ---------------------------------------------------------------- QL3xx risky options

@rule("QL301", "privileged", ERROR, "Container runs privileged.", kinds=("container",))
def privileged(ctx):
    e = ctx.main("Privileged")
    if e is not None and e.value.lower() in TRUE_WORDS:
        yield e.line, ("Privileged= is not a Quadlet key and is ignored, but the intent is a "
                       "privileged container; grant specific AddCapability=/AddDevice= instead")
    for _, value, entry in ctx.opts("--privileged"):
        if value != "false":
            yield entry.line, ("PodmanArgs=--privileged disables nearly all isolation (all capabilities, "
                               "all devices, no SELinux/seccomp); grant specific AddCapability=/AddDevice= instead")


@rule("QL302", "security-label-disabled", WARNING, "SELinux separation is turned off.", kinds=("container",))
def security_label_disabled(ctx):
    if ctx.is_true("SecurityLabelDisable"):
        yield ctx.main("SecurityLabelDisable").line, (
            "SecurityLabelDisable=true turns off SELinux separation; prefer :z/:Z volume "
            "options or SecurityLabelType=")
    for _, value, entry in ctx.opts("--security-opt"):
        if value.replace(":", "=", 1) == "label=disable":
            yield entry.line, "PodmanArgs --security-opt label=disable turns off SELinux separation"


@rule("QL303", "host-network", WARNING, "Uses the host network namespace.", kinds=("container", "pod"))
def host_network(ctx):
    for e in ctx.main_all("Network"):
        if e.value.split(":", 1)[0] == "host":
            yield e.line, "Network=host shares the host's network stack: no port isolation, host services reachable"
    for _, value, entry in ctx.opts("--network"):
        if value.split(":", 1)[0] == "host":
            yield entry.line, "PodmanArgs --network=host shares the host's network stack"


@rule("QL304", "podman-socket-mount", ERROR, "Mounts the Podman or Docker API socket.",
      kinds=("container", "pod"))
def podman_socket(ctx):
    candidates = [(e.value.split(":", 1)[0], e) for e in ctx.main_all("Volume")]
    for e in ctx.main_all("Mount"):
        for part in e.value.split(","):
            k, _, v = part.partition("=")
            if k.strip() in ("source", "src"):
                candidates.append((v.strip(), e))
    for _, value, entry in ctx.opts("--volume"):
        candidates.append((value.split(":", 1)[0], entry))
    for _, value, entry in ctx.opts("--mount"):
        for part in value.split(","):
            k, _, v = part.partition("=")
            if k.strip() in ("source", "src"):
                candidates.append((v.strip(), entry))
    for source, entry in candidates:
        if SOCKET_RE.search(source):
            yield entry.line, (f"mounting {source} hands the container full control of the container "
                               "engine, which is root-equivalent on the host for rootful Podman")


@rule("QL305", "host-namespace", WARNING, "Joins the host PID, IPC, UTS or cgroup namespace.",
      kinds=("container", "pod"))
def host_namespace(ctx):
    for name, value, entry in ctx.opts("--pid", "--ipc", "--uts", "--cgroupns"):
        if value == "host":
            yield entry.line, f"PodmanArgs {name}=host shares the host's {name[2:].upper()} namespace"


@rule("QL306", "dangerous-capability", WARNING, "Adds a broad Linux capability.", kinds=("container",))
def dangerous_capability(ctx):
    found = [(cap, e) for e in ctx.main_all("AddCapability") for cap in e.value.split()]
    found += [(cap, entry) for _, value, entry in ctx.opts("--cap-add") for cap in value.split(",")]
    for cap, entry in found:
        norm = cap.upper().removeprefix("CAP_")
        if norm in DANGEROUS_CAPS:
            yield entry.line, f"capability {norm} is close to full root; narrow it if you can"


@rule("QL307", "confinement-disabled", WARNING, "Seccomp or AppArmor confinement is turned off.",
      kinds=("container",))
def confinement_disabled(ctx):
    e = ctx.main("SeccompProfile")
    if e is not None and e.value == "unconfined":
        yield e.line, "SeccompProfile=unconfined removes the syscall filter"
    e = ctx.main("AppArmor")
    if e is not None and e.value == "unconfined":
        yield e.line, "AppArmor=unconfined removes the AppArmor profile"
    for _, value, entry in ctx.opts("--security-opt"):
        norm = value.replace(":", "=", 1)
        if norm in ("seccomp=unconfined", "apparmor=unconfined"):
            yield entry.line, f"PodmanArgs --security-opt {norm} removes confinement"


# ---------------------------------------------------------------- QL4xx restart / service

def _service_type(ctx):
    e = ctx.service("Type")
    if e is not None:
        return e.value
    return {"container": "notify", "pod": "forking", "volume": "oneshot", "network": "oneshot"}[ctx.kind]


def _restart(ctx):
    e = ctx.service("Restart")
    if e is not None:
        return e.value, e
    if ctx.kind == "pod":
        return "on-failure", None  # quadlet's default for pods
    return "no", None


@rule("QL401", "restart-missing", INFO, "Container service has no Restart= policy.", kinds=("container",))
def restart_missing(ctx):
    if ctx.service("Restart") is None and _service_type(ctx) != "oneshot":
        line = ctx.unit.sections.get("Service", ctx.first_line())
        yield line, ("no Restart= in [Service]; quadlet does not set one, so a crashed container "
                     "stays down (Restart=on-failure is the usual choice)")


@rule("QL402", "restart-invalid", ERROR, "Restart= value is not one systemd accepts.")
def restart_invalid(ctx):
    e = ctx.service("Restart")
    if e is not None and e.value not in RESTART_VALUES:
        yield e.line, f"Restart={e.value} is not one of {', '.join(sorted(RESTART_VALUES))}"


@rule("QL403", "restart-oneshot", ERROR, "Restart=always/on-success on a Type=oneshot service.")
def restart_oneshot(ctx):
    value, e = _restart(ctx)
    if e is not None and value in ("always", "on-success") and _service_type(ctx) == "oneshot":
        why = "" if ctx.service("Type") else f" (quadlet makes .{ctx.kind} units Type=oneshot)"
        yield e.line, f"systemd refuses Restart={value} for Type=oneshot services{why}"


@rule("QL404", "restart-without-delay", INFO, "Restart= without RestartSec= can exhaust the start limit.",
      kinds=("container", "pod"))
def restart_without_delay(ctx):
    value, e = _restart(ctx)
    if e is None or value == "no" or value not in RESTART_VALUES:
        return
    if ctx.service("RestartSec") is None and ctx.unit.last("Unit", "StartLimitIntervalSec") is None \
            and ctx.unit.last("Unit", "StartLimitBurst") is None:
        yield e.line, (f"Restart={value} with the default RestartSec=100ms: a container that fails fast "
                       "hits the start limit (5 starts in 10s) and stays failed. Set RestartSec= "
                       "(e.g. 5s) or StartLimit*= in [Unit]")


@rule("QL405", "service-type-invalid", ERROR, "Container [Service] Type= other than notify or oneshot.",
      kinds=("container",))
def service_type_invalid(ctx):
    e = ctx.service("Type")
    if e is not None and e.value not in ("notify", "oneshot"):
        yield e.line, f"Type={e.value}: quadlet only accepts Type=notify or Type=oneshot for containers"


@rule("QL406", "health-action-without-restart", WARNING,
      "HealthOnFailure=kill/stop without a Restart= that brings the container back.", kinds=("container",))
def health_action_without_restart(ctx):
    e = ctx.main("HealthOnFailure")
    if e is None or e.value not in ("kill", "stop"):
        return
    value, _ = _restart(ctx)
    if value not in ("on-failure", "on-abnormal", "always") and not (value == "on-abort" and e.value == "kill"):
        yield e.line, (f"HealthOnFailure={e.value} stops an unhealthy container, but Restart={value} "
                       "never starts it again; add Restart=on-failure")


# ---------------------------------------------------------------- QL5xx stop timeouts

def _stop_timeout(ctx):
    """(seconds, entry, explicit) for the container's stop timeout."""
    e = ctx.main("StopTimeout")
    if e is not None:
        return e.value, e, True
    opts = ctx.opts("--stop-timeout")
    if opts:
        return opts[-1][1], opts[-1][2], True
    return str(PODMAN_DEFAULT_STOP_TIMEOUT), None, False


def _stop_key(ctx):
    """How to set the stop timeout on the target release (StopTimeout= arrived in 5.0)."""
    if ctx.db.status("Container", "StopTimeout", ctx.target)[0] == "ok":
        return "StopTimeout="
    return "PodmanArgs=--stop-timeout"


def _timeout_stop_sec(ctx):
    for key in ("TimeoutStopSec", "TimeoutSec"):
        e = ctx.service(key)
        if e is not None:
            return e.value, e, key
    return str(SYSTEMD_DEFAULT_TIMEOUT_STOP), None, None


@rule("QL501", "stop-timeout-exceeds-systemd", WARNING,
      "Podman stop timeout is not shorter than systemd's TimeoutStopSec.", kinds=("container",))
def stop_timeout_order(ctx):
    raw_stop, stop_entry, stop_explicit = _stop_timeout(ctx)
    raw_sd, sd_entry, sd_key = _timeout_stop_sec(ctx)
    if not stop_explicit and sd_key is None:
        return
    try:
        stop = int(raw_stop)
    except ValueError:
        return  # QL503
    sd = parse_timespan(raw_sd)
    if sd is None or sd == 0:
        # TimeoutStopSec=0 disables the timeout, like infinity.
        return
    if stop < 0:
        return
    if stop >= sd:
        where = stop_entry or sd_entry
        stop_desc = f"StopTimeout={stop}" if stop_explicit else f"podman's default stop timeout ({stop}s)"
        sd_desc = f"{sd_key}={raw_sd}" if sd_key else f"systemd's default TimeoutStopSec ({format_seconds(sd)})"
        yield _line(where, ctx), (f"{stop_desc} is not shorter than {sd_desc}: systemd kills the stop "
                                  "command before podman's graceful window ends. Keep "
                                  f"{_stop_key(ctx)} a few seconds below TimeoutStopSec")


@rule("QL502", "stop-timeout-implicit", INFO,
      "TimeoutStopSec= is set but the container's stop timeout is left implicit.", kinds=("container",))
def stop_timeout_implicit(ctx):
    _, _, stop_explicit = _stop_timeout(ctx)
    raw_sd, sd_entry, sd_key = _timeout_stop_sec(ctx)
    if sd_key is None or stop_explicit:
        return
    sd = parse_timespan(raw_sd)
    if sd is not None and sd > PODMAN_DEFAULT_STOP_TIMEOUT:
        key = _stop_key(ctx)
        yield sd_entry.line, (f"{sd_key}={raw_sd} is set but the stop timeout is podman's default "
                              f"({PODMAN_DEFAULT_STOP_TIMEOUT}s, or containers.conf stop_timeout); "
                              f"set {key} explicitly so the ordering is visible")


@rule("QL503", "invalid-timeout", ERROR, "StopTimeout= or TimeoutStopSec= cannot be parsed.")
def invalid_timeout(ctx):
    if ctx.kind == "container":
        raw, e, explicit = _stop_timeout(ctx)
        if explicit:
            try:
                int(raw)
            except ValueError:
                yield _line(e, ctx), f"stop timeout {raw!r} is not a whole number of seconds"
    for key in ("TimeoutStopSec", "TimeoutSec", "TimeoutStartSec", "RestartSec"):
        e = ctx.service(key)
        if e is not None and parse_timespan(e.value) is None:
            yield e.line, f"{key}={e.value} is not a systemd time span (e.g. 30, 30s, 1min 30s, infinity)"


# ---------------------------------------------------------------- QL6xx read-only rootfs

def _read_only(ctx):
    if ctx.is_true("ReadOnly"):
        return True
    return any(v != "false" for _, v, _ in ctx.opts("--read-only"))


@rule("QL601", "read-only-rootfs", INFO, "Root filesystem is writable.", kinds=("container",))
def read_only_rootfs(ctx):
    if ctx.unit.has_section("Container") and not _read_only(ctx):
        e = ctx.main("ReadOnly")
        yield _line(e, ctx), ("root filesystem is writable; consider ReadOnly=true with Volume=/Tmpfs= "
                              "for the paths the app writes (ReadOnlyTmpfs keeps /tmp, /run writable)")


@rule("QL602", "read-only-no-tmpfs", INFO, "ReadOnly=true with ReadOnlyTmpfs=false and no writable /tmp.",
      kinds=("container",))
def read_only_no_tmpfs(ctx):
    if not _read_only(ctx):
        return
    e = ctx.main("ReadOnlyTmpfs")
    if e is None or e.value.lower() in TRUE_WORDS:
        return
    if ctx.main_all("Tmpfs") or ctx.is_true("VolatileTmp"):
        return
    if any("tmpfs" in m.value for m in ctx.main_all("Mount")):
        return
    yield e.line, "ReadOnly=true with ReadOnlyTmpfs=false leaves /tmp, /var/tmp and /run read-only; add Tmpfs=/tmp if the app needs scratch space"


def run_rules(ctx, selected):
    findings = []
    for rule_id, r in RULES.items():
        if rule_id not in selected or ctx.kind not in r.kinds:
            continue
        if rule_id in ctx.unit.disabled:
            continue
        for line, message in r.check(ctx):
            if rule_id in ctx.unit.disabled_lines.get(line, ()):
                continue
            findings.append(Finding(rule_id, r.name, r.severity, ctx.unit.path, line, message))
    return findings
