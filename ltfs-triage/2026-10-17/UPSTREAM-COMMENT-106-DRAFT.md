# DRAFT - NOT POSTED

Proposed comment for https://github.com/LinearTapeFileSystem/ltfs/issues/106 (maintainers' call whether to post; nothing has been sent upstream). Evidence: `FIX-106.md`, `fixes/106.patch`, `fixes/106/`.

---

Hi, and thanks for maintaining LTFS. We drafted a small fix for this issue (not yet a PR): accept 0x1f in `_pathname_valid_in_xml` and stop `decode_entry_name` from rewriting a decoded US to `%1F` or `_` (the two TODOs).

We tested it on the file backend against efc9e3a. Today, creating a file, directory or symlink with a 0x1f name fails with EINVAL. With the patch they succeed, and after unmount and remount the names and file content read back correctly.

One side effect: an older LTFS that rewrites the index of such a tape renames those entries to literal `%1F` text (data intact).

Two questions:
1. What was the "historical issue" behind rejecting US?
2. Is that mixed-version rename acceptable, or would you prefer a different approach?

If a PR is welcome, we're happy to open one with the test.
