# Security notes and known limitations

lto-exporter is a small personal/public project with no security team. If
you find a problem, open a GitHub issue without exploit details, or use
GitHub's private vulnerability reporting if it is enabled on the repository.

## What it is designed to guarantee

* The only program it executes is `sg_logs`, with exactly
  `--readonly --page=0x2|0x3 /dev/sgN`. This is enforced in
  `commands._check()`, which every spawn path calls. No shell.
* The only SCSI commands that result are INQUIRY and LOG SENSE (SP=0) of
  pages 0x02/0x03, issued by sg_logs on an `O_RDONLY` descriptor. No LOG
  SELECT, no TapeAlert read, no tape motion, no writes, no reset. The CI
  mhvtl job verifies this at the syscall/CDB level against an emulated drive.
* It needs no root and no capabilities: read access to one sg node.
* Nothing from the device (text, serials, paths) is exposed over HTTP other
  than the parsed counters and the alias you configured.

## What it does not guarantee (read before deploying)

* **Not tested on real tape hardware.** Only unit tests, real `sg_logs`
  output for synthetic pages, and the mhvtl emulator.
* **Device identity is point-in-time.** The exporter pins `/dev/sgN` at
  startup and re-checks before each scrape (symlink target, char major 21,
  sysfs type 1). Between that check and sg_logs' `open()` the kernel can
  re-assign the number to another device (hot-plug, HBA reset). The command
  would then be the same read-only LOG SENSE on the other device. Pinning a
  path string cannot prevent kernel device replacement.
* **SIGKILL is not a guarantee.** A `sg_logs` stuck in uninterruptible
  sleep on a hung device/HBA cannot be killed or reaped. The exporter stops
  spawning for that device (and globally after 4), exposes
  `tape_exporter_unreaped_children`, and keeps serving HTTP, but the stuck
  process stays until the kernel releases it (often only a reboot or HBA
  reset).
* **Unprivileged SG_IO filtering is a kernel policy.** On a read-only fd the
  kernel only allows a list of non-modifying commands. That is what keeps an
  unprivileged exporter read-only even if sg_logs misbehaved. Granting write
  access to the node or `CAP_SYS_RAWIO` removes that safety net.
* **Vendor behaviour.** Some drives may have side effects on LOG SENSE that
  the standard doesn't describe. Pages 0x02/0x03 are the least likely, but
  this is untested on real drives.
* **No TLS, no authentication.** Default bind is 127.0.0.1. Put a reverse
  proxy in front if you need remote scraping.
* **Supply chain.** Runtime has no Python dependencies. CI pins GitHub
  Actions by commit SHA, Prometheus by version + SHA-256, mhvtl by commit;
  the build tools (`build`, `twine`, setuptools) are pinned by version only,
  not by hash.
* The `contrib/` systemd and udev files are examples. They were checked with
  `systemd-analyze verify` / `security --offline`, not run under systemd by
  this project.
