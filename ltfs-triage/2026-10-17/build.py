# usage: python3 -I build.py <snapshot.json> ; writes census.json, TRIAGE.md (reads/writes cwd)
import json,sys,hashlib
d=json.load(open(sys.argv[1])); R=d['responses']
prs={i['number'] for i in R[1]['items']}
iss=sorted((i for i in R[0]['items'] if i['number'] not in prs),key=lambda i:i['number'])
HW={201,343,371,505,517,528,640,656,638,527,582,591,554,350,240,264,266,498,290,291}
PIN="https://github.com/LinearTapeFileSystem/ltfs/blob/efc9e3a5c498504c03c2f1d0b931bae058922104/"
REPRO={106:"source read: src/libltfs/pathname.c:442 and xml_reader_libltfs.c:125,147 reject US(0x1f); maintainer TODO cites #106. Sketch (not run): libltfs pathname validate on 'a\x1fb', or file-backend mount + touch.",
 333:"source read: src/libltfs/xml_writer.c:101-133,157-181 aborts index write on cache-file error; xml_writer_libltfs.c:894-897. Sketch (not run): file backend + index cache on a full/failing fs.",
 305:"source partly read (ltfs.c mount/traverse); needs new recovery logic for two EOD layouts; large. Sketch (not run): hand-craft file-backend image with trailing partial index.",
 535:"source read: filedebug_tc.c:1349-1380 rejects cart_type not matching emulated drive generation (drive_type from product_id, L577-606); may be by design/doc gap. Sketch (not run): issue's mkltfs -e file steps.",
 522:"UNVERIFIED symptom (body not read); closed PR 660 (2-line xattr str fix) NOT merged into main per fresh clone; ltfs_ordered_copy is pure python, runs against any mounted file-backend volume."}
DUP={392:"covered by OPEN PR 658 (diff adds dir attr walk in ltfs_ordered_copy)",513:"covered by OPEN PR 659 (diff adds --sort-files to man page)",661:"covered by OPEN PR 662 (diff: isdir(dst) -> join basename)",650:"covered by OPEN PR 651 (diff: NULL supported-device table guard in sg_tape.c, cam_tc.c); matches title, body not cross-checked",
 541:"real on main (main.c:1246-1272 ret overwritten by ltfs_set_signal_handlers) but FIXED IN OPEN PR 603 ('return fuse_failed ? 1 : ret')"}
FIXED={598:"fixed on main by merged PR 599, commit ba1695e (2026-07-29), ltfs_ordered_copy single-file path L413-432",
 414:"fixed on main by commit 7d5c5af (#415, 2023-08-24) 'Fix a memory leak at tape_read_attr()'; tape.c:3616-3645 frees inner_buf on every path"}
QUESTION_EXTRA={308:"design discussion, not a bug: forward traverse for rollback already exists (ltfs.c:1875, ltfsck traverse_mode)"}
rows=[];cnt={}
for i in iss:
    n=i['number'];L=[l if isinstance(l,str) else l.get('name') for l in i['labels']]
    if n in FIXED: c,w,cf="already-fixed-upstream",FIXED[n],"high (commit read in fresh clone)"
    elif n in QUESTION_EXTRA: c,w,cf="question/support",QUESTION_EXTRA[n],"medium"
    elif n in DUP: c,w,cf="stale/duplicate",DUP[n],"medium (PR diff read)"
    elif n in HW: c,w,cf="hardware-only","drive/HBA/firmware/timing behaviour per title/labels "+str(L),"low (title/labels only)"
    elif n in REPRO: c,w,cf="repro-mhvtl",REPRO[n]+". ","medium (source read)" if n in (106,333,535) else "UNVERIFIED"
    else: c,w,cf="question/support","question/enhancement/build/doc/discussion, not a concrete bug per title/labels "+str(L),"low (title/labels only)"
    cnt[c]=cnt.get(c,0)+1
    rows.append((n,i['html_url'],i['title'].replace('|','/')[:90],c,w.replace('|','/'),cf))
cls=["repro-mhvtl","hardware-only","question/support","stale/duplicate","already-fixed-upstream"]
t="# TRIAGE - open non-PR issues, snapshot 2026-10-17\n\nSource: attached normalized public API snapshot (not raw HTTP), fetched 2026-10-17T09:03Z. Classes are title/body/label-based judgments, not runs. Maintainer comment threads were not supplied. Updated 2026-10-23 with a fresh clone of upstream at efc9e3a (same commit as the snapshot) and PR heads fetched read-only.\n\n"+"Counts: "+", ".join(f"{c}={cnt.get(c,0)}" for c in cls)+f" (total {len(rows)})\n\n| # | title | class | reason/source | confidence |\n|---|---|---|---|---|\n"
for n,u,ti,c,w,cf in rows: t+=f"| [#{n}]({u}) | {ti} | {c} | {w} | {cf} |\n"
open("TRIAGE.md","w").write(t)
json.dump({"collected_utc":d['collected_utc'],"cutoff":d['cutoff_closed_at_utc'],"source_commits":d['source_commits'],
 "responses":[{k:r[k] for k in('kind','url','fetched_utc','original_response_sha256','original_count','has_next_link','headers')} for r in R],
 "open_issue_numbers":[r[0] for r in rows],"open_pr_numbers":sorted(prs),
 "closed_issues_in_window_closed_at":sum(1 for i in R[2]['items'] if i['closed_at'] and i['closed_at']>=d['cutoff_closed_at_utc']),
 "closed_prs_in_window_closed_at":sum(1 for i in R[3]['items'] if i['closed_at'] and i['closed_at']>=d['cutoff_closed_at_utc']),
 "closed_prs_min_updated":min(i['updated_at'] for i in R[3]['items'])},open("census.json","w"),indent=1)

import csv
with open("triage/ALL.csv","w",newline="") as f:
    w=csv.writer(f);w.writerow(["issue","url","title","class","reason_source","confidence"])
    for n,u,ti,c,wh,cf in rows: w.writerow([n,u,ti,c,wh,cf])
