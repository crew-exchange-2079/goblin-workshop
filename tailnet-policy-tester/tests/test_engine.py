import ipaddress

import pytest

from conftest import INVENTORY, mk
from tailnet_policy_tester.engine import (
    Endpoint, QueryError, check_network, check_ssh, load_inventory,
    parse_target, resolve, ssh_user_allowed,
)
from tailnet_policy_tester.policy import PolicyError, load_policy

ALICE = Endpoint("alice", user="alice@example.com", roles=frozenset({"member"}))
BOB = Endpoint("bob", user="bob@example.com", roles=frozenset({"member"}))
WEB = Endpoint("web", tags=frozenset({"tag:web"}))
DB = Endpoint("db", tags=frozenset({"tag:db"}))


def ip(s, name=None):
    return Endpoint(name or s, ips=(ipaddress.ip_address(s),))


def acl(src, dst, **kw):
    return {"action": "accept", "src": src, "dst": dst, **kw}


def verdict(p, s, d, proto="tcp", port=443):
    return check_network(p, s, d, proto, port).verdict


def test_default_deny():
    assert verdict(mk(), ALICE, WEB) == "deny"


def test_user_and_group():
    p = mk(groups={"group:eng": ["alice@example.com"]},
           acls=[acl(["group:eng"], ["tag:web:443"]), acl(["bob@example.com"], ["tag:db:5432"])])
    assert verdict(p, ALICE, WEB) == "allow"
    assert verdict(p, BOB, WEB) == "deny"
    assert verdict(p, BOB, DB, port=5432) == "allow"


def test_tagged_device_loses_user_identity():
    p = mk(acls=[acl(["alice@example.com"], ["*:*"])])
    tagged_alice = Endpoint("ci", user="alice@example.com", tags=frozenset({"tag:ci"}))
    assert verdict(p, tagged_alice, WEB) == "deny"
    assert verdict(p, ALICE, WEB) == "allow"


def test_member_vs_tagged():
    p = mk(acls=[acl(["autogroup:member"], ["tag:db:1"]), acl(["autogroup:tagged"], ["tag:web:2"])])
    assert verdict(p, ALICE, DB, port=1) == "allow" and verdict(p, WEB, DB, port=1) == "deny"
    assert verdict(p, WEB, WEB, port=2) == "allow" and verdict(p, ALICE, WEB, port=2) == "deny"


def test_autogroup_self():
    p = mk(acls=[acl(["autogroup:member"], ["autogroup:self:*"])])
    alice2 = Endpoint("alice-phone", user="alice@example.com")
    assert verdict(p, ALICE, alice2) == "allow"
    assert verdict(p, ALICE, BOB) == "deny"
    assert verdict(p, ALICE, WEB) == "deny"


def test_ports_and_ranges():
    p = mk(acls=[acl(["*"], ["tag:web:80,8000-8100"])])
    assert verdict(p, ALICE, WEB, port=80) == "allow"
    assert verdict(p, ALICE, WEB, port=8050) == "allow"
    assert verdict(p, ALICE, WEB, port=443) == "deny"


def test_proto_restriction():
    p = mk(acls=[acl(["*"], ["tag:web:53"], proto="udp")])
    assert verdict(p, ALICE, WEB, "udp", 53) == "allow"
    assert verdict(p, ALICE, WEB, "tcp", 53) == "deny"


def test_default_protos_exclude_sctp_and_gre():
    p = mk(acls=[acl(["*"], ["tag:web:*"])])
    assert verdict(p, ALICE, WEB, "udp", 9) == "allow"
    assert verdict(p, ALICE, WEB, "sctp", 9) == "deny"
    assert verdict(p, ALICE, WEB, "gre", None) == "deny"


def test_icmp_needs_all_ports():
    p = mk(acls=[acl(["*"], ["tag:web:22"]), acl(["*"], ["tag:db:*"])])
    assert verdict(p, ALICE, WEB, "icmp", None) == "deny"
    assert verdict(p, ALICE, DB, "icmp", None) == "allow"


def test_grant_any_proto():
    p = mk(grants=[{"src": ["*"], "dst": ["tag:web"], "ip": ["*"]}])
    assert verdict(p, ALICE, WEB, "gre", None) == "allow"
    assert verdict(p, ALICE, WEB, "sctp", 1) == "allow"


def test_grant_specific():
    p = mk(grants=[{"src": ["*"], "dst": ["tag:web"], "ip": ["tcp:443", "udp:3478"]}])
    assert verdict(p, ALICE, WEB, "tcp", 443) == "allow"
    assert verdict(p, ALICE, WEB, "udp", 443) == "deny"
    assert verdict(p, ALICE, WEB, "udp", 3478) == "allow"


