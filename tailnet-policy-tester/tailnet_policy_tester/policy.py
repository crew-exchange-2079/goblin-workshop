"""Parse and validate a Tailscale policy file into plain data structures.

Only the sections the evaluator uses are modelled: groups, tagOwners, hosts,
acls, grants, ssh, tests and sshTests. Other known sections are kept out of
evaluation and reported as warnings; see README "Unsupported".

Keys are matched case-insensitively, as Go's encoding/json does when the
control plane unmarshals the file ("ACLs", "Acls" and "acls" are the same).
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field

from . import hujson

# --- vocabulary --------------------------------------------------------------

# IANA protocol names Tailscale accepts in "proto" / grant "ip" (policy-syntax
# docs, "proto"); numbers 0-255 are also accepted and normalised to names.
PROTO_NUMBERS = {
    "icmp": 1, "igmp": 2, "ipv4": 4, "ip-in-ip": 4, "tcp": 6, "egp": 8,
    "igp": 9, "udp": 17, "gre": 47, "esp": 50, "ah": 51, "ipv6-icmp": 58,
    "sctp": 132, "fc": 133,
}
_NUMBER_TO_PROTO = {}
for _name, _num in PROTO_NUMBERS.items():
    _NUMBER_TO_PROTO.setdefault(_num, _name)

PORTED_PROTOS = frozenset({"tcp", "udp", "sctp"})
# ACL with "proto" omitted: TCP, UDP and ICMP (v4 and v6).
DEFAULT_PROTOS = frozenset({"tcp", "udp", "icmp", "ipv6-icmp"})
ANY_PROTO = "*"
# Protocols a tests entry may name (policy-syntax docs, "tests").
TEST_PROTOS = PORTED_PROTOS | {"icmp"}

ROLE_AUTOGROUPS = {
    "autogroup:owner": "owner", "autogroup:admin": "admin",
    "autogroup:member": "member", "autogroup:it-admin": "it-admin",
    "autogroup:network-admin": "network-admin",
    "autogroup:billing-admin": "billing-admin",
    "autogroup:auditor": "auditor",
}
SUPPORTED_AUTOGROUPS = frozenset(ROLE_AUTOGROUPS) | {
    "autogroup:tagged", "autogroup:self", "autogroup:internet",
    "autogroup:danger-all", "autogroup:nonroot",
}
# Autogroups that exist in Tailscale but that this tool cannot model.
UNSUPPORTED_AUTOGROUPS = frozenset({"autogroup:shared"})

KNOWN_SECTIONS = {
    "groups", "hosts", "tagowners", "acls", "grants", "ssh", "tests",
    "sshtests", "defaultsrcposture",
}
IGNORED_SECTIONS = {
    "nodeattrs", "autoapprovers", "derpmap", "disableipv4", "onecgnatroute",
    "randomizeclientport", "postures", "ipsets",
    "attrconfig", "warndevicesgrantedbyacls",
}

TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")

_TAG_RE = re.compile(r"^tag:[A-Za-z0-9][A-Za-z0-9-]*$")
_GROUP_RE = re.compile(r"^group:[A-Za-z0-9][A-Za-z0-9._-]*$")
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_DURATION_RE = re.compile(r"^(?:\d+(?:\.\d+)?(?:h|m|s|ms))+$")
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(h|ms|m|s)")


class PolicyError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


# --- data --------------------------------------------------------------------

@dataclass(frozen=True)
class Selector:
    """One src/dst alias. kind: wildcard|user|group|tag|autogroup|host|prefix."""
    kind: str
    value: str
    network: object = None  # ipaddress network for prefix/host

    def __str__(self):
        return self.value


@dataclass(frozen=True)
class PortSet:
    ranges: tuple[tuple[int, int], ...]

    @classmethod
    def any(cls):
        return cls(((0, 65535),))

    def contains(self, port: int) -> bool:
        return any(lo <= port <= hi for lo, hi in self.ranges)

    def __str__(self):
        if self.ranges == ((0, 65535),):
            return "*"
        return ",".join(str(lo) if lo == hi else f"{lo}-{hi}" for lo, hi in self.ranges)


@dataclass(frozen=True)
class Cap:
    """Protocols (frozenset of names, or ANY_PROTO) with a port set."""
    protos: object
    ports: PortSet

    def allows(self, proto: str, port: int | None) -> bool:
        if self.protos != ANY_PROTO and proto not in self.protos:
            return False
        if proto not in PORTED_PROTOS:
            # tailscaled lets ICMP and other portless protocols through only
            # for rules that cover every port (wgengine/filter:
            # matchProtoAndIPsOnlyIfAllPorts).
            return self.ports == PortSet.any()
        return port is not None and self.ports.contains(port)


@dataclass
class DstEntry:
    selector: Selector
    caps: list[Cap]


@dataclass
class NetRule:
    origin: str  # e.g. "acls[2]" or "grants[0]"
    srcs: list[Selector]
    dsts: list[DstEntry]
    conditions: list[str] = field(default_factory=list)


@dataclass
class SSHRule:
    origin: str
    action: str  # accept | check
    srcs: list[Selector]
    dsts: list[Selector]
    users: list[str]
    check_period: str | None = None
    accept_env: list[str] = field(default_factory=list)


@dataclass
class NetTest:
    origin: str
    src: str
    proto: str | None
    accept: list[str]
    deny: list[str]


@dataclass
class SSHTest:
    origin: str
    src: str
    dst: list[str]
    accept: list[str]
    check: list[str]
    deny: list[str]


@dataclass
class Policy:
    groups: dict[str, list[str]] = field(default_factory=dict)
    tag_owners: dict[str, list[str]] = field(default_factory=dict)
    hosts: dict[str, object] = field(default_factory=dict)
    rules: list[NetRule] = field(default_factory=list)
    ssh: list[SSHRule] = field(default_factory=list)
    tests: list[NetTest] = field(default_factory=list)
    ssh_tests: list[SSHTest] = field(default_factory=list)
    default_src_posture: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --- helpers -----------------------------------------------------------------

def is_user(s: str) -> bool:
    return "@" in s and ":" not in s.split("@", 1)[0] and not s.startswith(("group:", "tag:", "autogroup:"))


def parse_network(s: str):
    """Parse an IP or CIDR; None if it is neither. Host bits must be zero."""
    try:
        if "/" in s:
            return ipaddress.ip_network(s, strict=True)
        return ipaddress.ip_network(ipaddress.ip_address(s))
    except ValueError:
        return None


def parse_ports(spec: str) -> PortSet:
    """'*', '22', '80,443', '1000-2000' (policy-syntax docs, "dst" ports)."""
    spec = spec.strip()
    if spec == "*":
        return PortSet.any()
    if not spec:
        raise ValueError("empty port list")
    ranges = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo_s, _, hi_s = part.partition("-")
            lo, hi = _port(lo_s), _port(hi_s)
            if lo > hi:
                raise ValueError(f"inverted port range {part!r}")
        else:
            lo = hi = _port(part)
            if lo == 0:
                raise ValueError("port 0 is only valid inside a range")
        ranges.append((lo, hi))
    return PortSet(_merge(ranges))


def _port(s: str) -> int:
    if not s.isdigit():
        raise ValueError(f"invalid port {s!r}")
    n = int(s)
    if not 0 <= n <= 65535:
        raise ValueError(f"port {n} out of range")
    return n


def _merge(ranges):
    out = []
    for lo, hi in sorted(ranges):
        if out and lo <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return tuple(out)


def parse_proto(value) -> str:
    """Return a canonical protocol name; numbers map to names when known."""
    s = str(value).strip().lower()
    if s.isdigit():
        n = int(s)
        if not 0 <= n <= 255:
            raise ValueError(f"protocol number {n} out of range")
        return _NUMBER_TO_PROTO.get(n, f"proto-{n}")
    if s in PROTO_NUMBERS:
        return s
    raise ValueError(f"unknown protocol {value!r}")


def split_dst(s: str) -> tuple[str, str]:
    """Split 'alias:ports' at the last colon; '[v6]:ports' is accepted."""
    if s.startswith("["):
        end = s.find("]")
        if end < 0 or s[end + 1:end + 2] not in (":", "/"):
            raise ValueError(f"bad bracketed address {s!r}")
        s = s[1:end] + s[end + 1:]
    alias, sep, ports = s.rpartition(":")
    if not sep or not alias or not ports:
        raise ValueError(f"destination {s!r} must be alias:ports")
    return alias, ports


def parse_duration(s: str) -> float:
    """Go time.ParseDuration subset (h, m, s, ms) -> seconds."""
    if not _DURATION_RE.match(s):
        raise ValueError(f"invalid duration {s!r}")
    mult = {"h": 3600, "m": 60, "s": 1, "ms": 0.001}
    return sum(float(n) * mult[u] for n, u in _DURATION_PART.findall(s))


def _ci(obj: dict) -> dict:
    """Lower-case the keys of a policy object; reject case-only duplicates."""
    out = {}
    for k, v in obj.items():
        lk = k.lower()
        if lk in out:
            raise ValueError(f"key {k!r} duplicates another key ignoring case")
        out[lk] = v
    return out


# --- parser ------------------------------------------------------------------

class _Parser:
    def __init__(self, data):
        self.errors: list[str] = []
        self.p = Policy()
        self.data = data
        self.used_tags: list[tuple[str, str]] = []
        self.used_groups: list[tuple[str, str]] = []
        self.used_hosts: list[tuple[str, str]] = []

    def err(self, path, msg):
        self.errors.append(f"{path}: {msg}")

    def warn(self, path, msg):
        self.p.warnings.append(f"{path}: {msg}")

    def run(self) -> Policy:
        if not isinstance(self.data, dict):
            raise PolicyError(["policy: top level must be an object"])
        try:
            top = _ci(self.data)
        except ValueError as e:
            raise PolicyError([f"policy: {e}"]) from None
        for k in top:
            if k in IGNORED_SECTIONS:
                self.warn(k, "section is not evaluated by this tool")
            elif k not in KNOWN_SECTIONS:
                self.warn(k, "unknown section, ignored")
        self.groups(top.get("groups", {}))
        self.tag_owners(top.get("tagowners", {}))
        self.hosts(top.get("hosts", {}))
        # defaultSrcPosture applies to every acls/grants rule without its own
        # srcPosture, so it must be known before those are parsed.
        self.p.default_src_posture = self.strlist(
            "defaultSrcPosture", top.get("defaultsrcposture"), required=False)
        if self.p.default_src_posture:
            self.warn("defaultSrcPosture", "not evaluated; acls/grants without their own "
                      "srcPosture are reported as conditional on it (SSH rules are evaluated without it)")
        for i, r in enumerate(self.lst(top, "acls")):
            self.acl(f"acls[{i}]", r)
        for i, r in enumerate(self.lst(top, "grants")):
            self.grant(f"grants[{i}]", r)
        for i, r in enumerate(self.lst(top, "ssh")):
            self.ssh(f"ssh[{i}]", r)
        for i, r in enumerate(self.lst(top, "tests")):
            self.test(f"tests[{i}]", r)
        for i, r in enumerate(self.lst(top, "sshtests")):
            self.ssh_test(f"sshTests[{i}]", r)
        self.check_references()
        if self.errors:
            raise PolicyError(self.errors)
        return self.p

    def lst(self, top, key):
        v = top.get(key, [])
        if v is None:
            return []
        if not isinstance(v, list):
            self.err(key, "must be a list")
            return []
        return v

    def obj(self, path, v):
        if not isinstance(v, dict):
            self.err(path, "must be an object")
            return None
        try:
            return _ci(v)
        except ValueError as e:
            self.err(path, str(e))
            return None

    def strlist(self, path, v, required=True):
        if v is None:
            if required:
                self.err(path, "is required")
            return []
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            self.err(path, "must be a list of strings")
            return []
        if required and not v:
            self.err(path, "must not be empty")
        return [x.strip() for x in v]

    # sections

    def groups(self, v):
        v = self.obj("groups", v)
        for name, members in (v or {}).items():
            path = f"groups[{name!r}]"
            if not _GROUP_RE.match(name):
                self.err(path, "group names must look like group:name")
                continue
            ms = self.strlist(path, members, required=False)
            for m in ms:
                if m.startswith("group:"):
                    self.err(path, f"groups cannot contain groups ({m})")
                elif not is_user(m):
                    self.err(path, f"group member {m!r} is not a user (user@domain)")
            self.p.groups[name] = ms

    def tag_owners(self, v):
        v = self.obj("tagOwners", v)
        for tag, owners in (v or {}).items():
            path = f"tagOwners[{tag!r}]"
            if not _TAG_RE.match(tag):
                self.err(path, "tag names must look like tag:name")
                continue
            os_ = self.strlist(path, owners, required=False)
            for o in os_:
                if o.startswith("group:"):
                    self.used_groups.append((path, o))
                elif o.startswith("tag:"):
                    self.used_tags.append((path, o))
                elif not (is_user(o) or o in ROLE_AUTOGROUPS):
                    self.err(path, f"invalid tag owner {o!r}")
            self.p.tag_owners[tag] = os_

    def hosts(self, v):
        v = self.obj("hosts", v)
        for name, addr in (v or {}).items():
            path = f"hosts[{name!r}]"
            if not _HOST_RE.match(name) or parse_network(name) is not None:
                self.err(path, "host names must be bare names (no '@', ':', or IP)")
                continue
            net = parse_network(addr) if isinstance(addr, str) else None
            if net is None:
                self.err(path, f"value {addr!r} is not an IP address or CIDR")
                continue
            self.p.hosts[name] = net

    def selector(self, path, s, side):
        """Parse an alias. side: 'src' or 'dst' (network rules)."""
        net = parse_network(s)
        if net is not None:
            return Selector("prefix", s, net)
        if s == "*":
            return Selector("wildcard", s)
        if s.startswith("autogroup:"):
            if s in UNSUPPORTED_AUTOGROUPS:
                self.err(path, f"{s} is not supported by this tool")
                return None
            if s not in SUPPORTED_AUTOGROUPS or s == "autogroup:nonroot":
                self.err(path, f"unknown or misplaced autogroup {s!r}")
                return None
            if side == "src" and s in ("autogroup:self", "autogroup:internet"):
                self.err(path, f"{s} is only valid as a destination")
                return None
            if side == "dst" and s == "autogroup:danger-all":
                self.err(path, "autogroup:danger-all is only valid as a source")
                return None
            return Selector("autogroup", s)
        if s.startswith("tag:"):
            if not _TAG_RE.match(s):
                self.err(path, f"invalid tag {s!r}")
                return None
            self.used_tags.append((path, s))
            return Selector("tag", s)
        if s.startswith("group:"):
            self.used_groups.append((path, s))
            return Selector("group", s)
        if s.startswith("ipset:"):
            self.err(path, f"ipsets are not supported by this tool ({s})")
            return None
        if is_user(s):
            return Selector("user", s)
        if ":" not in s and _HOST_RE.match(s):
            self.used_hosts.append((path, s))
            return Selector("host", s)
        self.err(path, f"unrecognised alias {s!r}")
        return None

    def selectors(self, path, items, side):
        out = []
        for j, s in enumerate(items):
            sel = self.selector(f"{path}[{j}]", s, side)
            if sel is not None:
                out.append(sel)
        return out

    def _conditions(self, path, r):
        """Posture conditions of an acls/grants rule: its own srcPosture, or
        else the policy's defaultSrcPosture, which it inherits."""
        if "srcposture" in r:
            self.warn(path, "srcPosture is not evaluated; matches are reported as conditional")
            return [f"srcPosture {r['srcposture']!r}"]
        if self.p.default_src_posture:
            return [f"defaultSrcPosture {self.p.default_src_posture!r}"]
        return []

    def acl(self, path, raw):
        r = self.obj(path, raw)
        if r is None:
            return
        action = r.get("action")
        if action != "accept":
            self.err(f"{path}.action", f"must be \"accept\" (got {action!r}); Tailscale ACLs have no deny")
        # "users"/"ports" are the legacy names of "src"/"dst".
        src_raw = r.get("src", r.get("users"))
        dst_raw = r.get("dst", r.get("ports"))
        for k in r:
            if k not in {"action", "src", "dst", "users", "ports", "proto", "srcposture"}:
                self.warn(f"{path}.{k}", "unknown field, ignored")
        srcs = self.selectors(f"{path}.src", self.strlist(f"{path}.src", src_raw), "src")
        protos = DEFAULT_PROTOS
        if r.get("proto") not in (None, ""):
            try:
                protos = frozenset({parse_proto(r["proto"])})
            except ValueError as e:
                self.err(f"{path}.proto", str(e))
        dsts = []
        for j, d in enumerate(self.strlist(f"{path}.dst", dst_raw)):
            dpath = f"{path}.dst[{j}]"
            try:
                alias, ports_s = split_dst(d)
                ports = parse_ports(ports_s)
            except ValueError as e:
                self.err(dpath, str(e))
                continue
            if ports != PortSet.any() and protos != DEFAULT_PROTOS and not (protos & PORTED_PROTOS):
                self.warn(dpath, f"proto {sorted(protos)[0]} has no ports; a rule with ports other than * never matches it")
            sel = self.selector(dpath, alias, "dst")
            if sel is not None:
                dsts.append(DstEntry(sel, [Cap(protos, ports)]))
        self._self_check(path, srcs, [d.selector for d in dsts])
        self.p.rules.append(NetRule(path, srcs, dsts, self._conditions(path, r)))

    def grant(self, path, raw):
        r = self.obj(path, raw)
        if r is None:
            return
        for k in r:
            if k not in {"src", "dst", "ip", "app", "via", "srcposture"}:
                self.warn(f"{path}.{k}", "unknown field, ignored")
        srcs = self.selectors(f"{path}.src", self.strlist(f"{path}.src", r.get("src")), "src")
        dst_sels = self.selectors(f"{path}.dst", self.strlist(f"{path}.dst", r.get("dst")), "dst")
        if "ip" not in r and "app" not in r:
            self.err(path, "a grant needs \"ip\" and/or \"app\"")
        caps = []
        for j, spec in enumerate(self.strlist(f"{path}.ip", r.get("ip"), required=False)):
            try:
                caps.append(parse_grant_ip(spec))
            except ValueError as e:
                self.err(f"{path}.ip[{j}]", str(e))
        if "app" in r:
            self.warn(f"{path}.app", "application capabilities are not evaluated")
        conds = self._conditions(path, r)
        if r.get("via"):
            via = self.strlist(f"{path}.via", r.get("via"))
            for j, t in enumerate(via):
                if not _TAG_RE.match(t):
                    self.err(f"{path}.via[{j}]", f"via entries must be tags, got {t!r}")
                else:
                    self.used_tags.append((f"{path}.via[{j}]", t))
            conds.append("via " + ",".join(via))
            self.warn(path, "via routing is not modelled; matches are reported as conditional")
        self._self_check(path, srcs, dst_sels)
        if caps:
            self.p.rules.append(NetRule(path, srcs, [DstEntry(s, caps) for s in dst_sels], conds))

    def _self_check(self, path, srcs, dsts):
        if any(d.value == "autogroup:self" for d in dsts):
            bad = [s.value for s in srcs if s.kind not in ("user", "group") and
                   s.value not in ROLE_AUTOGROUPS and s.kind != "wildcard"]
            if bad:
                self.err(path, f"autogroup:self needs user-like sources, not {', '.join(bad)}")

    def ssh(self, path, raw):
        r = self.obj(path, raw)
        if r is None:
            return
        for k in r:
            if k not in {"action", "src", "dst", "users", "checkperiod", "acceptenv", "srcposture"}:
                self.warn(f"{path}.{k}", "unknown field, ignored")
        action = r.get("action")
        if action not in ("accept", "check"):
            self.err(f"{path}.action", f"must be \"accept\" or \"check\" (got {action!r})")
        srcs = []
        for j, s in enumerate(self.strlist(f"{path}.src", r.get("src"))):
            sel = self.selector(f"{path}.src[{j}]", s, "src")
            if sel is None:
                continue
            if sel.kind == "wildcard":
                self.err(f"{path}.src[{j}]", "\"*\" is not allowed as an SSH source")
                continue
            srcs.append(sel)
        dsts = []
        for j, s in enumerate(self.strlist(f"{path}.dst", r.get("dst"))):
            sel = self.selector(f"{path}.dst[{j}]", s, "dst")
            if sel is None:
                continue
            if sel.kind == "wildcard" or sel.value in ("autogroup:internet", "autogroup:danger-all"):
                self.err(f"{path}.dst[{j}]", f"{s!r} is not allowed as an SSH destination")
                continue
            dsts.append(sel)
        # Docs: a user dst is only valid when it is the source user itself;
        # autogroup:self is the general form.
        for d in dsts:
            if d.kind in ("user", "group"):
                if d.kind == "group" or [s.value for s in srcs] != [d.value]:
                    self.err(path, f"SSH dst {d.value} must be the same single user as src (use autogroup:self)")
            if d.value == "autogroup:self" and any(
                    s.kind == "tag" or s.value == "autogroup:tagged" for s in srcs):
                self.err(path, "autogroup:self SSH destination requires user sources")
            if d.value == "autogroup:member" and any(
                    s.kind == "tag" or s.value == "autogroup:tagged" for s in srcs):
                self.err(path, "tags in SSH src cannot reach autogroup:member")
        users = self.strlist(f"{path}.users", r.get("users"))
        for j, u in enumerate(users):
            if not u:
                self.err(f"{path}.users[{j}]", "empty user")
            elif u.startswith("autogroup:") and u != "autogroup:nonroot":
                self.err(f"{path}.users[{j}]", f"{u} is not valid in SSH users")
            elif u.startswith("localpart:") and not re.match(r"^localpart:\*@[^@\s]+$", u):
                self.err(f"{path}.users[{j}]", "localpart entries look like localpart:*@domain")
        cp = r.get("checkperiod")
        if cp is not None:
            if action != "check":
                self.err(f"{path}.checkPeriod", "is only valid with action \"check\"")
            if cp != "always":
                try:
                    secs = parse_duration(str(cp))
                    if secs <= 0 or secs > 7 * 24 * 3600:
                        self.err(f"{path}.checkPeriod", "must be positive and at most 168h")
                except ValueError as e:
                    self.err(f"{path}.checkPeriod", str(e))
        elif action == "check":
            cp = "12h"
        env = self.strlist(f"{path}.acceptEnv", r.get("acceptenv"), required=False)
        if "srcposture" in r:
            self.err(path, "srcPosture on SSH rules is not supported by this tool")
        self.p.ssh.append(SSHRule(path, action or "", srcs, dsts, users, cp, env))

    def test(self, path, raw):
        r = self.obj(path, raw)
        if r is None:
            return
        for k in r:
            if k not in {"src", "proto", "accept", "deny", "srcpostureattrs"}:
                self.warn(f"{path}.{k}", "unknown field, ignored")
        if "srcpostureattrs" in r:
            self.err(path, "srcPostureAttrs in tests is not supported by this tool")
        src = r.get("src")
        if not isinstance(src, str) or not src.strip():
            self.err(f"{path}.src", "must be a single non-empty string")
            src = ""
        proto = None
        if r.get("proto") not in (None, ""):
            try:
                proto = parse_proto(r["proto"])
                if proto not in TEST_PROTOS:
                    self.err(f"{path}.proto", "tests only support tcp, udp, sctp or icmp")
            except ValueError as e:
                self.err(f"{path}.proto", str(e))
        accept = self.strlist(f"{path}.accept", r.get("accept"), required=False)
        deny = self.strlist(f"{path}.deny", r.get("deny"), required=False)
        if not accept and not deny:
            self.err(path, "needs at least one accept or deny entry")
        for j, d in enumerate(accept + deny):
            try:
                _, ports = split_dst(d)
                if proto == "icmp":
                    # ICMP tests name destination port 0.
                    if ports != "0":
                        raise ValueError("icmp test destinations use port 0")
                    continue
                ps = parse_ports(ports)
                if len(ps.ranges) != 1 or ps.ranges[0][0] != ps.ranges[0][1]:
                    raise ValueError("test destinations need exactly one port")
            except ValueError as e:
                self.err(f"{path} {d!r}", str(e))
        self.p.tests.append(NetTest(path, src.strip(), proto, accept, deny))

    def ssh_test(self, path, raw):
        r = self.obj(path, raw)
        if r is None:
            return
        for k in r:
            if k not in {"src", "dst", "accept", "check", "deny"}:
                self.warn(f"{path}.{k}", "unknown field, ignored")
        src = r.get("src")
        if not isinstance(src, str) or not src.strip():
            self.err(f"{path}.src", "must be a single non-empty string")
            src = ""
        dst = self.strlist(f"{path}.dst", r.get("dst"))
        for d in dst:
            if not is_single_ssh_dst(d):
                self.err(f"{path}.dst", f"{d!r} is not a single SSH destination (no ports, CIDRs or autogroup:internet)")
        lists = {k: self.strlist(f"{path}.{k}", r.get(k), required=False)
                 for k in ("accept", "check", "deny")}
        if not any(lists.values()):
            self.err(path, "needs at least one accept, check or deny entry")
        self.p.ssh_tests.append(SSHTest(path, src.strip(), dst, **lists))

    def check_references(self):
        for path, t in self.used_tags:
            if t not in self.p.tag_owners:
                self.err(path, f"tag {t} is not defined in tagOwners")
        for path, g in self.used_groups:
            if g not in self.p.groups:
                self.err(path, f"group {g} is not defined in groups")
        for path, h in self.used_hosts:
            if h not in self.p.hosts:
                self.err(path, f"host {h!r} is not defined in hosts")


