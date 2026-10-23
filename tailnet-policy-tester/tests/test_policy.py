import pytest

from conftest import mk
from tailnet_policy_tester.policy import (
    ANY_PROTO, DEFAULT_PROTOS, PolicyError, PortSet, load_policy, parse_grant_ip,
    parse_duration, parse_ports, parse_proto, split_dst,
)


def errors(**sections):
    with pytest.raises(PolicyError) as e:
        mk(**sections)
    return " | ".join(e.value.errors)


def acl(src, dst, **kw):
    return {"action": "accept", "src": src, "dst": dst, **kw}


def test_example_parses(example_text):
    p = load_policy(example_text)
    assert len(p.rules) == 5 and len(p.ssh) == 2 and not p.warnings


def test_keys_are_case_insensitive():
    p = load_policy('{"TagOwners": {"tag:web": []}, "ACLs": [{"Action": "accept", "Src": ["*"], "Dst": ["tag:web:80"]}]}')
    assert p.rules[0].dsts[0].selector.value == "tag:web"


def test_case_only_duplicate_keys_rejected():
    with pytest.raises(PolicyError, match="ignoring case"):
        load_policy('{"acls": [], "ACLs": []}')


def test_legacy_users_ports_fields():
    p = mk(acls=[{"action": "accept", "users": ["*"], "ports": ["tag:web:22"]}])
    assert p.rules[0].srcs[0].kind == "wildcard"


def test_deny_action_rejected():
    assert "no deny" in errors(acls=[{"action": "deny", "src": ["*"], "dst": ["*:*"]}])


def test_undefined_tag_group_host():
    e = errors(acls=[acl(["group:nope"], ["tag:nope:22", "nohost:80"])])
    assert "group:nope" in e and "tag:nope" in e and "'nohost'" in e


def test_nested_groups_rejected():
    assert "cannot contain groups" in errors(groups={"group:a": ["x@y.z"], "group:b": ["group:a"]})


def test_group_member_must_be_user():
    assert "not a user" in errors(groups={"group:a": ["tag:web"]})


@pytest.mark.parametrize("spec,expected", [
    ("*", ((0, 65535),)),
    ("22", ((22, 22),)),
    ("80,443", ((80, 80), (443, 443))),
    ("1000-2000,1500-2500", ((1000, 2500),)),
    ("22,23", ((22, 23),)),
])
def test_parse_ports(spec, expected):
    assert parse_ports(spec).ranges == expected


@pytest.mark.parametrize("spec", ["", "0", "70000", "a", "20-10", "1-2-3", "22,"])
def test_parse_ports_rejects(spec):
    with pytest.raises(ValueError):
        parse_ports(spec)


@pytest.mark.parametrize("raw,name", [("tcp", "tcp"), ("TCP", "tcp"), ("6", "tcp"),
                                      ("17", "udp"), ("58", "ipv6-icmp"), ("99", "proto-99")])
def test_parse_proto(raw, name):
    assert parse_proto(raw) == name


@pytest.mark.parametrize("raw", ["nope", "256", "-1"])
def test_parse_proto_rejects(raw):
    with pytest.raises(ValueError):
        parse_proto(raw)


@pytest.mark.parametrize("dst,alias,ports", [
    ("tag:web:443", "tag:web", "443"),
    ("100.64.0.1:22", "100.64.0.1", "22"),
    ("[fd7a:115c:a1e0::1]:22", "fd7a:115c:a1e0::1", "22"),
    ("fd7a:115c:a1e0::1:22", "fd7a:115c:a1e0::1", "22"),
    ("10.0.0.0/8:*", "10.0.0.0/8", "*"),
    ("*:*", "*", "*"),
])
def test_split_dst(dst, alias, ports):
    assert split_dst(dst) == (alias, ports)


def test_split_dst_needs_ports():
    with pytest.raises(ValueError):
        split_dst("tag")


def test_proto_number_in_acl():
    p = mk(acls=[acl(["*"], ["tag:web:53"], proto="17")])
    assert p.rules[0].dsts[0].caps[0].protos == frozenset({"udp"})


def test_default_protos():
    p = mk(acls=[acl(["*"], ["tag:web:80"])])
    assert p.rules[0].dsts[0].caps[0].protos == DEFAULT_PROTOS


def test_portless_proto_with_ports_warns():
    p = mk(acls=[acl(["*"], ["tag:web:22"], proto="icmp")])
    assert any("no ports" in w for w in p.warnings)


@pytest.mark.parametrize("src,msg", [
    ("autogroup:self", "only valid as a destination"),
    ("autogroup:internet", "only valid as a destination"),
    ("autogroup:shared", "not supported"),
    ("autogroup:bogus", "unknown"),
    ("ipset:corp", "ipsets"),
])
def test_bad_sources(src, msg):
    assert msg in errors(acls=[acl([src], ["tag:web:80"])])


