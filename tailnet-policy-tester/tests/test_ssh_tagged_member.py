"""SSH src autogroup:tagged must not reach autogroup:member (README line ~140).

unittest-only: run with
    python3 -m unittest tests.test_ssh_tagged_member -v
"""

import json
import unittest

from tailnet_policy_tester.policy import PolicyError, parse_policy

MEMBER_MSG = "tags in SSH src cannot reach autogroup:member"
SELF_MSG = "autogroup:self SSH destination requires user sources"


def mk(**sections):
    data = {"tagOwners": {t: ["admin@example.com"] for t in
                          ("tag:web", "tag:db", "tag:ci", "tag:prod", "tag:router")}}
    data.update(sections)
    return parse_policy(json.loads(json.dumps(data)))


def ssh(**kw):
    base = {"action": "accept", "src": ["autogroup:member"],
            "dst": ["autogroup:self"], "users": ["root"]}
    base.update(kw)
    return base


def errors(**sections):
    try:
        mk(**sections)
    except PolicyError as e:
        return " | ".join(e.errors)
    raise AssertionError("expected PolicyError, got none")


class TestSshTaggedMember(unittest.TestCase):
    def test_tagged_to_member_rejected(self):
        self.assertIn(MEMBER_MSG, errors(ssh=[ssh(src=["autogroup:tagged"],
                                                  dst=["autogroup:member"])]))

    def test_tag_to_member_still_rejected(self):
        self.assertIn(MEMBER_MSG, errors(ssh=[ssh(src=["tag:ci"],
                                                  dst=["autogroup:member"])]))

    def test_user_to_member_still_accepted(self):
        p = mk(ssh=[ssh(src=["a@x.y"], dst=["autogroup:member"])])
        self.assertEqual(len(p.ssh), 1)
        self.assertEqual(p.ssh[0].dsts[0].value, "autogroup:member")

    def test_tagged_to_self_still_rejected(self):
        self.assertIn(SELF_MSG, errors(ssh=[ssh(src=["autogroup:tagged"],
                                                dst=["autogroup:self"])]))

    def test_tagged_to_tag_dst_still_accepted(self):
        # README ~140 lists only `*`, internet and mismatching user dsts as
        # invalid SSH destinations; tag sources can't reach member/self, a
        # tag dst is not restricted, so this must parse without errors.
        p = mk(ssh=[ssh(src=["autogroup:tagged"], dst=["tag:web"])])
        self.assertEqual(len(p.ssh), 1)
        self.assertEqual(p.ssh[0].dsts[0].value, "tag:web")


if __name__ == "__main__":
    unittest.main()
