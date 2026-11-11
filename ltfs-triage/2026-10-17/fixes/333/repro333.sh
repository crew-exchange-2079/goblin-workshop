#!/bin/sh
# Repro/regression test for LTFS issue #333 (index cache write failure must not lose the index on tape).
# usage: repro333.sh <ltfs-install-prefix> <cache_fault.so>      (run as root; scenario B needs /dev/fuse)
# Uses the LTFS file backend (no tape hardware) and an LD_PRELOAD shim that makes writes to the
# index-cache file fail with ENOSPC. Exit status 0 = all scenarios pass, 1 = a failure was observed.
I=$1; SHIM=$2
[ -x "$I/bin/mkltfs" ] && [ -f "$SHIM" ] || { echo "usage: $0 <prefix> <shim.so>"; exit 2; }
export LD_LIBRARY_PATH=$I/lib
W=$(mktemp -d)
trap 'umount $W/mnt 2>/dev/null; [ -n "$KEEP" ] && cp -r $W "$KEEP"; rm -rf $W' EXIT
cd $W || exit 2
fail=0
quiet() { grep -v "^LTFS9015W" ; }

echo "== A: mkltfs (index smaller than one block -> close callback path)"
mkdir tapeA
LTFS_FI_CACHE=$W/cacheA.xml LD_PRELOAD=$SHIM $I/bin/mkltfs -e file -d tapeA -b 4096 -f >A.log 2>&1
rcA=$?
grep -E "FI:|LTFS1(1083|1279|5023|5024)|LTFS17245" A.log | quiet
echo "mkltfs exit=$rcA"
mkdir mntA 2>/dev/null
# can the medium be mounted afterwards (no fault)?
$I/bin/ltfs -o devname=$W/tapeA -o tape_backend=file mntA >mA.log 2>&1; mrc=$?
sleep 1; ok=$(mount | grep -c " $W/mntA ")
[ "$ok" -gt 0 ] && umount $W/mntA
echo "remount without fault: ltfs exit=$mrc mounted=$ok"
if [ $rcA -eq 0 ] && [ "$ok" -gt 0 ]; then echo "A: PASS"; else echo "A: FAIL"; fail=1; fi

echo "== B: mount + 60 files + unmount (index spans several 4 KiB blocks -> write callback path; cache is only used for the index partition write)"
mkdir tapeB mnt
$I/bin/mkltfs -e file -d tapeB -b 4096 -f >/dev/null 2>&1 || { echo "setup mkltfs failed"; exit 2; }
LTFS_FI_CACHE=$W/cacheB.xml LD_PRELOAD=$SHIM $I/bin/ltfs -f -o devname=$W/tapeB -o tape_backend=file -o sync_type=unmount mnt >B1.log 2>&1 &
LPID=$!
sleep 3
if ! mount | grep -q " $W/mnt "; then echo "setup mount failed"; tail -3 B1.log; exit 2; fi
i=0; while [ $i -lt 60 ]; do : > mnt/file_with_a_reasonably_long_name_to_grow_the_index_$i.txt; i=$((i+1)); done
umount $W/mnt; wait $LPID 2>/dev/null; sleep 1
grep -E "FI:|LTFS17(244|245)E|LTFS11(083|033)E|LTFS17236I.*Part = a" B1.log | sed 's/^[0-9a-f]* //' | sort | uniq -c | quiet
# the Unmount index of the index partition (a) must be written completely and unmount must not fail
ia=$(grep -c "LTFS17236I.*Part = a" B1.log); bad=$(grep -c "LTFS11033E" B1.log)
echo "index written to partition a at unmount: $ia (expected 1); 'Cannot unmount' errors: $bad (expected 0)"
if [ "$ia" -eq 1 ] && [ "$bad" -eq 0 ]; then echo "B: PASS"; else echo "B: FAIL"; fail=1; fi

[ $fail -eq 0 ] && echo "RESULT: PASS" || echo "RESULT: FAIL"
exit $fail
