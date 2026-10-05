import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
from common import *
root,data,store = env(4)
T=[{"name":"bad","function":"helpers:bad_tool"}]
P={"kind":"python","function":"helpers:always_right","model":"p"}
r = run_eval(cfg(root,data,provider=P,solver={"function":"helpers:swallow_tool_solver"},tools=T), store)
c=r["cases"][0]; print("tool-then-model:", c["outcome"], {k:v for k,v in c.items() if "degrad" in k}, r["aggregate"].get("degraded"), store.verify_run(r["run_id"]).ok)
r = run_eval(cfg(root,data,provider=P,solver={"function":"helpers:tool_after_model"},tools=T), store)
c=r["cases"][0]; print("model-then-tool-fail:", c["outcome"], c["error"] and c["error"].get("stage"), store.verify_run(r["run_id"]).ok)
for fn in ["nan_latency","surrogate","inf_tokens"]:
    try:
        r = run_eval(cfg(root,data,provider={"kind":"python","function":"helpers:"+fn,"model":"p"}), store)
        print(fn, {k:v for k,v in r["aggregate"]["counts"].items() if v}, store.verify_run(r["run_id"]).ok)
    except Exception as e: print(fn,"ABORT",type(e).__name__,e)
# invalid-row digest stability across reorder/rename, with CSV too
