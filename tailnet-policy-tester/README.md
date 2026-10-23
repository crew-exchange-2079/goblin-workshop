# tailnet-policy-tester

Offline evaluator for Tailscale policy files (HuJSON). Python 3.12+, standard
library only, nothing to install, no network access, no Tailscale account.

**It is a model, not the control plane.** Answers come from this tool's reading
of Tailscale's published semantics, not from Tailscale's compiler. Use it to
review a change before you apply it; the admin console (or the Tailscale API's
policy validate endpoint) remains the authority. Not affiliated with Tailscale Inc.

```console
$ python3 -m tailnet_policy_tester validate examples/policy.hujson
valid: 5 network rules, 2 ssh rules, 3 tests, 2 sshTests

$ python3 -m tailnet_policy_tester check examples/policy.hujson --src alice@example.com --dst tag:web:443
allow       alice@example.com -> tag:web tcp:443  via acls[0]

$ python3 -m tailnet_policy_tester ssh examples/policy.hujson --src carol@example.com --dst tag:db --user root
check  carol@example.com -> tag:db as root  via ssh[0] (check)  checkPeriod=12h

$ python3 -m tailnet_policy_tester test examples/policy.hujson
PASS         tests[0]
...
5 tests: 5 passed, 0 failed, 0 inconclusive

$ python3 -m tailnet_policy_tester diff examples/policy.hujson examples/policy-new.hujson
+ net alice@example.com -> tag:web tcp:8000-8100
+ net alice@example.com -> tag:web udp:8000-8100
+ net bob@example.com -> tag:web tcp:8000-8100
+ net bob@example.com -> tag:web udp:8000-8100
- ssh carol@example.com -> tag:db as root: check -> deny
- ssh carol@example.com -> tag:web as root: check -> deny
```

Run from this directory, or put it on `PYTHONPATH`. `-` reads the policy from stdin.

## Commands

| command | question | exit codes |
|---|---|---|
| `validate POLICY` | does it parse and pass the checks below? | 0 ok, 2 invalid |
| `check POLICY --src S --dst D:PORT [--proto tcp]` | can S reach D on PORT? (`--dst D` alone for icmp/gre/…) | 0 allow, 1 deny, 3 conditional |
| `ssh POLICY --src S --dst D --user LOGIN [--ssh-only]` | can S `ssh LOGIN@D`? | 0 accept/check, 1 deny |
| `test POLICY` | do the policy's own `tests` and `sshTests` pass? | 0 pass, 1 fail, 3 inconclusive |
| `diff OLD NEW` | which flows/logins does NEW newly allow or deny? | 0 no change, 1 changes |

All take `--json`. All but `validate` take `--inventory FILE`. Exit 2 is any
bad input (parse error, failed validation, unresolvable query).

`S` and `D` are a user (`alice@example.com`), group (expands to every member),
tag, `hosts` name, single IP, or (with an inventory) a device name. When a
name covers several endpoints every pair is reported, and the command only
succeeds if every pair does.

`ssh` also requires an acls/grants path on tcp/22, because a Tailscale SSH
connection still has to get through the packet filter; `--ssh-only` drops that
requirement. `test` (sshTests) and `diff` look at `ssh` rules only.

### Without an inventory (identity mode)

Offline there are no devices. `alice@example.com` becomes one synthetic,
untagged device owned by alice; `tag:web` one device with that tag; an IP an
address with no owner. Synthetic user/tag endpoints have **no IP**, so `hosts`
and CIDR rules never match them. That is the biggest way an offline answer can
differ from your tailnet.

### With an inventory

```jsonc
{
  "users": {"carol@example.com": {"roles": ["admin"]}},      // role autogroups
  "devices": [
    {"name": "web1", "tags": ["tag:web"], "ips": ["100.64.0.10"]},
    {"name": "alice-mbp", "user": "alice@example.com", "ips": ["100.64.0.2"]}
  ]
}
```

Devices let IP/host/CIDR rules match users and tags, give role autogroups
(`autogroup:admin`, …) something to match, and make `diff` enumerate your real
devices. See `examples/inventory.hujson`. A device with tags has no user
identity, whatever `user` says.

### `diff`

