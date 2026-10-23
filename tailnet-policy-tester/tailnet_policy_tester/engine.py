"""Evaluate a parsed policy: who can reach what, and who can SSH as whom.

Tailscale compiles a policy into per-device packet filters keyed by IP. Offline
there are no devices, so this evaluator works on *endpoints*: either devices
from an optional inventory file, or synthetic endpoints built from the names in
a query ("alice@example.com" becomes an untagged device owned by alice,
"tag:web" a device carrying that tag, "100.64.0.5" a bare address).

A synthetic endpoint has no IP, so host/CIDR rules never match it; give an
inventory to model those. This is the main way offline answers can differ from
a live tailnet.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field

from . import hujson
from .policy import (
    ANY_PROTO, PORTED_PROTOS, ROLE_AUTOGROUPS, Policy, PolicyError, Selector,
    _merge, is_tailnet_ip, is_user, parse_network, parse_proto, split_dst,
)


class QueryError(ValueError):
    pass


@dataclass(frozen=True)
class Endpoint:
    name: str
    user: str | None = None
    tags: frozenset = frozenset()
    ips: tuple = ()
    roles: frozenset = frozenset()
    synthetic: bool = True

    @property
    def tagged(self) -> bool:
        return bool(self.tags)

    @property
    def owner(self) -> str | None:
        """User identity for policy purposes: tagged devices have none."""
        return None if self.tags else self.user

    @property
    def internet(self) -> bool:
        """A bare address outside the tailnet ranges that is publicly routable."""
        return (self.user is None and not self.tags and bool(self.ips) and
                all(ip.is_global and not is_tailnet_ip(ip) for ip in self.ips))


@dataclass
class Inventory:
    devices: list[Endpoint] = field(default_factory=list)
    roles: dict[str, frozenset] = field(default_factory=dict)

    def by_name(self, name):
        return [d for d in self.devices if d.name == name]

    def by_ip(self, ip):
        return [d for d in self.devices if ip in d.ips]


def load_inventory(text) -> Inventory:
    """Inventory file (HuJSON):

    {"users": {"alice@example.com": {"roles": ["admin"]}},
     "devices": [{"name": "web1", "ips": ["100.64.0.10"], "tags": ["tag:web"]},
                 {"name": "alice-mbp", "ips": ["100.64.0.2"], "user": "alice@example.com"}]}
    """
    try:
        data = hujson.loads(text)
    except hujson.HuJSONError as e:
        raise PolicyError([f"inventory HuJSON: {e}"]) from None
    errors = []
    if not isinstance(data, dict):
        raise PolicyError(["inventory: top level must be an object"])
    roles = {}
    for u, info in (data.get("users") or {}).items():
        rs = (info or {}).get("roles", [])
        if not isinstance(rs, list) or not all(isinstance(r, str) for r in rs):
            errors.append(f"inventory users[{u!r}].roles: must be a list of strings")
            continue
        bad = set(rs) - set(ROLE_AUTOGROUPS.values())
        if bad:
            errors.append(f"inventory users[{u!r}].roles: unknown roles {sorted(bad)}")
        roles[u] = frozenset(rs) | {"member"}
    devices, names = [], set()
    for i, d in enumerate(data.get("devices") or []):
        path = f"inventory devices[{i}]"
        if not isinstance(d, dict) or not isinstance(d.get("name"), str):
            errors.append(f"{path}: needs a string name")
            continue
        if d["name"] in names:
            errors.append(f"{path}: duplicate device name {d['name']!r}")
        names.add(d["name"])
        ips = []
        for s in d.get("ips") or []:
            try:
                ips.append(ipaddress.ip_address(s))
            except (ValueError, TypeError):
                errors.append(f"{path}: bad ip {s!r}")
        tags = d.get("tags") or []
        if not isinstance(tags, list) or not all(isinstance(t, str) and t.startswith("tag:") for t in tags):
            errors.append(f"{path}: tags must be a list of tag:name strings")
            tags = []
        user = d.get("user")
        if user is not None and (not isinstance(user, str) or not is_user(user)):
            errors.append(f"{path}: user must look like user@domain")
            user = None
        if user is None and not tags:
            errors.append(f"{path}: a device needs a user or tags")
        devices.append(Endpoint(d["name"], user, frozenset(tags), tuple(ips),
                                roles.get(user, frozenset({"member"})) if user else frozenset(),
                                synthetic=False))
    if errors:
        raise PolicyError(errors)
    return Inventory(devices, roles)


# --- endpoint resolution -----------------------------------------------------

def resolve(policy: Policy, inv: Inventory | None, name: str) -> list[Endpoint]:
    """Turn a query name into the concrete endpoints it denotes."""
    name = name.strip()
    if not name:
        raise QueryError("empty endpoint")
    if inv is not None and inv.by_name(name):
        return inv.by_name(name)
    if name in policy.hosts:
        net = policy.hosts[name]
        if net.num_addresses != 1:
            raise QueryError(f"host {name!r} is a CIDR ({net}); query one address")
        return _ip_endpoints(inv, net.network_address, name)
    net = parse_network(name)
    if net is not None:
        if net.num_addresses != 1:
            raise QueryError(f"{name} is a CIDR; query one address")
        return _ip_endpoints(inv, net.network_address, name)
    if name.startswith("group:"):
        if name not in policy.groups:
            raise QueryError(f"unknown group {name}")
        out = []
        for u in policy.groups[name]:
            out.extend(resolve(policy, inv, u))
        if not out:
            raise QueryError(f"{name} has no members")
        return out
    if name.startswith("tag:"):
        if name not in policy.tag_owners:
            raise QueryError(f"tag {name} is not defined in tagOwners")
        if inv is not None:
            found = [d for d in inv.devices if name in d.tags]
            if found:
                return found
        return [Endpoint(name, tags=frozenset({name}))]
    if is_user(name):
        if inv is not None:
            found = [d for d in inv.devices if d.owner == name]
            if found:
                return found
        roles = inv.roles.get(name, frozenset({"member"})) if inv else frozenset({"member"})
        return [Endpoint(name, user=name, roles=roles)]
    if name.startswith("autogroup:"):
        raise QueryError(f"{name} is a set, not an endpoint; query a user, tag, host or IP")
    raise QueryError(f"cannot resolve {name!r} (not a device, host, IP, user, group or tag)")


def _ip_endpoints(inv, ip, label):
    if inv is not None:
        found = inv.by_ip(ip)
        if found:
            return found
    return [Endpoint(label if label != str(ip) else str(ip), ips=(ip,))]


# --- matching ----------------------------------------------------------------

def matches(policy: Policy, sel: Selector, ep: Endpoint, src: Endpoint | None = None) -> bool:
    """Does selector ``sel`` cover endpoint ``ep``? ``src`` is set on the dst
    side so that autogroup:self can compare owners."""
    k, v = sel.kind, sel.value
    if k == "wildcard":
        # "*" as a destination is every tailnet device and route, but not the
        # public internet via an exit node (that needs autogroup:internet).
        return src is None or not ep.internet
    if k in ("prefix", "host"):
        net = sel.network if k == "prefix" else policy.hosts[v]
        return any(ip.version == net.version and ip in net for ip in ep.ips)
    if k == "user":
        return ep.owner == v
    if k == "group":
        return ep.owner is not None and ep.owner in policy.groups.get(v, ())
    if k == "tag":
        return v in ep.tags
    if k == "autogroup":
        if v == "autogroup:tagged":
            return ep.tagged
        if v == "autogroup:danger-all":
            return True
        if v == "autogroup:internet":
            return ep.internet
        if v == "autogroup:self":
            return src is not None and src.owner is not None and ep.owner == src.owner
        if v in ROLE_AUTOGROUPS:
            return ep.owner is not None and ROLE_AUTOGROUPS[v] in ep.roles
    return False


@dataclass
class Decision:
    allowed: bool
    conditional: bool  # allowed only through rules with srcPosture/via
    matched: list[str]  # origins of matching rules
    conditions: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if self.allowed:
            return "allow"
        return "conditional" if self.conditional else "deny"


def check_network(policy: Policy, src: Endpoint, dst: Endpoint, proto: str,
                  port: int | None) -> Decision:
    if proto in PORTED_PROTOS and port is None:
        raise QueryError(f"{proto} needs a port")
    matched, cond_matched, conds = [], [], []
    for rule in policy.rules:
        if not any(matches(policy, s, src) for s in rule.srcs):
            continue
        hit = any(matches(policy, d.selector, dst, src) and
                  any(c.allows(proto, port) for c in d.caps) for d in rule.dsts)
        if hit:
            if rule.conditions:
                cond_matched.append(rule.origin)
                conds.extend(rule.conditions)
            else:
                matched.append(rule.origin)
    if matched:
        return Decision(True, False, matched)
    return Decision(False, bool(cond_matched), cond_matched, conds)


def allowed_ports(policy: Policy, src: Endpoint, dst: Endpoint, proto: str):
    """Ports (as merged ranges) on which src may reach dst over proto, plus a
    the ranges reachable only through posture/via-conditional rules.
    Portless protos return ((0, 65535),) when allowed at all."""
    ranges, cond_ranges = [], []
    for rule in policy.rules:
        if not any(matches(policy, s, src) for s in rule.srcs):
            continue
        for d in rule.dsts:
            if not matches(policy, d.selector, dst, src):
                continue
            for c in d.caps:
                if c.protos != ANY_PROTO and proto not in c.protos:
                    continue
                if proto in PORTED_PROTOS:
                    rs = c.ports.ranges
                elif c.allows(proto, None):
                    rs = ((0, 65535),)
                else:
                    continue
                (cond_ranges if rule.conditions else ranges).extend(rs)
    return _merge(ranges), _merge(cond_ranges)


# --- SSH -----------------------------------------------------------------------

@dataclass
class SSHDecision:
    action: str | None  # "accept", "check" or None (denied)
    matched: list[tuple[str, str]]  # (origin, action) of every matching rule
    check_period: str | None = None
    network: Decision | None = None

    @property
    def verdict(self) -> str:
        if self.action is None:
            return "deny"
        if self.network is not None and self.network.verdict != "allow":
            return "deny (no network path on tcp/22)"
        return self.action


def ssh_user_allowed(users: list[str], login: str, src: Endpoint) -> bool:
    if not login:
        return False
    if login == "root":
        if "root" in users:
            return True
    elif "autogroup:nonroot" in users or login in users:
        return True
    for u in users:
        if u.startswith("localpart:*@") and src.owner:
            local, _, domain = src.owner.partition("@")
            if domain.lower() == u[len("localpart:*@"):].lower() and local == login:
                return True
    return False


def check_ssh(policy: Policy, src: Endpoint, dst: Endpoint, login: str,
              require_network: bool = True) -> SSHDecision:
    """Rules are scanned in policy order and the first match decides the action
    (tailscaled evaluates the compiled SSHPolicy first-match); every match is
    listed. With require_network, tcp/22 must also be allowed by acls/grants."""
    hits = []
    for rule in policy.ssh:
        if not any(matches(policy, s, src) for s in rule.srcs):
            continue
        if not any(matches(policy, d, dst, src) for d in rule.dsts):
            continue
        if ssh_user_allowed(rule.users, login, src):
            hits.append(rule)
    dec = SSHDecision(hits[0].action if hits else None,
                      [(r.origin, r.action) for r in hits],
                      hits[0].check_period if hits else None)
    if require_network and hits:
        dec.network = check_network(policy, src, dst, "tcp", 22)
    return dec


def parse_target(s: str) -> tuple[str, int | None]:
    """'host:port', '[v6]:port', or a bare name/IP (for portless protocols)."""
    s = s.strip()
    if s.startswith("["):
        try:
            alias, port = split_dst(s)
        except ValueError as e:
            raise QueryError(str(e)) from None
    elif parse_network(s) is not None:
        return s, None
    else:
        alias, sep, port = s.rpartition(":")
        if not sep or not alias or not port.isdigit():
            if port == "*" or "," in port or "-" in port:
                raise QueryError(f"query {s!r} with exactly one port")
            return s, None
    if not port.isdigit() or not 0 < int(port) <= 65535:
        raise QueryError(f"query {s!r} with exactly one port (1-65535)")
    return alias, int(port)


def norm_proto(p: str) -> str:
    try:
        return parse_proto(p)
    except ValueError as e:
        raise QueryError(str(e)) from None