def test_acls_and_grants_union():
    p = mk(acls=[acl(["*"], ["tag:web:80"])],
           grants=[{"src": ["*"], "dst": ["tag:web"], "ip": ["443"]}])
    d = check_network(p, ALICE, WEB, "tcp", 80)
    assert d.matched == ["acls[0]"]
    assert check_network(p, ALICE, WEB, "tcp", 443).matched == ["grants[0]"]


def test_matched_lists_every_rule():
    p = mk(acls=[acl(["*"], ["tag:web:443"]), acl(["alice@example.com"], ["tag:web:*"])])
    assert check_network(p, ALICE, WEB, "tcp", 443).matched == ["acls[0]", "acls[1]"]


def test_hosts_and_cidrs_match_ips_only():
    p = mk(hosts={"nas": "100.64.0.50", "office": "192.168.10.0/24"},
           acls=[acl(["*"], ["nas:445", "office:*", "fd7a:115c:a1e0::/48:22"])])
    assert verdict(p, ALICE, ip("100.64.0.50"), port=445) == "allow"
    assert verdict(p, ALICE, ip("192.168.10.7"), port=1) == "allow"
    assert verdict(p, ALICE, ip("192.168.11.7"), port=1) == "deny"
    assert verdict(p, ALICE, ip("fd7a:115c:a1e0::9"), port=22) == "allow"
    assert verdict(p, ALICE, WEB, port=445) == "deny"  # synthetic endpoint has no IP


def test_wildcard_dst_excludes_internet():
    p = mk(acls=[acl(["*"], ["*:*"])])
    assert verdict(p, ALICE, ip("100.64.0.9")) == "allow"
    assert verdict(p, ALICE, ip("10.1.2.3")) == "allow"  # private: a subnet route
    assert verdict(p, ALICE, ip("1.1.1.1")) == "deny"


def test_autogroup_internet():
    p = mk(acls=[acl(["autogroup:member"], ["autogroup:internet:*"])])
    assert verdict(p, ALICE, ip("1.1.1.1")) == "allow"
    assert verdict(p, ALICE, ip("100.64.0.9")) == "deny"
    assert verdict(p, WEB, ip("1.1.1.1")) == "deny"


def test_danger_all_source():
    p = mk(acls=[acl(["autogroup:danger-all"], ["tag:web:443"])])
    assert verdict(p, ip("203.0.113.9"), WEB) == "allow"


def test_posture_and_via_are_conditional():
    p = mk(acls=[acl(["*"], ["tag:web:443"], srcPosture=["posture:latest"])],
           grants=[{"src": ["*"], "dst": ["10.0.0.0/8"], "ip": ["*"], "via": ["tag:router"]}])
    d = check_network(p, ALICE, WEB, "tcp", 443)
    assert d.verdict == "conditional" and "srcPosture" in d.conditions[0]
    assert verdict(p, ALICE, ip("10.0.0.1")) == "conditional"


def test_unconditional_beats_conditional():
    p = mk(acls=[acl(["*"], ["tag:web:443"], srcPosture=["posture:x"]), acl(["*"], ["tag:web:443"])])
    assert verdict(p, ALICE, WEB) == "allow"


def test_role_autogroups_need_roles():
    p = mk(acls=[acl(["autogroup:admin"], ["tag:db:22"])])
    admin = Endpoint("carol", user="carol@example.com", roles=frozenset({"member", "admin"}))
    assert verdict(p, admin, DB, port=22) == "allow"
    assert verdict(p, ALICE, DB, port=22) == "deny"


def test_port_required_for_tcp():
    with pytest.raises(QueryError):
        check_network(mk(), ALICE, WEB, "tcp", None)


@pytest.mark.parametrize("s,expected", [
    ("tag:web:443", ("tag:web", 443)),
    ("tag:web", ("tag:web", None)),
    ("100.64.0.1:22", ("100.64.0.1", 22)),
    ("100.64.0.1", ("100.64.0.1", None)),
    ("fd7a::1", ("fd7a::1", None)),
    ("[fd7a::1]:22", ("fd7a::1", 22)),
    ("alice@example.com:22", ("alice@example.com", 22)),
])
def test_parse_target(s, expected):
    assert parse_target(s) == expected


@pytest.mark.parametrize("s", ["tag:web:*", "tag:web:80,443", "tag:web:1-2", "tag:web:0", "tag:web:70000"])
def test_parse_target_needs_one_port(s):
    with pytest.raises(QueryError):
        parse_target(s)


