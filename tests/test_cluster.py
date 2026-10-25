import pytest

from baic_preflight.checks import BLOCKER, OK, WARN, check_cluster, parse_cpu, parse_mem_gib


def levels(found):
    return {f.subject: f.level for f in found}


def test_quantities():
    assert parse_cpu("3920m") == pytest.approx(3.92)
    assert parse_cpu("4") == 4
    assert parse_mem_gib("8Gi") == 8
    assert parse_mem_gib("8388608Ki") == 8
    assert parse_mem_gib("1000000000") == pytest.approx(0.931, abs=1e-3)


def test_guide_minimum_cluster_is_tight(spec, fixture):
    found = check_cluster(fixture("nodes-guide-minimum.json"), spec, fixture("storageclass-none.json"))
    lv = levels(found)
    assert lv["kubernetes"] == OK and lv["nodes"] == OK
    assert lv["capacity"] == WARN  # 11.8 vCPU allocatable vs ~12 vCPU footprint
    assert lv["storageclass"] == WARN


def test_roomy_cluster_skips_control_plane(spec, fixture):
    found = check_cluster(fixture("nodes-roomy.json"), spec, fixture("storageclass-default.json"))
    assert all(f.level == OK for f in found)
    assert any("3 schedulable" in f.message for f in found)


def test_old_kubernetes_and_small_cluster(spec, fixture):
    nodes = fixture("nodes-guide-minimum.json")
    nodes["items"] = nodes["items"][:2]
    for n in nodes["items"]:
        n["status"]["nodeInfo"]["kubeletVersion"] = "v1.26.9"
    lv = levels(check_cluster(nodes, spec))
    assert lv["kubernetes"] == BLOCKER
    assert lv["nodes"] == WARN
    assert lv["capacity"] == BLOCKER  # 7.8 vCPU is far under ~12


def test_server_version_wins_over_kubelet(spec, fixture):
    found = check_cluster(fixture("nodes-roomy.json"), spec, server_version="v1.25.0")
    assert levels(found)["kubernetes"] == BLOCKER
