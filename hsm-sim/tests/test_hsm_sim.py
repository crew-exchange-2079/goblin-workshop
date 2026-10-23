"""Tests for hsm-sim: golden timelines for the examples, validation, and model behaviour."""

import copy
import os
import pathlib
import re
import subprocess
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import hsm_sim  # noqa: E402
from hsm_sim import ScenarioError, parse_scenario, simulate  # noqa: E402

EXAMPLES = sorted((ROOT / "examples").glob("*.yaml"))

BASE = {
    "name": "t",
    "duration_s": 3600,
    "cache": {"capacity_mb": 1000, "high_watermark": 0.8, "low_watermark": 0.5},
    "library": {"drives": 2, "tapes": 3, "tape_capacity_mb": 1000,
                "mount_s": 60, "seek_s": 10, "rate_mb_s": 100},
    "policy": {"scan_interval_s": 300, "migrate_after_s": 0},
    "events": [{"t": 0, "op": "write", "file": "a", "size_mb": 100}],
}


def scenario(events=None, **sections):
    doc = copy.deepcopy(BASE)
    for key, value in sections.items():
        if isinstance(value, dict):
            doc[key].update(value)
        else:
            doc[key] = value
    if events is not None:
        doc["events"] = events
    return doc


def run(doc):
    sim = hsm_sim.Sim(parse_scenario(doc)).run()
    return sim, "\n".join(sim.lines)


# ---- golden outputs

def test_five_examples():
    assert len(EXAMPLES) == 5


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_golden(path):
    got = simulate(hsm_sim.load_scenario(path))
    golden = path.with_suffix(".out")
    if os.environ.get("HSM_SIM_UPDATE_GOLDEN") == "1":
        golden.write_text(got, encoding="utf-8")
    assert got == golden.read_text(encoding="utf-8"), (
        f"{golden.name} differs; if the change is intended, rerun with HSM_SIM_UPDATE_GOLDEN=1")


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
def test_deterministic(path):
    sc = hsm_sim.load_scenario(path)
    assert simulate(sc) == simulate(sc)


def test_cli_matches_golden_and_stats_only():
    path = EXAMPLES[0]
    full = subprocess.run([sys.executable, str(ROOT / "hsm_sim.py"), str(path)],
                          capture_output=True, text=True, check=True)
    assert full.stdout == path.with_suffix(".out").read_text(encoding="utf-8")
    brief = subprocess.run([sys.executable, str(ROOT / "hsm_sim.py"), "--stats-only", str(path)],
                           capture_output=True, text=True, check=True)
    assert "== timeline ==" not in brief.stdout
    assert "== stats ==" in brief.stdout