def is_single_ssh_dst(d: str) -> bool:
    net = parse_network(d)
    if net is not None:
        return net.num_addresses == 1
    if d == "autogroup:internet":
        return False
    if d.startswith(("tag:", "group:", "autogroup:")):
        return d.count(":") == 1
    return ":" not in d


def parse_grant_ip(spec: str) -> Cap:
    """Grant "ip" entry: '*', 'tcp:443', '443', 'udp:1000-2000', 'icmp:*'."""
    spec = spec.strip()
    if spec == "*":
        return Cap(ANY_PROTO, PortSet.any())
    if ":" not in spec:
        return Cap(DEFAULT_PROTOS, parse_ports(spec))
    proto_s, _, ports_s = spec.partition(":")
    if ":" in ports_s:
        raise ValueError(f"invalid ip entry {spec!r}")
    return Cap(frozenset({parse_proto(proto_s)}), parse_ports(ports_s))


def parse_policy(data) -> Policy:
    return _Parser(data).run()


def load_policy(text: str | bytes) -> Policy:
    try:
        data = hujson.loads(text)
    except hujson.HuJSONError as e:
        raise PolicyError([f"HuJSON: {e}"]) from None
    return parse_policy(data)


def is_tailnet_ip(ip) -> bool:
    return ip in TAILNET_V4 or ip in TAILNET_V6
