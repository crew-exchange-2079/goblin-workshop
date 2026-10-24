"""Diff two policies: which flows and SSH logins become allowed or denied.

The comparison runs over a finite universe of endpoints: the inventory's
devices when one is given, otherwise one synthetic endpoint per user, tag and
single-address host named in either policy, plus representatives for "some
other member", the first address of every CIDR, and a public address when
autogroup:internet is used. Ports are compared exactly as interval sets, so
nothing is sampled; endpoints are, which is the main limit of a diff without
an inventory.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from .engine import Endpoint, allowed_ports, check_ssh
from .policy import ANY_PROTO, PORTED_PROTOS, ROLE_AUTOGROUPS, Policy

OTHER_USER = "unlisted-user@example.invalid"
OTHER_LOGIN = "<other-nonroot>"


@dataclass
class Change:
    kind: str  # "net" | "ssh"
    src: str
    dst: str
    detail: str
    direction: str  # "allowed" | "denied" | "changed"

    def __str__(self):
        sign = {"allowed": "+", "denied": "-", "changed": "~"}[self.direction]
        return f"{sign} {self.kind} {self.src} -> {self.dst} {self.detail}"


def subtract(a, b):
    """Interval-set difference a - b over merged inclusive ranges."""
    out = []
    for lo, hi in a:
        cur = lo
        for blo, bhi in b:
            if bhi < cur or blo > hi:
                continue
            if blo > cur:
                out.append((cur, blo - 1))
            cur = max(cur, bhi + 1)
            if cur > hi:
                break
        if cur <= hi:
            out.append((cur, hi))
    return tuple(out)


def fmt_ranges(rs):
    if rs == ((0, 65535),):
        return "*"
    return ",".join(str(lo) if lo == hi else f"{lo}-{hi}" for lo, hi in rs)


def _users(p: Policy):
    us = set()
    for ms in p.groups.values():
        us.update(ms)
    for r in p.rules:
        us.update(s.value for s in r.srcs if s.kind == "user")
        us.update(d.selector.value for d in r.dsts if d.selector.kind == "user")
    for r in p.ssh:
        us.update(s.value for s in r.srcs + r.dsts if s.kind == "user")
    return us


def _uses(p: Policy, value):
    return any(s.value == value for r in p.rules for s in r.srcs + [d.selector for d in r.dsts])


def _universe(old: Policy, new: Policy):
    eps = {}
    for p in (old, new):
        for u in _users(p):
            eps[u] = Endpoint(u, user=u, roles=frozenset({"member"}))
        for t in p.tag_owners:
            eps[t] = Endpoint(t, tags=frozenset({t}))
        nets = list(p.hosts.items())
        for r in p.rules:
            nets += [(s.value, s.network) for s in r.srcs + [d.selector for d in r.dsts]
                     if s.kind == "prefix"]
        for label, net in nets:
            ip = net.network_address if net.num_addresses == 1 else next(net.hosts(), net.network_address)
            eps.setdefault(str(ip), Endpoint(label if net.num_addresses == 1 else str(ip), ips=(ip,)))
    eps[OTHER_USER] = Endpoint(OTHER_USER, user=OTHER_USER, roles=frozenset({"member"}))
    if _uses(old, "autogroup:internet") or _uses(new, "autogroup:internet"):
        # Documentation ranges are not is_global, so use a real public address.
        ip = ipaddress.ip_address("1.1.1.1")
        eps[str(ip)] = Endpoint("internet(1.1.1.1)", ips=(ip,))
    return list(eps.values())


def _protos(old: Policy, new: Policy):
    ps = {"tcp", "udp", "sctp", "icmp", "ipv6-icmp"}
    for p in (old, new):
        for r in p.rules:
            for d in r.dsts:
                for c in d.caps:
                    if c.protos != ANY_PROTO:
                        ps.update(c.protos)
    return sorted(ps)


def _logins(old: Policy, new: Policy):
    ls = {"root", OTHER_LOGIN}
    for p in (old, new):
        for r in p.ssh:
            ls.update(u for u in r.users if u not in ("autogroup:nonroot", "root")
                      and not u.startswith("localpart:"))
    return sorted(ls)


def diff(old: Policy, new: Policy, endpoints=None) -> tuple[list[Change], list[str]]:
    notes = []
    if endpoints is None:
        endpoints = _universe(old, new)
        if any(s.value in ROLE_AUTOGROUPS and s.value != "autogroup:member"
               for p in (old, new) for r in p.rules + p.ssh
               for s in r.srcs):
            notes.append("role autogroups (admin, owner, ...) need an inventory with user roles to be diffed")
    changes = []
    protos = _protos(old, new)
    for s in endpoints:
        for d in endpoints:
            if s is d:
                continue
            for proto in protos:
                o, oc = allowed_ports(old, s, d, proto)
                n, nc = allowed_ports(new, s, d, proto)
                label = proto if proto not in PORTED_PROTOS else None
                for rs, direction, tag in ((subtract(n, o), "allowed", ""),
                                           (subtract(o, n), "denied", ""),
                                           (subtract(subtract(nc, n), subtract(oc, o)), "allowed", " (conditional)"),
                                           (subtract(subtract(oc, o), subtract(nc, n)), "denied", " (conditional)")):
                    if rs:
                        detail = label if label else f"{proto}:{fmt_ranges(rs)}"
                        changes.append(Change("net", s.name, d.name, detail + tag, direction))
            for login in _logins(old, new):
                a = check_ssh(old, s, d, login, require_network=False).action
                b = check_ssh(new, s, d, login, require_network=False).action
                if a != b:
                    direction = "allowed" if a is None else "denied" if b is None else "changed"
                    changes.append(Change("ssh", s.name, d.name,
                                          f"as {login}: {a or 'deny'} -> {b or 'deny'}", direction))
    # localpart: logins depend on the source user's name.
    for s in endpoints:
        if not s.owner:
            continue
        lp = s.owner.partition("@")[0]
        if lp in _logins(old, new) or lp == "root":
            continue
        for d in endpoints:
            if s is d:
                continue
            a = check_ssh(old, s, d, lp, require_network=False).action
            b = check_ssh(new, s, d, lp, require_network=False).action
            ao = check_ssh(old, s, d, OTHER_LOGIN, require_network=False).action
            bo = check_ssh(new, s, d, OTHER_LOGIN, require_network=False).action
            if (a, b) != (ao, bo) and a != b:
                direction = "allowed" if a is None else "denied" if b is None else "changed"
                changes.append(Change("ssh", s.name, d.name,
                                      f"as {lp} (localpart): {a or 'deny'} -> {b or 'deny'}", direction))
    changes.sort(key=lambda c: (c.src, c.dst, c.kind, c.detail))
    return changes, notes