# --- resolution and inventory ----------------------------------------------

def test_resolve_synthetic():
    p = mk(groups={"group:eng": ["a@x.y", "b@x.y"]}, hosts={"nas": "100.64.0.50", "lan": "10.0.0.0/8"})
    assert [e.owner for e in resolve(p, None, "group:eng")] == ["a@x.y", "b@x.y"]
    assert resolve(p, None, "tag:web")[0].tags == {"tag:web"}
    assert resolve(p, None, "nas")[0].ips == (ipaddress.ip_address("100.64.0.50"),)
    for bad in ("lan", "10.0.0.0/8", "tag:nope", "group:nope", "autogroup:member", "what"):
        with pytest.raises(QueryError):
            resolve(p, None, bad)


def test_inventory_resolution():
    inv = load_inventory(INVENTORY.read_bytes())
    p = load_policy((INVENTORY.parent / "policy.hujson").read_text())
    assert [d.name for d in resolve(p, inv, "tag:web")] == ["web1", "web2"]
    assert [d.name for d in resolve(p, inv, "100.64.0.20")] == ["db1"]
    assert [d.name for d in resolve(p, inv, "fd7a:115c:a1e0::10")] == ["web1"]
    assert resolve(p, inv, "carol@example.com")[0].roles >= {"admin"}
    assert resolve(p, inv, "100.64.0.99")[0].synthetic


def test_inventory_validation():
    with pytest.raises(PolicyError) as e:
        load_inventory(b'{"devices": [{"name": "a"}, {"name": "a", "user": "x", "ips": ["nope"]}],'
                       b' "users": {"u@x.y": {"roles": ["wizard"]}}}')
    msg = " ".join(e.value.errors)
    assert "needs a user or tags" in msg and "duplicate" in msg and "bad ip" in msg and "wizard" in msg


def test_inventory_ip_rules_apply_to_tags():
    inv = load_inventory(INVENTORY.read_bytes())
    p = mk(acls=[acl(["100.64.0.2"], ["100.64.0.20:5432"])])
    alice = resolve(p, inv, "alice@example.com")[0]
    db = resolve(p, inv, "tag:db")[0]
    assert verdict(p, alice, db, port=5432) == "allow"


# --- SSH -------------------------------------------------------------------

def ssh(**kw):
    base = {"action": "accept", "src": ["autogroup:member"], "dst": ["tag:web"], "users": ["root"]}
    base.update(kw)
    return base


@pytest.mark.parametrize("users,login,ok", [
    (["root"], "root", True),
    (["root"], "alice", False),
    (["autogroup:nonroot"], "deploy", True),
    (["autogroup:nonroot"], "root", False),
    (["deploy"], "deploy", True),
    (["deploy"], "other", False),
    (["localpart:*@example.com"], "alice", True),
    (["localpart:*@example.com"], "bob", False),
    (["localpart:*@other.org"], "alice", False),
    (["root"], "", False),
])
def test_ssh_users(users, login, ok):
    assert ssh_user_allowed(users, login, ALICE) is ok


def test_localpart_needs_user_source():
    assert not ssh_user_allowed(["localpart:*@example.com"], "web", WEB)


def test_ssh_first_match_decides_action():
    p = mk(ssh=[ssh(action="check"), ssh(action="accept")])
    d = check_ssh(p, ALICE, WEB, "root", require_network=False)
    assert d.action == "check" and [a for _, a in d.matched] == ["check", "accept"]
    assert d.check_period == "12h"


def test_ssh_requires_network_path():
    p = mk(ssh=[ssh()])
    assert check_ssh(p, ALICE, WEB, "root").verdict.startswith("deny (no network")
    assert check_ssh(p, ALICE, WEB, "root", require_network=False).verdict == "accept"
    p2 = mk(ssh=[ssh()], acls=[acl(["*"], ["tag:web:22"])])
    assert check_ssh(p2, ALICE, WEB, "root").verdict == "accept"


def test_ssh_self_destination():
    p = mk(ssh=[ssh(dst=["autogroup:self"], users=["autogroup:nonroot"])])
    alice2 = Endpoint("alice-phone", user="alice@example.com")
    assert check_ssh(p, ALICE, alice2, "alice", require_network=False).action == "accept"
    assert check_ssh(p, ALICE, BOB, "alice", require_network=False).action is None


def test_ssh_denied_without_rule():
    assert check_ssh(mk(), ALICE, WEB, "root").verdict == "deny"
