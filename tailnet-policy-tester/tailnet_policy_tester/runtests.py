"""Run a policy's own ``tests`` and ``sshTests`` blocks offline.

Semantics (Tailscale policy-syntax docs, "tests" and "sshTests"):

* tests: ``src`` is one alias; every ``accept`` entry ("dst:port") must be
  reachable and every ``deny`` entry must not be. With ``proto`` omitted the
  check passes if TCP or UDP is allowed; otherwise only that protocol. ICMP
  tests name destination port 0.
* sshTests: ``src`` is one alias, ``dst`` a list of hosts; every ``accept``
  login must be allowed (by an accept or check rule), every ``check`` login
  must hit a check rule, every ``deny`` login must hit nothing. Only ``ssh``
  rules are consulted, not the tcp/22 network path.

When an alias resolves to several endpoints (a group, or a tag with several
inventory devices) this tool is strict in both directions: accept needs every
src/dst pair to pass, deny needs every pair to be denied.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .engine import QueryError, check_network, check_ssh, parse_target, resolve
from .policy import PORTED_PROTOS, Policy, split_dst

TEST_DEFAULT_PROTOS = ("tcp", "udp")


@dataclass
class Result:
    origin: str
    passed: bool = True
    failures: list[str] = field(default_factory=list)
    inconclusive: list[str] = field(default_factory=list)

    def fail(self, msg):
        self.passed = False
        self.failures.append(msg)

    @property
    def status(self):
        if not self.passed:
            return "FAIL"
        return "INCONCLUSIVE" if self.inconclusive else "PASS"


def _net_verdict(policy, s, d, protos, port):
    """'allow', 'deny' or 'conditional' for one endpoint pair."""
    verdicts = [check_network(policy, s, d, p, port).verdict for p in protos]
    if "allow" in verdicts:
        return "allow"
    return "conditional" if "conditional" in verdicts else "deny"


def run_net_test(policy: Policy, inv, t) -> Result:
    res = Result(t.origin)
    try:
        srcs = resolve(policy, inv, t.src)
    except QueryError as e:
        res.fail(f"src {t.src!r}: {e}")
        return res
    protos = (t.proto,) if t.proto else TEST_DEFAULT_PROTOS
    for want, entries in (("allow", t.accept), ("deny", t.deny)):
        for entry in entries:
            try:
                if t.proto and t.proto not in PORTED_PROTOS:
                    alias, port = split_dst(entry)[0], None  # icmp: "host:0"
                else:
                    alias, port = parse_target(entry)
                dsts = resolve(policy, inv, alias)
            except QueryError as e:
                res.fail(f"{entry}: {e}")
                continue
            got = {(s.name, d.name): _net_verdict(policy, s, d, protos, port)
                   for s in srcs for d in dsts}
            bad = [k for k, v in got.items() if v != want and v != "conditional"]
            cond = [k for k, v in got.items() if v == "conditional"]
            if bad:
                pairs = ", ".join(f"{a} -> {b}" for a, b in bad)
                res.fail(f"{'accept' if want == 'allow' else 'deny'} {entry}: "
                         f"expected {want}, got {got[bad[0]]} ({pairs})")
            elif cond:
                res.inconclusive.append(f"{entry}: depends on srcPosture/via")
    return res


def run_ssh_test(policy: Policy, inv, t) -> Result:
    res = Result(t.origin)
    try:
        srcs = resolve(policy, inv, t.src)
        dsts = [d for name in t.dst for d in resolve(policy, inv, name)]
    except QueryError as e:
        res.fail(str(e))
        return res
    for kind in ("accept", "check", "deny"):
        for login in getattr(t, kind):
            if not login:
                res.fail(f"{kind}: empty login name")
                continue
            for s in srcs:
                for d in dsts:
                    dec = check_ssh(policy, s, d, login, require_network=False)
                    actions = {a for _, a in dec.matched}
                    ok = {"accept": bool(actions), "check": "check" in actions,
                          "deny": not actions}[kind]
                    if not ok:
                        got = "/".join(sorted(actions)) or "deny"
                        res.fail(f"{kind} {login}: {s.name} -> {d.name} got {got}")
    return res


def run_all(policy: Policy, inv=None) -> list[Result]:
    return ([run_net_test(policy, inv, t) for t in policy.tests] +
            [run_ssh_test(policy, inv, t) for t in policy.ssh_tests])
