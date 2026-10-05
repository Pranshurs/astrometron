# receipt-file-only forgery (re-sealed, index untouched): verify says INVALID but compare happily uses it
import sys, json; sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
from common import *
from astrometron.receipt import seal
from astrometron.compare import compare
import subprocess
root,data,store = env(20)
a = run_eval(cfg(root,data,provider={"model":"m1","accuracy":0.5}), store)
b = run_eval(cfg(root,data,provider={"model":"m2","accuracy":0.5}), store)
forged = json.loads(json.dumps(b))
for c in forged["cases"]:
    c["outcome"]="PASS"; c["score"]={"passed":True,"value":1.0}; c["error"]=None
from astrometron.receipt import aggregate
forged["aggregate"]=aggregate(forged["cases"],forged["dataset"]["selected_case_ids"],[])
forged=seal(forged)
(store.root/"runs"/b["run_id"]/"receipt.json").write_text(json.dumps(forged))
print("verify_run(b):", store.verify_run(b["run_id"]).ok, store.verify_run(b["run_id"]).problems[:1])
import os
out = subprocess.run([sys.executable,"-m","astrometron.cli","compare",a["run_id"],b["run_id"],"--json"],capture_output=True,text=True,env={**os.environ,"ASTROMETRON_HOME":str(store.root)})
print(out.stdout[:300],out.stderr[-300:]);res=json.loads(out.stdout); print("CLI compare:", res["comparability"], res.get("reasons"))
