#!/bin/sh
# Round-trip test for LTFS issue #106: a file name containing US (0x1f) must survive write/unmount/remount.
# usage: repro106.sh <prefix-under-test> <prefix-of-unfixed-build>     (run as root; needs /dev/fuse)
# File backend only (no tape hardware). Exit 0 = round trip PASS, 1 = FAIL. The cross-version part is informational.
P=$1; OLD=$2
[ -x "$P/bin/mkltfs" ] && [ -x "$OLD/bin/ltfs" ] || { echo "usage: $0 <prefix> <old-prefix>"; exit 2; }
W=$(mktemp -d); trap 'umount $W/mnt 2>/dev/null; rm -rf $W' EXIT; cd $W || exit 2
mkdir mnt tape
US=$(printf '\037'); NAME="aaa${US}aaa"
mnt() { LD_LIBRARY_PATH=$1/lib $1/bin/ltfs -o devname=$W/tape -o tape_backend=file mnt >mnt.log 2>&1; sleep 1; mount | grep -q " $W/mnt " || { echo "mount failed with $1"; tail -2 mnt.log; return 1; }; }
unmnt() { umount $W/mnt; sleep 1; }
show() { ls -b mnt | sed 's/^/    /'; }

LD_LIBRARY_PATH=$P/lib $P/bin/mkltfs -e file -d tape -f >/dev/null 2>&1 || { echo "mkltfs failed"; exit 2; }
echo "== 1. create (build under test: $(basename $P))"
mnt $P || exit 2
printf hello > "mnt/$NAME" 2>err1.txt; rc1=$?
echo "create file name 'aaa<US>aaa': rc=$rc1 $(cat err1.txt)"
mkdir "mnt/dir${US}x" 2>err2.txt; echo "mkdir  name 'dir<US>x': rc=$?"
ln -s tgt "mnt/lnk${US}y" 2>err3.txt; echo "symlink name 'lnk<US>y': rc=$?"
: > "mnt/lit%1Fz"   # literal percent sign text, must stay distinct from a real US
unmnt
echo "index on tape contains encoded name: $(grep -al 'aaa%1Faaa\|aaa%1faaa' tape/*_R 2>/dev/null | wc -l) record(s)"
echo "== 2. remount and read back"
mnt $P || exit 2
echo "names (ls -b):"; show
ok=0
if [ -f "mnt/$NAME" ] && [ "$(cat "mnt/$NAME")" = hello ] && [ -d "mnt/dir${US}x" ] && [ -L "mnt/lnk${US}y" ] && [ -f "mnt/lit%1Fz" ] && [ ! -e "mnt/lit${US}z" ]; then ok=1; fi
unmnt
if [ $ok -eq 1 ]; then echo "ROUND TRIP: PASS (file content, dir and symlink found by their 0x1f names; literal '%1F' name stays distinct)"; else echo "ROUND TRIP: FAIL"; fi

echo "== 3. informational: tape written by build under test, read and then rewritten by the UNFIXED build"
if [ $ok -eq 1 ]; then
  mnt $OLD || exit 2; echo "unfixed build sees:"; show; touch mnt/dirty; unmnt
  mnt $P || exit 2; echo "fixed build sees after the unfixed build rewrote the index:"; show; unmnt
fi
[ $ok -eq 1 ] && exit 0 || exit 1
