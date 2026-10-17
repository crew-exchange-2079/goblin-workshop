# mhvtl log-page regression kit

A public developer test artifact. It runs a real-library regression test for mhvtl's
optional log pages (`DEVICE_STATUS` 0x11, `TAPE_ALERT` 0x2e, `SEQUENTIAL_ACCESS_DEVICE`
0x0c) in a fresh, disposable checkout of upstream.

This is **not** a production fix and not a fuzzer. mhvtl and its production fix belong
to their authors: upstream [#168](https://github.com/markh794/mhvtl/issues/168) is
already fixed (guard commit `622df7c`) and this kit does not touch it. See
[LICENSE-PROVENANCE.md](LICENSE-PROVENANCE.md). Nothing here was sent to upstream.

## What it checks

`tests.patch` adds `tests/test_log_device_status.c` to an upstream checkout, plus a
minimal `tests/Makefile` hook. The test links the real `libvtlscsi.so` and builds
synthetic in-memory page lists with the real `add_log_*` / `dealloc_all_log_pages` API.
`lookup_device_status_pg()` is static, so it is exercised through the public setters.

| case | checks |
|---|---|
| `empty_list_is_safe` | on an initialised empty `lunit.log_pg`: `set_TapeAlert(0)` and nonzero flags return -1; the DEVICE_STATUS setters are no-ops; `set_current_state` and `set_tape_load_status` still update global state |
| `tape_alert_without_other_pages` | only TAPE_ALERT: flags 0, bit 19, bit 20 and bit 63 give the expected 64 per-flag bytes in the registered page; a missing SEQUENTIAL_ACCESS_DEVICE page is allowed |
| `device_status_page_present` | setters set and clear their fields; flag 19/20 drive CRQRD/CRQST; TAFC follows zero/nonzero; `b6` activity codes and medium bits follow state |
| `sequential_access_field` | flags `0x8000000000180001` give bytes `80 00 00 00 00 18 00 01`; free/reinit leaves no state behind |

The expected bit patterns are literal in the test, not derived from production code.
The only symbol the test supplies is the usual `char mhvtl_driver_name[]`.

## Prerequisites

`git`, `gcc`, `make`, `python3` (3.8+, stdlib only), network access to clone
`https://github.com/markh794/mhvtl.git`. Sanitizer runs need gcc's `libasan`/`libubsan`.
The existing suite also needs dev headers for lz4, zstd, lzo2 and libudev
(Debian/Ubuntu: `liblz4-dev libzstd-dev liblzo2-dev libudev-dev`). If a tool or header is
missing the runner reports BLOCKED with the exact reason. It never reports PASS for
something it did not run. No sudo, kernel modules, daemons, hardware or installs are used.

## Run

```sh
./run.sh                                  # all four checks, ~1 minute
./run.sh --evidence-dir /some/new/dir     # also write compact summary.json + trimmed logs
./run.sh --keep --work /new/empty/dir     # keep the build trees
./run.sh --skip-suite                     # skip the long existing `make test`
./run.sh --upstream /path/to/mirror       # alternative read-only source, same pin check
```

`run.sh` resolves its own directory and clones the upstream into a new temporary
directory. It verifies the literal base `7ca971131c3e68d3a37073b83d235848dfa46559` and that the
patch touches only the two permitted paths. It then makes four separate fresh trees:
`normal`, `san`, `mutant`, and the shared upstream clone they come from. It never uses
a caller's checkout and deletes only the directory it created. Exit 0 means all four PASS
(`FULL_ACCEPTANCE`). One overall deadline (`--deadline`, default 1200 s) covers all child
processes, which run in their own process groups and are killed on timeout. Each command's
log is capped (`--log-cap-kb`, default 256).

| result | meaning |
|---|---|
| `DIRECT_REGRESSION` | `make -C usr libvtlscsi.so`, `make -C tests test_log_device_status`, run with `LD_LIBRARY_PATH` at the build-tree library; `ldd` must resolve that exact `.so`; 4/4 cases OK |
| `EXISTING_SUITE` | `make -C etc`, `make -C usr` (helpers), then `make -C tests test`, all inside the temp tree |
| `SANITIZERS` | library built with `make -C usr libvtlscsi.so RPM_OPT_FLAGS="-fsanitize=address,undefined ..."` (keeps upstream's includes, defines and `-fpic`) and test built with `CC="gcc -fsanitize=..."`; requires ASan/UBSan references in the production `mhvtl_log.o`, both binaries linking libasan, and no diagnostics |
| `MUTATION_CONTROL` | in a third tree, apply [mutation-missing-guard.diff](mutation-missing-guard.diff) (removes only the `if (!log_pg) return NULL;` guard), build as above, run `empty_list_is_safe`; must exit nonzero with a diagnostic at `mhvtl_log.c` and not a load or link error |

To reproduce the sanitizer or mutation steps by hand, copy the commands from
[validation/build-commands.txt](validation/build-commands.txt) into your own fresh checkout.
Do not apply the mutation diff to a tree you intend to keep.

## Actual validation

Run on 2026-10-17, gcc 13.3.0, GNU make 4.3, git 2.43.0, Ubuntu 24.04 container,
base `7ca9711…` (upstream `master` had not moved past it). All four results were PASS in
one `./run.sh` run (about 62 s). Evidence is in [validation/](validation/):

- `summary.json`: per-result evidence, source and patch hashes, library/test/object hashes,
  the library path resolved by each executed test, the exact commands run.
- `build-commands.txt`: the actual compile and link lines for the normal, sanitizer and
  mutant builds.
- `n-direct.log`, `n-suite.log`, `s-run.log`, `m-run.log` and a few helper logs (trimmed).
  In `m-run.log` the mutant fails at `mhvtl_log.c:414` in `lookup_device_status_pg`,
  called from `set_TapeAlert`, with "member access within null pointer".

The dev packages for the existing suite were installed with `apt-get` in the throwaway
container before the run. Paths in evidence are shown as `<WORK>` / `<KIT>`.

## Limitations

- Test-only, pinned to one upstream commit. A later upstream may need the patch rebased.
- Open-PR overlap check at the pin: PRs #280-#287 touch no device-status coverage. PR 286
  edits other lines of `tests/Makefile` (script list, integration tests), so a textual
  merge conflict is possible but the patch adds its own separate `TESTS +=` line to stay
  small. Not exhaustive for newer PRs.
- Existing tests use hard-coded `/tmp/mhvtl-*` temp directories of their own; they are
  upstream's behaviour and are left alone.
- Linux/GNU toolchain only (ASan/UBSan, `ldd`, `nm`, `readelf`). The mutation control
  shows UBSan catching the null access; whether ASan reports a SEGV instead depends on
  inlining.
- Not a CI workflow; no CI files are added. A time limit here is an operating bound, not a
  provider-enforced spending cap.