def test_cli_error_exit_code(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: x\n", encoding="utf-8")
    res = subprocess.run([sys.executable, str(ROOT / "hsm_sim.py"), str(bad)],
                         capture_output=True, text=True)
    assert res.returncode == 2
    assert res.stdout == ""
    assert "hsm-sim: error:" in res.stderr


def test_cli_unparsable_and_missing(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("events: [\n", encoding="utf-8")
    with pytest.raises(ScenarioError, match="not valid YAML"):
        hsm_sim.load_scenario(bad)
    with pytest.raises(ScenarioError):
        hsm_sim.load_scenario(tmp_path / "missing.yaml")


# ---- validation

@pytest.mark.parametrize("doc, message", [
    (scenario(cache={"low_watermark": 0.9}), "low_watermark must be below"),
    (scenario(cache={"capacity_mb": True}), "positive integer"),
    (scenario(cache={"high_watermark": 1.5}), r"number in \(0, 1\]"),
    (scenario(library={"drives": 1}, policy={"repack_below": 0.5}), "at least 2 drives"),
    (scenario(events=[{"t": 0, "op": "repack", "tape": "T01"}], library={"drives": 1}),
     "at least 2 drives"),
    (scenario(events=[{"t": 0, "op": "repack", "tape": "T09"}]), "no tape 'T09'"),
    (scenario(events=[{"t": 4000, "op": "read", "file": "a"}]), "after duration_s"),
    (scenario(events=[{"t": 0, "op": "read", "file": "a", "count": 3, "every_s": 2000}]),
     "after duration_s"),
    (scenario(events=[{"t": 0, "op": "write", "file": "a", "size_mb": 2000}]),
     "does not fit on one"),
    (scenario(events=[{"t": 0, "op": "write", "file": "a", "size_mb": 900}],
              cache={"capacity_mb": 800, "high_watermark": 0.8}), "larger than the cache"),
    (scenario(events=[{"t": 0, "op": "write", "file": "a{i}{i.x}", "size_mb": 1, "count": 2}]),
     "placeholders"),
    (scenario(events=[{"t": 0, "op": "write", "file": "a", "size_mb": 1, "count": 2}]),
     "needs an \\{i\\} placeholder"),
    (scenario(events=[{"t": 0, "op": "copy", "file": "a"}]), "op: must be one of"),
    (scenario(events=[{"t": 0, "op": "read", "file": "a", "size_mb": 1}]), "unknown key"),
    (scenario(events=[{"t": 0, "op": "read", "file": False}]), "non-empty string"),
    (scenario(events=[]), "non-empty list"),
    (scenario(colour="blue"), "unknown key"),
    (dict((k, v) for k, v in BASE.items() if k != "policy"), "policy: required"),
    (["not", "a", "mapping"], "expected a mapping"),
])
def test_validation(doc, message):
    with pytest.raises(ScenarioError, match=message):
        parse_scenario(doc)


def test_yaml_no_is_not_a_file_name(tmp_path):
    # YAML 1.1 reads a bare `no` as false; the validator must say so, not crash.
    path = tmp_path / "s.yaml"
    path.write_text("name: t\nduration_s: 10\ncache: {capacity_mb: 10, high_watermark: 0.8, "
                    "low_watermark: 0.5}\nlibrary: {drives: 1, tapes: 1, tape_capacity_mb: 10, "
                    "mount_s: 1, seek_s: 1, rate_mb_s: 1}\npolicy: {scan_interval_s: 5, "
                    "migrate_after_s: 0}\nevents:\n  - {t: 0, op: read, file: no}\n",
                    encoding="utf-8")
    with pytest.raises(ScenarioError, match="file: must be a non-empty string, got False"):
        hsm_sim.load_scenario(path)


def test_count_expansion_and_ordering():
    sc = parse_scenario(scenario(events=[
        {"t": 10, "op": "read", "file": "x"},
        {"t": 0, "op": "write", "file": "f{i:02d}", "size_mb": 1, "count": 3, "every_s": 10},
    ]))
    got = [(e["t"], e["op"], e["file"]) for e in sc["events"]]
    # Same-time events keep file order: the read (listed first) precedes f01's write.
    assert got == [(0, "write", "f00"), (10, "read", "x"), (10, "write", "f01"),
                   (20, "write", "f02")]


# ---- behaviour

def test_lifecycle_states():
    sim, log = run(scenario(events=[
        {"t": 0, "op": "write", "file": "a", "size_mb": 300},
        {"t": 400, "op": "write", "file": "b", "size_mb": 600},
        {"t": 1000, "op": "read", "file": "a"},
    ]))
    assert "migrate   start on drive0: 1 file(s) 300 MB -> T01 (aged): a" in log
    assert "purge     1 file(s) 300 MB for watermark cycle (down to 50%): a -> stubs" in log
    assert "read      a miss: stub only, recall queued from T01" in log
    assert sim.files["a"].state == hsm_sim.PREMIGRATED
    assert sim.st["latencies"] == [10 + 3]  # T01 still mounted: seek + 300 MB at 100 MB/s


def test_write_rejected_when_cache_holds_only_resident_data():
    sim, log = run(scenario(
        events=[{"t": 0, "op": "write", "file": f"f{i}", "size_mb": 400} for i in range(3)],
        policy={"migrate_after_s": 3000}))
    assert "f2 400 MB REJECTED" in log
    assert sim.st["writes_rejected"] == 1
    assert "f2" not in sim.files


def test_recalls_for_one_tape_share_a_mount_in_tape_order():
    events = [{"t": 0, "op": "write", "file": f"f{i}", "size_mb": 100} for i in range(5)]
    events += [{"t": 1000, "op": "write", "file": "fill", "size_mb": 900}]
    events += [{"t": 2000, "op": "read", "file": n} for n in ("f3", "f0", "f4")]
    sim, log = run(scenario(events=events, library={"drives": 1},
                            cache={"capacity_mb": 2000, "high_watermark": 0.4,
                                   "low_watermark": 0.05}))
    starts = [line for line in sim.lines if "recall    start" in line]
    # f3 goes first on its own; f0 and f4 queue behind it and then share one pass.
    assert len(starts) == 2
    assert "2 file(s) 200 MB from T01 in tape order: f0, f4, 22 s" in starts[1]
    assert sim.tape_by_id["T01"].mounts == 2  # migration, then f3; f0 and f4 need no new mount


def test_delete_during_recall_fails_the_reader_and_discards_the_copy():
    sim, log = run(scenario(
        events=[{"t": 0, "op": "write", "file": "a", "size_mb": 900},
                {"t": 1500, "op": "read", "file": "zzz"},
                {"t": 1500, "op": "read", "file": "a"},
                {"t": 1500, "op": "delete", "file": "a"}],
        library={"drives": 1, "mount_s": 600}))
    assert sim.st["read_errors"] == 2  # zzz never existed; a was deleted mid-recall
    assert "1 waiting read(s) fail" in log
    assert "0 file(s) back on disk; 1 discarded (changed meanwhile)" in log
    assert sim.reserved == 0 and sim.used == 0


def test_delete_cancels_a_queued_recall():
    sim, log = run(scenario(
        events=[{"t": 0, "op": "write", "file": "a", "size_mb": 500},
                {"t": 0, "op": "write", "file": "b", "size_mb": 300},
                {"t": 1500, "op": "read", "file": "a"},
                {"t": 1500, "op": "read", "file": "b"},  # same tape, queued behind a
                {"t": 1501, "op": "delete", "file": "b"}],
        library={"drives": 1}, cache={"high_watermark": 0.5, "low_watermark": 0.1}))
    assert "read      b miss: stub only, recall queued from T01" in log
    assert "recall    start" in log and log.count("recall    start") == 1
    assert sim.st["read_errors"] == 1
    assert not sim.queue


def test_overwrite_during_migration_leaves_a_stale_copy():
    sim, log = run(scenario(events=[
        {"t": 0, "op": "write", "file": "a", "size_mb": 500},
        {"t": 310, "op": "write", "file": "a", "size_mb": 100},
    ]))
    assert "done -> T01: 0 file(s) premigrated; 1 copy(ies) already stale" in log
    t01 = sim.tape_by_id["T01"]
    assert t01.used >= 500 and t01.live <= 100


def test_repack_moves_live_data_and_frees_tape():
    events = [{"t": 0, "op": "write", "file": f"f{i}", "size_mb": 100, } for i in range(10)]
    events += [{"t": 1000, "op": "delete", "file": f"f{i}"} for i in range(7)]
    events += [{"t": 2000, "op": "write", "file": "big", "size_mb": 900},
               {"t": 3000, "op": "read", "file": "f9"}]
    sim, log = run(scenario(events=events, policy={"repack_below": 0.5},
                            cache={"capacity_mb": 2000, "high_watermark": 0.4,
                                   "low_watermark": 0.05}))
    assert "repack    T01 queued (live 30% < 50%): 300/1000 MB live" in log
    assert "reclaim   T01 -> scratch (repacked onto T02)" in log
    assert sim.files["f9"].seg.tape.id != "T01"
    assert "recall queued from T02" in log or "recall queued from T03" in log
    assert sim.st["repacked_mb"] == 300


def test_empty_full_tape_is_reclaimed_without_a_mount():
    events = [{"t": 0, "op": "write", "file": f"f{i}", "size_mb": 250} for i in range(4)]
    events += [{"t": 1000, "op": "delete", "file": f"f{i}"} for i in range(4)]
    sim, log = run(scenario(events=events, cache={"capacity_mb": 2000}))
    assert "reclaim   T01 -> scratch (no live data left)" in log
    assert sim.drives[0].mounts + sim.drives[1].mounts == 1  # the one migration mount


def test_manual_repack_of_scratch_tape_is_ignored():
    _sim, log = run(scenario(events=[{"t": 0, "op": "repack", "tape": "T03"}]))
    assert "T03 requested but it is a scratch tape, ignored" in log


def test_unfinished_work_is_reported():
    sc = scenario(events=[{"t": 0, "op": "write", "file": "a", "size_mb": 100}],
                  duration_s=320, library={"mount_s": 600})
    out = simulate(parse_scenario(sc))
    assert re.search(r"^unfinished +1 running, 0 queued: migrate$", out, re.M)


def test_migration_stalls_without_scratch_tape():
    sim, log = run(scenario(
        events=[{"t": 0, "op": "write", "file": f"f{i}", "size_mb": 200} for i in range(6)],
        library={"tapes": 1}, cache={"capacity_mb": 2000}))
    assert "wait      migrate stalled: no scratch tape left" in log
    assert sim.files["f5"].state == hsm_sim.RESIDENT
    assert sim.tape_by_id["T01"].state == hsm_sim.FULL


def test_recall_waits_for_cache_space():
    # The cache is full of resident data that cannot be purged, so a recall has
    # nowhere to land until migration makes some of it purgeable.
    sim, log = run(scenario(
        events=[{"t": 0, "op": "write", "file": "old", "size_mb": 300},
                {"t": 400, "op": "write", "file": "fill", "size_mb": 800},
                {"t": 401, "op": "read", "file": "old"}]))
    assert "purge     1 file(s) 300 MB for room for fill: old -> stubs" in log
    assert "wait      recall old: no cache space to recall into" in log
    assert sim.files["old"].state == hsm_sim.PREMIGRATED
    assert sim.st["latencies"][0] > 0