Endpoints are the inventory devices, or else one synthetic endpoint for every
user, tag, single-address host, CIDR (its first address), a stand-in
"unlisted member" (`unlisted-user@example.invalid`) and, if
`autogroup:internet` is used, `1.1.1.1`. For every ordered pair and protocol
(tcp, udp, sctp, icmp, ipv6-icmp, plus any protocol a rule names) it compares
the **exact** allowed port sets as intervals, so nothing is sampled except
endpoints. SSH is compared per login: `root`, every login named in a rule,
`<other-nonroot>` (what `autogroup:nonroot` grants), and each source's
localpart. Role autogroups need an inventory with roles; without one `diff`
prints a note.

## Semantics implemented

Primary sources (Tailscale knowledge base): policy syntax
<https://tailscale.com/kb/1337/policy-syntax>, grants
<https://tailscale.com/kb/1324/grants>, Tailscale SSH
<https://tailscale.com/kb/1193/tailscale-ssh>, tags
<https://tailscale.com/kb/1068/tags>, exit nodes / `autogroup:internet`
<https://tailscale.com/kb/1103/exit-nodes>, device posture
<https://tailscale.com/kb/1288/device-posture>, and HuJSON
<https://github.com/tailscale/hujson>.

Provenance, plainly: tailscale.com was not reachable from the environment this
was written in, so those pages were **not re-read** for this release; the
rules below are from prior knowledge of them, cross-checked line by line
against the open-source headscale v2 policy engine
(`hscontrol/policy/v2/{types,utils,test,sshtest,policy,filter}.go`, commit
`30ff8df9`), whose tests encode captured Tailscale SaaS behaviour. Three rows
(SSH order of evaluation, `tests` protocols, `defaultSrcPosture`) were corrected
after review against the current docs pages linked in those rows. Where this
tool goes beyond both, the row says "this tool".

