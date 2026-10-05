import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
from common import *
from astrometron.reproduce import reproduce
from astrometron.compare import compare
# (a) total outage run "REPRODUCED"
root,data,store = env(10)
r = run_eval(cfg(root,data,provider={"model":"m1","infra_error_rate":1.0}), store)
print("original counts:", {k:v for k,v in r["aggregate"]["counts"].items() if v}, "pass_rate", r["aggregate"]["pass_rate"]["value"])
rep = reproduce(store, r["run_id"])
print("(a) verdict:", rep.verdict, rep.agreement["identical"], "/", rep.agreement["cases"])
# (b) nondeterministic python provider (pinned defaults True), 1 case
root,data,store = env(1)
c = cfg(root,data,provider={"kind":"python","function":"helpers:noisy","model":"n"})
verdicts=[]
for i in range(8):
    r = run_eval(c, store); verdicts.append(reproduce(store, r["run_id"]).verdict)
print("(b) noisy provider verdicts:", verdicts)
# (c) INCOMPLETE run compared as STRONG
root,data,store = env(10)
a = run_eval(cfg(root,data), store)
def prog(d,t):
    if d==3: raise KeyboardInterrupt
b = run_eval(cfg(root,data), store, progress=prog)
print("(c) b status", b["status"], {k:v for k,v in b["aggregate"]["counts"].items() if v})
res = compare(a,b); print("    comparability:", res["comparability"], res["warnings"], "pass", res["pass_rate"], res["paired"]["regressed"], res["paired"]["mcnemar_p"])
