# examples

End-to-end walkthroughs that chain the workshop's tools on synthetic data.
Each one is a directory with a `walkthrough.sh` that runs in containers, a
README that explains each step, and a short recording (`walkthrough.cast`
for asciinema, `walkthrough.gif` for the README). CI runs every live
walkthrough end to end.

| walkthrough | chains | status |
|---|---|---|
| [exporter → Grafana](exporter-to-grafana/) | `tape-exporter` → Prometheus + alert rules → Grafana dashboard | **live**, tested in CI |
| `validator-to-viewer` | `ltfs-index-validator` JSON report → validator web viewer | slot: waiting for the viewer, which is being built separately |
| `label-gen-to-capacity-planner` | `tape-label-gen` volser ranges → `lto-capacity-planner` | slot: waiting for [#21](https://github.com/crew-exchange-2079/goblin-workshop/pull/21) (planner) and [#24](https://github.com/crew-exchange-2079/goblin-workshop/pull/24) (label-gen) |

The slots are left empty on purpose. A walkthrough is added only once every
tool it chains is on `main`, so it never documents an interface that might
still change in review. [#28](https://github.com/crew-exchange-2079/goblin-workshop/issues/28) lists what each slot needs.

## Run

```sh
examples/exporter-to-grafana/walkthrough.sh          # needs Docker + compose, python3
python3 examples/run_walkthrough.py exporter-to-grafana   # same run, checked, always torn down (what CI does)
python -m pytest -q examples/tests                   # static checks, no Docker
```

## Adding a walkthrough

1. `examples/<a>-to-<b>/walkthrough.sh`: each command goes through
   `step` so it's echoed. Pass `--down` to tear down at the end. Images are
   pinned by digest, ports bind to `127.0.0.1`, and repo files are mounted
   read-only rather than copied.
2. Add its expected output lines to `EXPECT` in `run_walkthrough.py`, and its
   name to `LIVE` in `tests/test_examples.py`.
3. Record it: `python3 examples/run_walkthrough.py <name> --cast examples/<name>/walkthrough.cast`,
   then make the GIF with [agg](https://github.com/asciinema/agg) 1.7.0
   (`agg --idle-time-limit 2 --font-size 14 --last-frame-duration 6 …`).
4. Add a step for it to the `examples` job in `.github/workflows/ci.yml`.
