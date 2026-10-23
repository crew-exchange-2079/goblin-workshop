import json

import pytest

from conftest import EXAMPLE, INVENTORY, mk
from tailnet_policy_tester.cli import main
from tailnet_policy_tester.diff import diff, subtract
from tailnet_policy_tester.engine import load_inventory
from tailnet_policy_tester.policy import load_policy
from tailnet_policy_tester.runtests import run_all


def acl(src, dst, **kw):
    return {"action": "accept", "src": src, "dst": dst, **kw}


# --- tests / sshTests ----------------------------------------------------------

def test_example_tests_pass(example_text):
    assert all(r.status == "PASS" for r in run_all(load_policy(example_text)))


def test_example_tests_pass_with_inventory(example_text):
    inv = load_inventory(INVENTORY.read_bytes())
    assert all(r.status == "PASS" for r in run_all(load_policy(example_text), inv))


def test_failing_accept_and_deny():
    p = mk(acls=[acl(["*"], ["tag:web:22"])],
           tests=[{"src": "a@x.y", "accept": ["tag:web:443"], "deny": ["tag:web:22"]}])
    (r,) = run_all(p)
    assert r.status == "FAIL" and len(r.failures) == 2


def test_test_proto_restricts():
    p = mk(acls=[acl(["*"], ["tag:web:53"], proto="udp")],
           tests=[{"src": "a@x.y", "proto": "tcp", "deny": ["tag:web:53"]},
                  {"src": "a@x.y", "accept": ["tag:web:53"]}])
    assert [r.status for r in run_all(p)] == ["PASS", "PASS"]


def test_default_test_proto_does_not_let_icmp_ignore_ports():
    p = mk(acls=[acl(["*"], ["tag:web:80"])], tests=[{"src": "a@x.y", "deny": ["tag:web:22"]}])
    assert run_all(p)[0].status == "PASS"


def test_conditional_is_inconclusive():
    p = mk(acls=[acl(["*"], ["tag:web:443"], srcPosture=["posture:x"])],
           tests=[{"src": "a@x.y", "accept": ["tag:web:443"]}])
    assert run_all(p)[0].status == "INCONCLUSIVE"


def test_group_source_is_strict():
    p = mk(groups={"group:eng": ["a@x.y", "b@x.y"]}, acls=[acl(["a@x.y"], ["tag:web:443"])],
           tests=[{"src": "group:eng", "accept": ["tag:web:443"]},
                  {"src": "group:eng", "deny": ["tag:web:443"]}])
    assert [r.status for r in run_all(p)] == ["FAIL", "FAIL"]


def test_unresolvable_source_fails():
    p = mk(tests=[{"src": "group:ghost", "accept": ["tag:web:1"]}])
    (r,) = run_all(p)
    assert r.status == "FAIL" and "unknown group" in r.failures[0]


def test_ssh_tests_accept_check_deny():
    p = mk(ssh=[{"action": "accept", "src": ["autogroup:member"], "dst": ["tag:web"], "users": ["deploy"]},
                {"action": "check", "src": ["autogroup:member"], "dst": ["tag:web"], "users": ["root"]}],
           sshTests=[{"src": "a@x.y", "dst": ["tag:web"], "accept": ["deploy", "root"], "check": ["root"],
                      "deny": ["admin"]},
                     {"src": "a@x.y", "dst": ["tag:web"], "check": ["deploy"]},
                     {"src": "a@x.y", "dst": ["tag:web"], "deny": ["root"]}])
    assert [r.status for r in run_all(p)] == ["PASS", "FAIL", "FAIL"]


# --- diff ------------------------------------------------------------------------

@pytest.mark.parametrize("a,b,expected", [
    (((0, 65535),), ((22, 22),), ((0, 21), (23, 65535))),
    (((10, 20),), ((0, 100),), ()),
    (((10, 20), (30, 40)), ((15, 35),), ((10, 14), (36, 40))),
    (((1, 5),), (), ((1, 5),)),
    ((), ((1, 5),), ()),
])
def test_subtract(a, b, expected):
    assert subtract(a, b) == expected