| rule | source | tests |
|---|---|---|
| HuJSON: `//` and `/* */` comments, trailing commas after a value | hujson | `test_hujson.py` |
| Duplicate keys are an error (also case-only duplicates) | this tool (safer than Go's last-wins) | `test_duplicate_keys_rejected`, `test_case_only_duplicate_keys_rejected` |
| Section and field names are case-insensitive (`ACLs` = `acls`) | Go `encoding/json`, which the control plane uses | `test_keys_are_case_insensitive` |
| Default deny; access is the union of every `acls` and `grants` rule; there is no deny rule | policy syntax, grants | `test_default_deny`, `test_acls_and_grants_union`, `test_deny_action_rejected` |
| Legacy ACL fields `users`/`ports` mean `src`/`dst` | policy syntax | `test_legacy_users_ports_fields` |
| ACL `dst` is `alias:ports`, split at the last colon; `[v6]:port` accepted; ports `*`, `N`, `N-M`, lists | policy syntax; headscale `splitDestinationAndPort`, `parsePortRange` | `test_split_dst`, `test_parse_ports*` |
| `proto` omitted = TCP, UDP, ICMP, ICMPv6; names or IANA numbers 0-255 | policy syntax "proto" | `test_default_protos*`, `test_parse_proto*` |
| Grant `ip`: `*` = every protocol and port, `tcp:443`, bare `443` = default protocols; `app` grants carry no IP access | grants | `test_grant_ip`, `test_grant_any_proto`, `test_app_only_grant_has_no_network_rule` |
| ICMP and other portless protocols are allowed only by rules covering all ports | tailscaled packet filter (`matchProtoAndIPsOnlyIfAllPorts`), from memory | `test_icmp_needs_all_ports` |
| Tagged devices have no user identity; user/group rules don't match them | tags | `test_tagged_device_loses_user_identity` |
| Groups hold users only; nested groups are an error | policy syntax "groups" | `test_nested_groups_rejected` |
| Every tag used must be in `tagOwners`; every group in `groups`; every bare name in `hosts` | policy syntax | `test_undefined_tag_group_host` |
| `hosts` values are an IP or CIDR (host bits zero) | policy syntax "hosts" | `test_hosts_validation` |
| `autogroup:member` = untagged user devices; `autogroup:tagged` = tagged devices | policy syntax "autogroups" | `test_member_vs_tagged` |
| `autogroup:self` (dst only) = devices of the same user as the source; needs user-like sources | policy syntax; headscale `ErrACLAutogroupSelfInvalidSource` | `test_autogroup_self`, `test_self_needs_user_sources` |
| `autogroup:internet` (dst only) = public internet via exit nodes; `*` as a dst does not include it | exit nodes | `test_autogroup_internet`, `test_wildcard_dst_excludes_internet` |
| `autogroup:danger-all` (src only) = any source, including non-tailnet addresses | policy syntax | `test_danger_all_source` |
| Role autogroups (`owner`, `admin`, `it-admin`, `network-admin`, `billing-admin`, `auditor`) match users with that role | policy syntax "autogroups" | `test_role_autogroups_need_roles` |
| SSH: `action` accept or check; `checkPeriod` only on check, default 12h, `always` or a duration up to 168h | Tailscale SSH; headscale `SSHCheckPeriod` | `test_ssh_check_period` |
| SSH `users`: `root` only if listed; `autogroup:nonroot` = any login but root; literal names; `localpart:*@domain` = source user's local part | Tailscale SSH "users"; headscale `sshRuleAllowsUser` | `test_ssh_users` |
| SSH src may not be `*`; dst may not be `*`/internet; a user dst must equal the (single) user src; tag sources can't reach `autogroup:member` or `autogroup:self` | Tailscale SSH; headscale errors | `test_ssh_*_rejected`, `test_ssh_self_needs_user_src` |
| SSH also needs tcp/22 allowed by acls/grants (`ssh` command only) | Tailscale SSH, from prior knowledge | `test_ssh_requires_network_path` |
| SSH check rules are evaluated before accept rules: if any matching rule is `check` the action is check (its `checkPeriod`), else accept; all matches are listed | policy file "order of evaluation" <https://tailscale.com/docs/reference/syntax/policy-file#order-of-evaluation>; headscale sorts check before accept (`filter.go`) | `test_ssh_check_rules_win_over_accept`, `test_diff_adding_a_later_check_rule_changes_accept` |
| `tests`: one `src`; `accept`/`deny` are `dst:port` with one port; `proto` tcp, udp, sctp or icmp (ICMP destinations use port 0); omitted = passes if TCP or UDP is allowed | policy file "tests" <https://tailscale.com/docs/reference/syntax/policy-file#tests>. headscale differs (ICMP in the default set, ICMP tests rejected); the docs win here | `test_tests_block_shapes`, `test_default_test_proto_is_tcp_and_udp`, `test_icmp_tests_use_port_zero` |
| `defaultSrcPosture` applies to every acls/grants rule without its own `srcPosture` | device posture in access rules <https://tailscale.com/docs/features/device-posture/postures-in-access-rules> | `test_default_src_posture_is_inherited`, `test_default_posture_makes_allows_conditional` |
| `sshTests`: `accept` = allowed by accept or check rule, `check` = a check rule matches, `deny` = no rule; dst has no ports or CIDRs | policy syntax "sshTests"; headscale `sshtest.go` | `test_ssh_tests_accept_check_deny`, `test_ssh_test_dst_shapes` |
| Multi-endpoint test src/dst: accept needs every pair, deny needs every pair denied | this tool (stricter than headscale, which passes a deny if any source is blocked) | `test_group_source_is_strict` |

## Unsupported

Reported, never silently guessed:

* **`srcPosture`**, inherited **`defaultSrcPosture`** (acls/grants) and
  **`via`** (grants): not evaluated. A match that depends on them is reported
  `conditional` (exit 3); tests become `INCONCLUSIVE`; `ssh` reports
  `conditional` when the tcp/22 path does. `srcPosture` on SSH rules and
  `srcPostureAttrs` in tests are errors. SSH rules themselves are evaluated
  without `defaultSrcPosture` (whether it applies to them is not modelled).
* **Ignored sections** (warned): `nodeAttrs`, `autoApprovers`, `postures`,
  `ipsets`, `derpMap`, `disableIPv4`, `OneCGNATRoute`,
  `randomizeClientPort`, `attrConfig`. Unknown sections and fields are warned too.
* **Rejected as unsupported**: `ipset:` aliases, `autogroup:shared`.
* **Application grants** (`app`): parsed, warned, not evaluated.
* **Not modelled**: which devices advertise which subnet routes or act as exit
  nodes (a CIDR rule matches by address only), shared-in and external users
  (every user counts as `autogroup:member`), user/device approval, key expiry,
  Tailnet Lock, Serve/Funnel, SCIM-synced groups, IP ranges written `a-b`,
  Go durations below a millisecond (`us`, `ns`).
* Synthetic endpoints carry no IPs (see identity mode) and `diff` only
  enumerates the endpoint universe described above.

## Tests

```sh
python -m pytest -q tests      # 189 test cases; pytest from ../.github/ci-requirements.txt
```

MIT licensed (see `LICENSE`).
