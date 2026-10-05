# index-only edit of runs.task (receipt untouched, digest binding intact) -> router uses evidence for the wrong task
import sys, sqlite3; sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
from common import *
from astrometron.router import route, validate_policy
root,data,store = env(20)
r = run_eval(cfg(root,data,**{"task":"summarize"}) if False else cfg(root,data), store)  # task "t"
rid = r["run_id"]
print("receipt task:", r["eval"]["task"])
pol = lambda t: validate_policy({"task":t,"candidates":[{"kind":"mock","model":"m1"}],"requirements":{"min_success_rate":0.5}})
print("route for 'medical' before tamper:", route(store,pol("medical"))["status"])
con = sqlite3.connect(store.db_path); con.execute("UPDATE runs SET task='medical' WHERE run_id=?", (rid,)); con.commit(); con.close()
print("verify_run after index-only tamper:", store.verify_run(rid).ok)
d = route(store,pol("medical"))
print("route for 'medical' after tamper:", d["status"], d["selected"], d["candidates"][0].get("evidence",{}).get("run_id"))
# same for provider/model/dataset columns: verify still ok?
con = sqlite3.connect(store.db_path); con.execute("UPDATE runs SET model='other', pass_rate=0.0, dataset_id='zzz', pinned=0 WHERE run_id=?", (rid,)); con.commit(); con.close()
print("verify_run after editing model/pass_rate/dataset_id columns:", store.verify_run(rid).ok)
