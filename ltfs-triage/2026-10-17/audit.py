import json,re
c=json.load(open("census.json")); t=open("TRIAGE.md").read()
ids=[int(m) for m in re.findall(r'^\| \[#(\d+)\]',t,re.M)]
cls=re.findall(r'^\| \[#\d+\]\([^)]*\) \|.*?\| (repro-mhvtl|hardware-only|question/support|stale/duplicate|already-fixed-upstream) \|',t,re.M)
top=[int(x) for x in re.findall(r'^RANKED: #(\d+)',open("TOP5.md").read(),re.M)]
print("snapshot issues",len(c['open_issue_numbers']),"rows",len(ids),"unique",len(set(ids)))
print("set equal",set(ids)==set(c['open_issue_numbers']))
print("class rows",len(cls),"distinct classes",sorted(set(cls)))
print("ranked",top,"exist",all(x in ids for x in top),"in open PR set",[x for x in top if x in c['open_pr_numbers']])
print("issue/PR overlap",set(ids)&set(c['open_pr_numbers']))
