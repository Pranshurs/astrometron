import sys, json; sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
from common import *
from astrometron.dataset import load_file
from astrometron.router import route, validate_policy
from astrometron.providers import build_provider
# (1) tool failure swallowed by solver -> TASK_FAILURE/PASS, not TOOL_ERROR
root,data,store = env(4)
r = run_eval(cfg(root,data,provider={"kind":"python","function":"helpers:always_right","model":"p"},
     solver={"function":"helpers:swallow_tool_solver"}, tools=[{"name":"bad","function":"helpers:bad_tool"}]), store)
print("(1) counts:", {k:v for k,v in r["aggregate"]["counts"].items() if v}, "| trajectory has tool ERROR:", any(s.get("status")=="ERROR" for s in r["cases"][0]["trajectory"]), "| verify:", store.verify_run(r["run_id"]).ok)
# (2) invalid row: revision depends on line position / file name
d = Path(root)/"inv1.jsonl"; d2 = Path(root)/"inv2.jsonl"
rows=[{"id":"a","input":"x","expected":"1"},{"id":"b","expected":"2"},{"id":"c","input":"z","expected":"3"}]
d.write_text("".join(json.dumps(x)+"\n" for x in rows))
d2.write_text("".join(json.dumps(x)+"\n" for x in [rows[1],rows[0],rows[2]]))
print("(2) reorder revision equal (invalid row present)?", load_file(d,"x").revision==load_file(d2,"x").revision)
d3=Path(root)/"renamed.jsonl"; d3.write_text(d.read_text())
print("    renamed file same bytes, same revision?", load_file(d,"x").revision==load_file(d3,"x").revision)
# (3) content change in a dropped column keeps revision
f1=Path(root)/"c1.jsonl"; f2=Path(root)/"c2.jsonl"
f1.write_text(json.dumps({"uid":"u1","id":"OLD","question":"q","input":"in-A","answer":"a"})+"\n")
f2.write_text(json.dumps({"uid":"u1","id":"NEW","question":"q","input":"in-B","answer":"a"})+"\n")
fl={"id":"uid","input":"question","expected":"answer"}
print("(3) differing 'id'/'input' columns -> same revision?", load_file(f1,"x",fields=fl).revision==load_file(f2,"x",fields=fl).revision)
# (4) router: exclusions not part of basis, min_cases counts excluded
root,data,store = env(20)
c = lambda **o: cfg(root,data,provider={"model":"m1","accuracy":0.5},**o)
a = run_eval(c(), store)
fails=[x["case_id"] for x in a["cases"] if x["outcome"]!="PASS"]
print("(4) honest run pass rate", a["aggregate"]["pass_rate"]["value"], "failing ids", len(fails))
store2 = Store(Path(root)/".ev2")
b = run_eval(c(dataset={"exclude":[{"case_id":i,"reason":"ambiguous"} for i in fails]}), store2)
print("    cherry-picked exclusion pass rate", b["aggregate"]["pass_rate"]["value"], b["aggregate"]["pass_rate"]["denominator"], "requested", b["aggregate"]["requested"])
pol = validate_policy({"task":"t","candidates":[{"kind":"mock","model":"m1","accuracy":0.5}],"requirements":{"min_success_rate":0.9,"min_cases":20}})
print("    route honest store:", route(store,pol)["status"], "| route excluded-store:", route(store2,pol)["status"], route(store2,pol)["selected"])
# (5) openai-compat identity ignores base_url / params
i1=build_provider({"kind":"openai-compat","model":"x","base_url":"http://trusted:1/v1"}).identity()
i2=build_provider({"kind":"openai-compat","model":"x","base_url":"http://other:2/v1","headers":{"X":"1"}}).identity()
print("(5) identity equal for different base_url:", i1==i2, i1.key)
