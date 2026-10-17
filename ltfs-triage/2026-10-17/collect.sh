#!/bin/sh
# usage: collect.sh <outdir>; unauthenticated, finite timeouts, public data only
OUT=${1:-raw}; B=https://api.github.com/repos/LinearTapeFileSystem/ltfs
fetch(){ # name url
  n=0; u="$2"
  while [ -n "$u" ] && [ $n -lt 15 ]; do
    n=$((n+1)); f="$OUT/$1.p$n.json"
    echo "$(date -u +%FT%TZ) $u" > "$f.meta"
    curl -sS --max-time 40 -D "$f.hdr" -o "$f" "$u" || { echo FAIL $u; return 1; }
    grep -iE '^(HTTP|link|x-ratelimit-remaining)' "$f.hdr" >> "$f.meta"
    u=$(grep -i '^link:' "$f.hdr" | tr ',' '\n' | sed -n 's/.*<\(.*\)>; rel="next".*/\1/p')
  done
}
fetch open_issues "$B/issues?state=open&per_page=100"
fetch open_pulls "$B/pulls?state=open&per_page=100"
fetch closed_issues "$B/issues?state=closed&since=2025-10-17T00:00:00Z&per_page=100"
fetch closed_pulls "$B/pulls?state=closed&sort=updated&direction=desc&per_page=100"