def test_identical_policies_have_no_diff(example_text):
    p = load_policy(example_text)
    assert diff(p, load_policy(example_text))[0] == []


def test_diff_port_widening_and_narrowing():
    old = mk(acls=[acl(["a@x.y"], ["tag:web:80-90"])])
    new = mk(acls=[acl(["a@x.y"], ["tag:web:85-100"], proto="tcp")])
    got = {str(c) for c in diff(old, new)[0]}
    assert "+ net a@x.y -> tag:web tcp:91-100" in got
    assert "- net a@x.y -> tag:web tcp:80-84" in got
    assert "- net a@x.y -> tag:web udp:80-90" in got


def test_diff_icmp_and_member_representative():
    old = mk()
    new = mk(acls=[acl(["autogroup:member"], ["tag:db:*"])])
    got = {str(c) for c in diff(old, new)[0]}
    assert "+ net unlisted-user@example.invalid -> tag:db icmp" in got
    assert "+ net unlisted-user@example.invalid -> tag:db tcp:*" in got


def test_diff_ssh_changes():
    rule = {"action": "accept", "src": ["a@x.y"], "dst": ["tag:web"], "users": ["root"]}
    old = mk(ssh=[rule])
    new = mk(ssh=[dict(rule, action="check"),
                  dict(rule, users=["autogroup:nonroot"])])
    got = {str(c) for c in diff(old, new)[0]}
    assert "~ ssh a@x.y -> tag:web as root: accept -> check" in got
    assert "+ ssh a@x.y -> tag:web as <other-nonroot>: deny -> accept" in got


def test_diff_internet_and_hosts():
    old = mk(hosts={"nas": "100.64.0.50"})
    new = mk(hosts={"nas": "100.64.0.50"},
             acls=[acl(["a@x.y"], ["autogroup:internet:*", "nas:445"])])
    got = {str(c) for c in diff(old, new)[0]}
    assert "+ net a@x.y -> internet(1.1.1.1) tcp:*" in got
    assert "+ net a@x.y -> nas tcp:445" in got


def test_diff_conditional_marked():
    old = mk()
    new = mk(acls=[acl(["a@x.y"], ["tag:web:443"], srcPosture=["posture:x"])])
    assert any("(conditional)" in str(c) for c in diff(old, new)[0])


def test_diff_with_inventory_uses_devices(example_text):
    inv = load_inventory(INVENTORY.read_bytes())
    old = load_policy(example_text)
    new = load_policy(example_text.replace('"tag:db:5432"]}', '"tag:db:5432,5433"]}'))
    got = {str(c) for c in diff(old, new, inv.devices)[0]}
    assert got == {"+ net web1 -> db1 tcp:5433", "+ net web1 -> db1 udp:5433",
                   "+ net web2 -> db1 tcp:5433", "+ net web2 -> db1 udp:5433"}


def test_diff_notes_role_autogroups():
    new = mk(acls=[acl(["autogroup:admin"], ["tag:db:22"])])
    assert diff(mk(), new)[1]


# --- CLI ---------------------------------------------------------------------------

E = str(EXAMPLE)


@pytest.mark.parametrize("argv,code", [
    (["validate", E], 0),
    (["test", E], 0),
    (["check", E, "--src", "alice@example.com", "--dst", "tag:web:443"], 0),
    (["check", E, "--src", "alice@example.com", "--dst", "tag:db:5432"], 1),
    (["check", E, "--src", "carol@example.com", "--dst", "tag:db", "--proto", "gre"], 0),
    (["check", E, "--src", "alice@example.com", "--dst", "tag:web"], 2),
    (["check", E, "--src", "nobody", "--dst", "tag:web:443"], 2),
    (["ssh", E, "--src", "carol@example.com", "--dst", "tag:web", "--user", "root"], 0),
    (["ssh", E, "--src", "alice@example.com", "--dst", "tag:web", "--user", "root"], 1),
    (["diff", E, E], 0),
    (["validate", "/nonexistent/policy.hujson"], 2),
])
def test_cli_exit_codes(argv, code, capsys):
    assert main(argv) == code


