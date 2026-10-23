"""Edge cases for individual rules, beyond the one-fixture-per-rule coverage."""

import pytest

from helpers import DIGEST, container, ids, lint


def test_clean_container_baseline():
    assert ids(container()) == []


# ---- keys and versions

def test_unknown_key_suggests_close_match():
    (f,) = lint(container("Healthcmd=/bin/true"), select={"QL003"})
    assert "did you mean HealthCmd=?" in f.message


def test_unknown_key_in_quadlet_section():
    assert ids(container() + "\n[Quadlet]\nDefaultDependencies=false\nBogus=1\n") == ["QL003"]


def test_unit_service_install_and_x_sections_are_not_key_checked():
    text = container() + "\n[Unit]\nAnything=1\n[Install]\nWantedBy=x\n[X-Container]\nWhatever=1\n"
    assert ids(text) == []


def test_key_too_new_reports_required_version():
    (f,) = lint(container("Pod=x.pod"), version="4.8", select={"QL004"})
    assert "needs Podman 5.0" in f.message and "target is 4.8" in f.message


def test_same_unit_is_clean_on_new_enough_podman():
    assert ids(container("StopTimeout=20"), version="5.0") == []
    assert ids(container("StopTimeout=20"), version="4.9") == ["QL004"]


def test_healthcmd_too_new_for_44():
    assert "QL004" in ids(container(), version="4.4")


def test_pod_file_on_podman_4_is_too_new():
    found = lint("[Pod]\n", kind="pod", version="4.9")
    assert [(f.rule, f.line) for f in found] == [("QL004", 1)]
    assert ".pod units need Podman 5.0" in found[0].message


# ---- booleans

@pytest.mark.parametrize("value", ["1", "yes", "TRUE", "On", "0", "no", "False", "off"])
def test_boolean_words_accepted(value):
    assert "QL007" not in ids(container(f"NoNewPrivileges={value}"))


def test_notify_accepts_healthy():
    assert "QL007" not in ids(container("Notify=healthy"))
    assert "QL007" in ids(container("Notify=ready"))


# ---- images

@pytest.mark.parametrize("image,expected", [
    (f"quay.io/a/b@sha256:{DIGEST}", []),
    (f"quay.io/a/b:1.0@sha256:{DIGEST}", []),
    ("quay.io/a/b:1.0", ["QL101"]),
    ("localhost/app:dev", ["QL101"]),
    ("registry.local:5000/app", ["QL101"]),
    ("app", ["QL101", "QL102"]),
    ("library/app:1", ["QL101", "QL102"]),
    ("app.image", []),
    ("app.build", []),
])
def test_image_references(image, expected):
    assert ids(container(image=image)) == expected


def test_image_short_digest_is_not_a_pin():
    assert ids(container(image="quay.io/a/b@sha256:abc")) == ["QL101"]


def test_volume_image_ignored_for_other_drivers():
    assert ids("[Volume]\nDriver=local\nImage=foo\n", kind="volume") == []


# ---- health

def test_health_cmd_via_podmanargs_counts():
    text = container().replace("HealthCmd=/bin/true\n", "PodmanArgs=--health-cmd /bin/true\n")
    assert "QL201" not in ids(text)


def test_health_cmd_none_is_no_healthcheck():
    text = container().replace("HealthCmd=/bin/true", "HealthCmd=none")
    assert "QL201" in ids(text)


def test_explicit_no_healthcheck_silences_ql201():
    text = container().replace("HealthCmd=/bin/true", "PodmanArgs=--no-healthcheck")
    assert "QL201" not in ids(text)


# ---- risky options

def test_privileged_false_is_fine():
    assert ids(container("PodmanArgs=--privileged=false")) == []


def test_security_opt_colon_form():
    assert ids(container("PodmanArgs=--security-opt=label:disable")) == ["QL302"]


def test_network_host_via_podmanargs_and_net_alias():
    assert ids(container("PodmanArgs=--net host")) == ["QL303"]


def test_network_quadlet_unit_is_fine():
    assert ids(container("Network=app.network\nNetwork=bridge")) == []


@pytest.mark.parametrize("volume", [
    "/run/user/1000/podman/podman.sock:/run/podman/podman.sock",
    "/var/run/docker.sock:/var/run/docker.sock:ro",
    "/run/podman:/run/podman",
])
def test_socket_mounts(volume):
    assert ids(container(f"Volume={volume}")) == ["QL304"]