def test_danger_all_not_a_destination():
    assert "only valid as a source" in errors(acls=[acl(["*"], ["autogroup:danger-all:*"])])


def test_self_needs_user_sources():
    assert "user-like sources" in errors(acls=[acl(["tag:web"], ["autogroup:self:*"])])


def test_hosts_validation():
    assert "not an IP" in errors(hosts={"nas": "nas.local"})
    assert "bare names" in errors(hosts={"10.0.0.1": "10.0.0.1"})
    assert "not an IP" in errors(hosts={"net": "10.0.0.1/8"})  # host bits set


def test_grant_requires_ip_or_app():
    assert "needs" in errors(grants=[{"src": ["*"], "dst": ["*"]}])


def test_app_only_grant_has_no_network_rule():
    p = mk(grants=[{"src": ["*"], "dst": ["tag:web"], "app": {"example.com/cap/x": [{}]}}])
    assert p.rules == [] and any("not evaluated" in w for w in p.warnings)


@pytest.mark.parametrize("spec,protos,ports", [
    ("*", ANY_PROTO, "*"),
    ("tcp:443", frozenset({"tcp"}), "443"),
    ("443", DEFAULT_PROTOS, "443"),
    ("udp:1000-2000", frozenset({"udp"}), "1000-2000"),
    ("icmp:*", frozenset({"icmp"}), "*"),
])
def test_grant_ip(spec, protos, ports):
    cap = parse_grant_ip(spec)
    assert cap.protos == protos and str(cap.ports) == ports


def test_grant_via_is_conditional_and_tag_checked():
    p = mk(grants=[{"src": ["*"], "dst": ["10.0.0.0/8"], "ip": ["*"], "via": ["tag:router"]}])
    assert p.rules[0].conditions == ["via tag:router"]
    assert "via entries must be tags" in errors(grants=[{"src": ["*"], "dst": ["*"], "ip": ["*"], "via": ["x@y.z"]}])


def test_unknown_and_ignored_sections_warn():
    p = mk(nodeAttrs=[], frobnicate=1)
    ws = " ".join(p.warnings)
    assert "nodeattrs: section is not evaluated" in ws and "frobnicate: unknown section" in ws


def ssh(**kw):
    base = {"action": "accept", "src": ["autogroup:member"], "dst": ["autogroup:self"], "users": ["root"]}
    base.update(kw)
    return base


def test_ssh_star_source_rejected():
    assert "not allowed as an SSH source" in errors(ssh=[ssh(src=["*"])])


def test_ssh_user_dst_must_match_src():
    assert "same single user" in errors(ssh=[ssh(src=["a@x.y"], dst=["b@x.y"])])
    mk(ssh=[ssh(src=["a@x.y"], dst=["a@x.y"])])


def test_ssh_tag_to_member_rejected():
    assert "cannot reach autogroup:member" in errors(ssh=[ssh(src=["tag:ci"], dst=["autogroup:member"])])


def test_ssh_self_needs_user_src():
    assert "requires user sources" in errors(ssh=[ssh(src=["tag:ci"], dst=["autogroup:self"])])


def test_ssh_check_period():
    p = mk(ssh=[ssh(action="check")])
    assert p.ssh[0].check_period == "12h"
    assert mk(ssh=[ssh(action="check", checkPeriod="always")]).ssh[0].check_period == "always"
    assert "only valid with action" in errors(ssh=[ssh(checkPeriod="1h")])
    assert "at most 168h" in errors(ssh=[ssh(action="check", checkPeriod="169h")])
    assert "invalid duration" in errors(ssh=[ssh(action="check", checkPeriod="1 day")])


def test_parse_duration():
    assert parse_duration("1h30m") == 5400 and parse_duration("90s") == 90


def test_ssh_users_validation():
    assert "not valid in SSH users" in errors(ssh=[ssh(users=["autogroup:member"])])
    assert "localpart:*@domain" in errors(ssh=[ssh(users=["localpart:alice"])])


def test_tests_block_shapes():
    assert "tcp, udp or sctp" in errors(tests=[{"src": "a@x.y", "proto": "icmp", "accept": ["tag:web:1"]}])
    assert "exactly one port" in errors(tests=[{"src": "a@x.y", "accept": ["tag:web:80,443"]}])
    assert "at least one" in errors(tests=[{"src": "a@x.y"}])
    assert "single non-empty string" in errors(tests=[{"src": ["a@x.y"], "accept": ["tag:web:1"]}])


@pytest.mark.parametrize("dst", ["tag:web:22", "10.0.0.0/8", "autogroup:internet", "nas:22"])
def test_ssh_test_dst_shapes(dst):
    assert "not a single SSH destination" in errors(
        sshTests=[{"src": "a@x.y", "dst": [dst], "accept": ["root"]}])


def test_portset_str():
    assert str(PortSet.any()) == "*" and str(parse_ports("22,80-90")) == "22,80-90"