def test_cli_check_with_inventory(capsys):
    assert main(["check", E, "--inventory", str(INVENTORY), "--src", "ci1", "--dst", "nas:445"]) == 0
    assert "ci1 -> nas" in capsys.readouterr().out


def test_cli_ssh_reports_network_gap(tmp_path, capsys):
    pol = tmp_path / "p.hujson"
    pol.write_text('{"tagOwners": {"tag:web": []}, "ssh": [{"action": "accept",'
                   ' "src": ["autogroup:member"], "dst": ["tag:web"], "users": ["root"]}]}')
    assert main(["ssh", str(pol), "--src", "a@x.y", "--dst", "tag:web", "--user", "root"]) == 1
    assert "no network path" in capsys.readouterr().out
    assert main(["ssh", str(pol), "--src", "a@x.y", "--dst", "tag:web", "--user", "root", "--ssh-only"]) == 0


def test_cli_json(capsys):
    assert main(["check", E, "--json", "--src", "alice@example.com", "--dst", "tag:web:443"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["results"][0]["rules"] == ["acls[0]"]


def test_cli_test_failure_and_inconclusive(tmp_path, capsys):
    pol = tmp_path / "p.hujson"
    pol.write_text('{"tagOwners": {"tag:web": []},'
                   ' "acls": [{"action": "accept", "src": ["*"], "dst": ["tag:web:443"], "srcPosture": ["posture:x"]}],'
                   ' "tests": [{"src": "a@x.y", "accept": ["tag:web:443"]}]}')
    assert main(["test", str(pol)]) == 3
    pol.write_text('{"tagOwners": {"tag:web": []}, "tests": [{"src": "a@x.y", "accept": ["tag:web:443"]}]}')
    assert main(["test", str(pol)]) == 1


def test_cli_validation_error_lists_all(tmp_path, capsys):
    pol = tmp_path / "p.hujson"
    pol.write_text('{"acls": [{"action": "deny", "src": ["group:x"], "dst": ["tag:y:1"]}]}')
    assert main(["validate", str(pol)]) == 2
    err = capsys.readouterr().err
    assert "deny" in err and "group:x" in err and "tag:y" in err


def test_cli_diff_reports(tmp_path, capsys):
    new = tmp_path / "new.hujson"
    new.write_text(EXAMPLE.read_text().replace('"tag:web:443"]}', '"tag:web:443,8443"]}'))
    assert main(["diff", E, str(new)]) == 1
    assert "+ net alice@example.com -> tag:web tcp:8443" in capsys.readouterr().out


def test_cli_hujson_error_position(tmp_path, capsys):
    pol = tmp_path / "p.hujson"
    pol.write_text('{\n  // c\n  "acls": [\n    {"action": }\n  ]\n}')
    assert main(["validate", str(pol)]) == 2
    assert "line 4" in capsys.readouterr().err


def test_readme_diff_example(capsys):
    new = str(EXAMPLE.parent / "policy-new.hujson")
    assert main(["diff", E, new]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "+ net alice@example.com -> tag:web tcp:8000-8100",
        "+ net alice@example.com -> tag:web udp:8000-8100",
        "+ net bob@example.com -> tag:web tcp:8000-8100",
        "+ net bob@example.com -> tag:web udp:8000-8100",
        "- ssh carol@example.com -> tag:db as root: check -> deny",
        "- ssh carol@example.com -> tag:web as root: check -> deny",
    ]


def test_stdin_policy(monkeypatch, capsys):
    import io
    import sys
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(EXAMPLE.read_bytes())))
    assert main(["validate", "-"]) == 0
