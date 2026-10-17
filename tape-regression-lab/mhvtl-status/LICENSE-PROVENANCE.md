# License and provenance

- **Underlying project:** [mhvtl](https://github.com/markh794/mhvtl) by Mark Harvey and
  contributors, GPL-2.0-or-later (per its source headers: "version 2 of the License, or
  (at your option) any later version"). Full text: upstream
  [`COPYING`](https://github.com/markh794/mhvtl/blob/7ca971131c3e68d3a37073b83d235848dfa46559/COPYING).
  No mhvtl source is copied into this kit. The kit builds and runs *upstream's* code,
  fetched fresh at run time from the pinned commit.
- **Production guard:** the missing-page guard in `lookup_device_status_pg()` (upstream
  issue [#168](https://github.com/markh794/mhvtl/issues/168), commit `622df7c`) is the
  upstream authors' work. This kit does not re-fix or propose any production change.
- **`tests.patch`:** a test-only patch (new `tests/test_log_device_status.c` plus one
  `TESTS +=` line and one rule in `tests/Makefile`) authored by the Goblin Workshop crew
  with Claude Code. It reuses upstream's bundled `acutest.h` (MIT, unchanged, not
  vendored here) and follows the conventions of upstream's `tests/test_open_fifo.c`.
  Licensed **GPL-2.0-or-later**, compatible with mhvtl, so upstream may take it as is.
  The patch is not endorsed by, submitted to, or written on behalf of the mhvtl authors.
- **`mutation-missing-guard.diff`:** a diagnostic diff that deletes the upstream guard, for
  a disposable checkout only. It is GPL-2.0-or-later like the code it edits. Never ship it.
- **`run.py`, `run.sh`, this README and `validation/`:** GPL-2.0-or-later.
- No other component licenses in this repository change.
