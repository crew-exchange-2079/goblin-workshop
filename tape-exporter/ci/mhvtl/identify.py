"""Identify the ONE emulated tape drive this harness created.

A SCSI generic node qualifies only if ALL hold:
  * its SCSI host's proc_name is "mhvtl" (the emulator's pseudo adapter),
  * peripheral type 1 (tape), vendor IBM, model ULT3580-TD8,
  * unit serial number (VPD 0x80) equals the synthetic --serial,
  * /dev/sgN is a char device 21:N matching sysfs.
Exactly one match is required. Prints /dev/sgN on stdout and writes the
evidence JSON. Serials of non-mhvtl devices are never recorded.
"""

import argparse
import glob
import json
import os
import re
import stat
import subprocess
import sys

SYS = "/sys/class/scsi_generic"


def rd(path, binary=False):
    try:
        with open(path, "rb") as fh:
            data = fh.read(4096)
    except OSError:
        return None
    return data if binary else data.decode("ascii", "replace").strip()


def serial_of(sg, devdir):
    raw = rd(os.path.join(devdir, "vpd_pg80"), binary=True)
    if raw and len(raw) >= 4 and raw[1] == 0x80:
        n = int.from_bytes(raw[2:4], "big")
        return raw[4:4 + n].decode("ascii", "replace").strip(), "sysfs vpd_pg80"
    # Fallback: INQUIRY VPD 0x80 via sg_inq (read-only).
    r = subprocess.run(["sg_inq", "--page=0x80", f"/dev/{sg}"],
                       capture_output=True, text=True, timeout=20)
    m = re.search(r"Unit serial number:\s*(\S+)", r.stdout)
    return (m.group(1) if m else None), "sg_inq --page=0x80"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    seen, matches = [], []
    for sgdir in sorted(glob.glob(os.path.join(SYS, "sg*"))):
        sg = os.path.basename(sgdir)
        devdir = os.path.realpath(os.path.join(sgdir, "device"))
        host = next((p for p in devdir.split("/") if re.fullmatch(r"host[0-9]+", p)), None)
        proc_name = rd(f"/sys/class/scsi_host/{host}/proc_name") if host else None
        info = {"sg": sg, "sysfs_device": devdir, "host": host, "proc_name": proc_name,
                "type": rd(os.path.join(devdir, "type")),
                "vendor": rd(os.path.join(devdir, "vendor")),
                "model": rd(os.path.join(devdir, "model")),
                "rev": rd(os.path.join(devdir, "rev")),
                "sg_dev": rd(os.path.join(sgdir, "dev"))}
        if proc_name == "mhvtl":
            info["serial"], info["serial_source"] = serial_of(sg, devdir)
        seen.append(info)
        if (proc_name == "mhvtl" and info["type"] == "1" and info["vendor"] == "IBM"
                and info["model"] == "ULT3580-TD8" and info.get("serial") == args.serial):
            matches.append(info)

    result = {"wanted_serial": args.serial, "all_sg_nodes": seen,
              "matches": [m["sg"] for m in matches]}
    ok = len(matches) == 1
    if ok:
        m = matches[0]
        node = f"/dev/{m['sg']}"
        st = os.stat(node)
        dev = f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"
        result["node"] = {"path": node, "is_char": stat.S_ISCHR(st.st_mode), "major_minor": dev,
                          "mode": oct(st.st_mode & 0o7777)}
        ok = stat.S_ISCHR(st.st_mode) and os.major(st.st_rdev) == 21 and dev == m["sg_dev"]
        result["virtual_evidence"] = {
            "scsi_host_proc_name": m["proc_name"],
            "sysfs_device": m["sysfs_device"],
            "mhvtl_module_loaded": any(l.startswith("mhvtl ") for l in open("/proc/modules")),
        }
    result["ok"] = ok
    with open(args.out, "w") as fh:
        json.dump(result, fh, indent=2)
    if not ok:
        print(f"identify: {len(matches)} matching device(s); need exactly 1", file=sys.stderr)
        return 1
    print(result["node"]["path"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
