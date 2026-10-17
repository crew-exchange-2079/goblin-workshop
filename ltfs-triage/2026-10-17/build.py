# usage: python3 -I build.py <snapshot.json> ; writes census.json, TRIAGE.md (reads/writes cwd)
import json,sys,hashlib
d=json.load(open(sys.argv[1])); R=d['responses']
prs={i['number'] for i in R[1]['items']}
iss=sorted((i for i in R[0]['items'] if i['number'] not in prs),key=lambda i:i['number'])
HW={201,343,371,505,517,528,640,656,638,527,582,591,554,350,240,264,266,498,290,291}
REPRO={106:"filename with 0x1f char rejected; likely filename validation in libltfs (arch/filename_handling.c, ltfs_fsops.c) - UNVERIFIED, source not read",
 333:"index write aborts if cache-file write fails; xml_output_tape_write_callback() per issue body - UNVERIFIED, source not read",
 414:"leak on MAM read, tape_read_attr() per title (src/libltfs/tape.c) - UNVERIFIED, source not read",
 541:"mount exit status: src/main.c L1246-1272 read at pinned commit - ret from fuse_main() is overwritten by ltfs_set_signal_handlers() before return (source read; behaviour not run)",
 535:"file backend only works for LTO5 format/type (tape_drivers/generic/file) - UNVERIFIED",
 305:"index recovery logic vs trailing invalid index (libltfs/ltfs_super.c?) - UNVERIFIED",
 308:"rollback forward traverse (libltfs) - UNVERIFIED"}
DUP={392:"covered by OPEN PR 658",513:"covered by OPEN PR 659",661:"covered by OPEN PR 662",650:"covered by OPEN PR 651 (NULL supported-device table)",
 522:"closed PR 660 'accept str values from xattr.get() (#522)'; merge state NOT in snapshot - UNVERIFIED",
 598:"closed PR 599 'single file special case' may fix; merge state NOT in snapshot - UNVERIFIED, no already-fixed claim"}
rows=[];cnt={}
for i in iss:
    n=i['number'];L=[l if isinstance(l,str) else l.get('name') for l in i['labels']]
    if n in DUP: c,w,cf="stale/duplicate",DUP[n],"medium" if n in(392,513,661,650) else "UNVERIFIED"
    elif n in HW: c,w,cf="hardware-only","drive/HBA/firmware/timing behaviour per title/labels "+str(L),"low (title/labels only)"
    elif n in REPRO: c,w,cf="repro-mhvtl",REPRO[n]+". Sketch: against mhvtl or file backend, mkltfs/mount a throwaway virtual tape and trigger the symptom; reasoned, NOT run.","UNVERIFIED"
    else: c,w,cf="question/support","question/enhancement/build/doc/discussion, not a concrete bug per title/labels "+str(L),"low (title/labels only)"
    cnt[c]=cnt.get(c,0)+1
    rows.append((n,i['html_url'],i['title'].replace('|','/')[:90],c,w.replace('|','/'),cf))
cls=["repro-mhvtl","hardware-only","question/support","stale/duplicate","already-fixed-upstream"]
t="# TRIAGE - open non-PR issues, snapshot 2026-10-17\n\nSource: attached normalized public API snapshot (not raw HTTP), fetched 2026-10-17T09:03Z. Classes are title/body/label-based judgments, not runs. Maintainer comment threads were not supplied. No already-fixed-upstream claim could be evidenced (no merged-state data), so that class is 0.\n\n"+"Counts: "+", ".join(f"{c}={cnt.get(c,0)}" for c in cls)+f" (total {len(rows)})\n\n| # | title | class | reason/source | confidence |\n|---|---|---|---|---|\n"
for n,u,ti,c,w,cf in rows: t+=f"| [#{n}]({u}) | {ti} | {c} | {w} | {cf} |\n"
open("TRIAGE.md","w").write(t)
json.dump({"collected_utc":d['collected_utc'],"cutoff":d['cutoff_closed_at_utc'],"source_commits":d['source_commits'],
 "responses":[{k:r[k] for k in('kind','url','fetched_utc','original_response_sha256','original_count','has_next_link','headers')} for r in R],
 "open_issue_numbers":[r[0] for r in rows],"open_pr_numbers":sorted(prs),
 "closed_issues_in_window_closed_at":sum(1 for i in R[2]['items'] if i['closed_at'] and i['closed_at']>=d['cutoff_closed_at_utc']),
 "closed_prs_in_window_closed_at":sum(1 for i in R[3]['items'] if i['closed_at'] and i['closed_at']>=d['cutoff_closed_at_utc']),
 "closed_prs_min_updated":min(i['updated_at'] for i in R[3]['items'])},open("census.json","w"),indent=1)
