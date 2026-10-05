import sys, time, threading; sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
from common import *
from astrometron.router import route, validate_policy
root,data,store = env(6)
r = run_eval(cfg(root,data,provider={"kind":"python","function":"helpers:sleepy","model":"s"},run={"timeout_s":0.1,"concurrency":3,"retries":0}), store)
print("timeout run:", {k:v for k,v in r["aggregate"]["counts"].items() if v}, "verify", store.verify_run(r["run_id"]).ok, "threads alive right after run:", threading.active_count())
c0=r["cases"][0]; print("  cost:", c0["cost_usd"], c0["cost_basis"], "complete:", r["aggregate"]["cost"]["complete"])
# router: cost limit with unknown cost; incomplete run
root,data,store = env(20)
a = run_eval(cfg(root,data), store)
pol = lambda **q: validate_policy({"task":"t","candidates":[{"kind":"mock","model":"m1"}],"requirements":q})
print("cost-unknown w/ limit:", route(store,pol(max_cost_per_case_usd=1.0))["candidates"][0]["status"])
def prog(d,t):
    if d==5: raise KeyboardInterrupt
b = run_eval(cfg(root,data), store, progress=prog)
print("incomplete status", b["status"], "route evidence run is the complete run:", route(store,pol())["candidates"][0]["evidence"]["run_id"]==a["run_id"])
# dataset changed since evidence; no pin
import json
data.write_text("".join(json.dumps({"id":f"c{i}","input":f"q{i}","expected":"CHANGED"})+"\n" for i in range(20)))
d=route(store,pol()); print("dataset edited on disk since evidence -> route:", d["status"])