def test_ordinary_volume_is_not_a_socket():
    assert ids(container("Volume=/srv/podman-data:/data:Z")) == []


def test_cap_add_via_podmanargs_comma_list():
    assert ids(container("PodmanArgs=--cap-add=CHOWN,sys_admin")) == ["QL306"]


def test_narrow_capabilities_are_fine():
    assert ids(container("AddCapability=NET_BIND_SERVICE CHOWN")) == []


def test_apparmor_unconfined():
    assert ids(container("AppArmor=unconfined")) == ["QL307"]
    assert ids(container("PodmanArgs=--security-opt seccomp=unconfined")) == ["QL307"]


def test_disabled_reset_list_is_not_reported():
    assert ids(container("Network=host\nNetwork=\nNetwork=app.network")) == []


# ---- restart / service

def test_pod_defaults_to_restart_on_failure():
    assert "QL401" not in ids("[Pod]\n\n[Service]\nRestartSec=5\n", kind="pod")
    assert ids("[Pod]\n", kind="pod") == []


def test_oneshot_container_restart_always():
    text = container(service="Type=oneshot\nRestart=always\nRestartSec=1")
    assert ids(text) == ["QL403"]


def test_volume_restart_on_failure_is_allowed():
    assert ids("[Volume]\n\n[Service]\nRestart=on-failure\n", kind="volume") == []


def test_start_limit_silences_ql404():
    text = container(service="Restart=always") + "\n[Unit]\nStartLimitBurst=20\n"
    assert ids(text) == []


def test_health_kill_with_restart_is_fine():
    assert ids(container("HealthOnFailure=kill")) == []
    assert ids(container("HealthOnFailure=none", service="RestartSec=1")) == ["QL401"]


# ---- stop timeouts

def test_stop_timeout_below_systemd_is_fine():
    assert ids(container("StopTimeout=25", service="Restart=on-failure\nRestartSec=1\nTimeoutStopSec=30")) == []


def test_stop_timeout_equal_to_systemd_warns():
    assert ids(container("StopTimeout=30", service="Restart=on-failure\nRestartSec=1\nTimeoutStopSec=30s")) == ["QL501"]


def test_timeoutsec_counts_as_stop_timeout():
    found = lint(container(service="Restart=on-failure\nRestartSec=1\nTimeoutSec=5"), select={"QL501"})
    assert len(found) == 1 and "TimeoutSec=5" in found[0].message


def test_stop_timeout_via_podmanargs():
    text = container("PodmanArgs=--stop-timeout 100")
    (f,) = lint(text, select={"QL501"})
    assert "default TimeoutStopSec (90s)" in f.message


def test_infinite_or_zero_systemd_timeout_never_conflicts():
    for value in ("infinity", "0"):
        assert ids(container("StopTimeout=600", service=f"Restart=on-failure\nRestartSec=1\nTimeoutStopSec={value}")) == []


def test_ql501_suggests_podmanargs_before_5_0():
    text = container(service="Restart=on-failure\nRestartSec=1\nTimeoutStopSec=5")
    (f,) = lint(text, version="4.9", select={"QL501"})
    assert "PodmanArgs=--stop-timeout" in f.message


def test_non_integer_stop_timeout():
    assert ids(container("StopTimeout=20s")) == ["QL503"]


# ---- read-only

def test_read_only_via_podmanargs():
    text = container().replace("ReadOnly=true", "PodmanArgs=--read-only")
    assert "QL601" not in ids(text)


def test_read_only_tmpfs_false_with_tmpfs_is_fine():
    assert ids(container("ReadOnlyTmpfs=false\nTmpfs=/tmp")) == []
    assert ids(container("ReadOnlyTmpfs=false\nMount=type=tmpfs,destination=/tmp")) == []


# ---- suppression

def test_file_level_disable():
    text = "# quadlet-lint: disable=QL101,QL102\n" + container(image="nginx")
    assert ids(text) == []


def test_disable_next_line_only_hits_that_line():
    text = container("# quadlet-lint: disable-next-line=QL303\nNetwork=host\nPodmanArgs=--network=host")
    found = lint(text, select={"QL303"})
    assert [f.message.startswith("PodmanArgs") for f in found] == [True]
