import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
from common import *
from astrometron.router import route, validate_policy
root,data,store = env(40)
c = lambda **o: cfg(root,data,provider={"model":"m1","accuracy":0.6},**o)
pol = validate_policy({"task":"t","candidates":[{"kind":"mock","model":"m1","accuracy":0.6}],"requirements":{"min_success_rate":0.8,"min_cases":40}})
a = run_eval(c(), store)
print("honest: pass", a["aggregate"]["pass_rate"]["value"], "route:", route(store,pol)["status"])
fails=[x["case_id"] for x in a["cases"] if x["outcome"]!="PASS"]
b = run_eval(c(dataset={"exclude":[{"case_id":i,"reason":"ambiguous label"} for i in fails]}), store)
pr=b["aggregate"]["pass_rate"]; print("cherry-picked exclusions:", pr["value"], f"{pr['numerator']}/{pr['denominator']}", "requested", b["aggregate"]["requested"], "excluded", len(fails))
d = route(store,pol); print("route:", d["status"], d["selected"], "evidence run is excluded-run:", d["candidates"][0]["evidence"]["run_id"]==b["run_id"], "cases(min_cases basis):", d["candidates"][0]["evidence"]["cases"])
